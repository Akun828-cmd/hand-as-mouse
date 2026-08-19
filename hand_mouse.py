"""
Hand Gesture Mouse Controller
-----------------------------
Controls your mouse cursor using hand gestures captured from a webcam.

Gestures:
    - Move cursor:  Move your index fingertip around the frame.
    - Left click:   Pinch thumb tip and index fingertip together (quick tap).
    - Right click:  Pinch thumb tip and middle fingertip together.
    - Scroll:       Raise index + middle fingers together ("peace sign")
                     and move hand up/down.
    - Drag:         Hold the left-click pinch and move your hand.

Press 'q' in the camera window to quit.

Windows-only: the mouse is controlled through the Win32 API directly
(SetCursorPos / mouse_event via ctypes) on a background thread. This
removes pyautogui's Python overhead entirely and guarantees the mouse
path can never block the webcam/MediaPipe loop.

Requirements (install with pip):
    pip install opencv-python mediapipe numpy
"""

import ctypes
import math
import threading
import time

import cv2
import mediapipe as mp
import numpy as np

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

CAM_INDEX = 0                  # webcam device index
FRAME_WIDTH = 640              # capture / display width
FRAME_HEIGHT = 480

# Resolution actually fed to MediaPipe. Feeding the model a smaller image
# cuts the resize/crop preprocessing and memory traffic in `hands.process`.
# The models' internal NN input size is fixed, so landmark *accuracy* is not
# hurt; all gesture math below works in normalized coordinates and is
# re-scaled to REF_WIDTH/REF_HEIGHT for tuning.
PROCESS_WIDTH = 320
PROCESS_HEIGHT = 240

# Reference resolution the pixel-based thresholds (CLICK_THRESHOLD, margin,
# scroll sensitivity) are tuned for. MediaPipe returns normalized landmarks
# (0..1), so we measure distances in this space and the feel is identical to
# the old 640x480 code regardless of the MediaPipe input resolution.
REF_WIDTH = FRAME_WIDTH
REF_HEIGHT = FRAME_HEIGHT

# Region of the camera frame mapped to the full screen (in pixels, at the
# reference resolution). Keeping a margin makes it easier to reach screen
# corners without needing to move your hand out of frame.
FRAME_MARGIN = 100

# Pinch distance (in pixels, at the reference resolution) below which we
# consider two fingertips to be "touching".
CLICK_THRESHOLD = 35

# Fingers must be at least this far apart before the pinch "releases".
# The gap between CLICK_THRESHOLD and this value is hysteresis, which
# prevents clicks from flickering on/off when you hover near the edge.
PINCH_RELEASE_THRESHOLD = 48

# Smoothing factor for cursor movement (0 = no smoothing, 1 = frozen).
# Higher values reduce jitter but add lag.
SMOOTHING = 0.6

# Minimum time between repeated clicks, to avoid accidental double clicks.
CLICK_COOLDOWN = 0.4

# Scroll sensitivity (pixels of hand movement per scroll "tick").
SCROLL_SENSITIVITY = 4

# Run MediaPipe on every Nth frame only. On the skipped frames the last
# detected landmarks are reused, so the cursor still updates every frame
# while the neural network runs far less often.
#   1 = track every frame (most accurate, heaviest)
#   2 = every other frame (good balance) <-- default
#   3-4 = lighter still, slightly more lag
TRACK_EVERY_N_FRAMES = 2

# Skeleton overlay style: small red dots on all 21 landmarks, connected
# by thin white lines following the MediaPipe hand topology.
LANDMARK_RADIUS = 5
LANDMARK_COLOR = (0, 0, 255)        # BGR red
CONNECTION_THICKNESS = 1
CONNECTION_COLOR = (255, 255, 255)  # BGR white

# Print a per-stage timing breakdown to the console every N frames.
PROFILE_EVERY_N_FRAMES = 60

# MediaPipe model complexity: 0 = lite (fastest, ~3x faster than full),
# 1 = full, 2 = heavy.
MODEL_COMPLEXITY = 0

# ----------------------------------------------------------------------
# Win32 mouse API (direct ctypes, no pyautogui)
# ----------------------------------------------------------------------


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32 = ctypes.windll.user32
user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [
    ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
]

SCREEN_W = user32.GetSystemMetrics(0)  # SM_CXSCREEN
SCREEN_H = user32.GetSystemMetrics(1)  # SM_CYSCREEN

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


def get_cursor_pos():
    p = _POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def set_cursor_pos(x, y):
    user32.SetCursorPos(int(x), int(y))


def mouse_event(flags, dx=0, dy=0, data=0):
    user32.mouse_event(flags, dx, dy, data, 0)


# ----------------------------------------------------------------------
# MediaPipe setup
# ----------------------------------------------------------------------

mp_hands = mp.solutions.hands

hands = mp_hands.Hands(
    static_image_mode=False,
    max_num_hands=1,
    model_complexity=MODEL_COMPLEXITY,
    min_detection_confidence=0.5,
    # Lower tracking threshold keeps MediaPipe in "track" mode longer so it
    # does NOT fall back to the expensive full-frame palm detector every time
    # your hand moves (which is constantly while steering the mouse).
    min_tracking_confidence=0.5,
)

# Landmark indices we care about (MediaPipe Hands convention)
THUMB_TIP = 4
INDEX_TIP = 8
MIDDLE_TIP = 12
INDEX_PIP = 6   # index finger's middle knuckle, used to check "raised"
MIDDLE_PIP = 10
RING_TIP = 16
RING_PIP = 14
PINKY_TIP = 20
PINKY_PIP = 18


def dist_norm(a, b):
    """Euclidean distance between two normalized landmarks, measured in
    pixels at the reference resolution so the thresholds behave exactly
    as they did at 640x480, whatever resolution MediaPipe actually sees."""
    return math.hypot(
        (a[0] - b[0]) * REF_WIDTH,
        (a[1] - b[1]) * REF_HEIGHT,
    )


def get_landmarks_normalized(hand_landmarks):
    """Return a list of 21 (x, y) landmarks in normalized [0, 1] coords."""
    return [(lm.x, lm.y) for lm in hand_landmarks.landmark]


class PinchTracker:
    """Tracks a pinch with hysteresis so the state doesn't flicker
    when the fingertips hover right at the threshold."""

    def __init__(self, press_threshold, release_threshold):
        self.press_threshold = press_threshold
        self.release_threshold = release_threshold
        self.active = False

    def update(self, dist):
        if self.active:
            if dist > self.release_threshold:
                self.active = False
        elif dist < self.press_threshold:
            self.active = True
        return self.active


class MouseController:
    """Direct Win32 mouse control.

    Continuous cursor movement runs on a background thread: the camera loop
    only stores a smoothed target position and wakes the thread, so even if
    a Win32 call ever stalls (OS scheduling, slow window manager), the
    webcam/MediaPipe loop keeps running at full speed. Clicks, drags and
    scroll are occasional single `mouse_event` calls made directly.
    """

    MIN_CURSOR_INTERVAL = 0.004  # safety cap: ~250 SetCursorPos calls/sec

    def __init__(self):
        self.prev_x, self.prev_y = get_cursor_pos()
        self.last_click_time = 0.0
        self.dragging = False
        self.prev_scroll_y = None
        self.auto_ms = 0.0  # cumulative time inside Win32 mouse calls

        self._target = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._cursor_loop, daemon=True)
        self._thread.start()

    # -- background cursor thread --------------------------------------

    def _cursor_loop(self):
        last = None
        last_time = 0.0
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                target = self._target
            now = time.perf_counter()
            if (target is not None and target != last
                    and now - last_time >= self.MIN_CURSOR_INTERVAL):
                set_cursor_pos(*target)
                self.auto_ms += (time.perf_counter() - now) * 1000
                last = target
                last_time = time.perf_counter()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=1.0)

    # -- movement -------------------------------------------------------

    def move(self, norm_x, norm_y):
        """Map a normalized fingertip position to the screen and hand it to
        the cursor thread. Smoothing is done here, exactly as before."""
        mx = FRAME_MARGIN / REF_WIDTH
        my = FRAME_MARGIN / REF_HEIGHT
        x = np.interp(norm_x, [mx, 1.0 - mx], [0, SCREEN_W])
        y = np.interp(norm_y, [my, 1.0 - my], [0, SCREEN_H])

        # Exponential smoothing to reduce jitter. SMOOTHING is the fraction
        # of the previous position we keep: 0 = instant, 1 = frozen.
        alpha = 1 - SMOOTHING
        curr_x = self.prev_x + (x - self.prev_x) * alpha
        curr_y = self.prev_y + (y - self.prev_y) * alpha

        curr_x = min(max(curr_x, 0), SCREEN_W - 1)
        curr_y = min(max(curr_y, 0), SCREEN_H - 1)

        # Only hand a new target to the cursor thread when the smoothed
        # position actually moved (avoids redundant cursor events).
        if abs(curr_x - self.prev_x) >= 0.5 or abs(curr_y - self.prev_y) >= 0.5:
            with self._lock:
                self._target = (curr_x, curr_y)
            self._wake.set()

        self.prev_x, self.prev_y = curr_x, curr_y

    # -- clicks / drag / scroll (single mouse_event calls, rare) --------

    def try_click(self, kind):
        now = time.time()
        if now - self.last_click_time < CLICK_COOLDOWN:
            return
        self.last_click_time = now
        t0 = time.perf_counter()
        if kind == "left":
            mouse_event(MOUSEEVENTF_LEFTDOWN)
            mouse_event(MOUSEEVENTF_LEFTUP)
        elif kind == "right":
            mouse_event(MOUSEEVENTF_RIGHTDOWN)
            mouse_event(MOUSEEVENTF_RIGHTUP)
        self.auto_ms += (time.perf_counter() - t0) * 1000

    def start_drag(self):
        if not self.dragging:
            t0 = time.perf_counter()
            mouse_event(MOUSEEVENTF_LEFTDOWN)
            self.auto_ms += (time.perf_counter() - t0) * 1000
            self.dragging = True

    def stop_drag(self):
        if self.dragging:
            t0 = time.perf_counter()
            mouse_event(MOUSEEVENTF_LEFTUP)
            self.auto_ms += (time.perf_counter() - t0) * 1000
            self.dragging = False

    def scroll(self, norm_y):
        cam_y = norm_y * REF_HEIGHT
        if self.prev_scroll_y is not None:
            delta = self.prev_scroll_y - cam_y
            if abs(delta) > 2:
                clicks = int(delta * SCROLL_SENSITIVITY)
                if clicks:
                    # One wheel event with the total delta (each notch = 120)
                    # scrolls the same total amount as pyautogui's loop of
                    # single-notch events, with far fewer calls.
                    t0 = time.perf_counter()
                    mouse_event(MOUSEEVENTF_WHEEL, 0, 0, clicks * WHEEL_DELTA)
                    self.auto_ms += (time.perf_counter() - t0) * 1000
        self.prev_scroll_y = cam_y

    def reset_scroll(self):
        self.prev_scroll_y = None


def fingers_up(landmarks):
    """Return True if index+middle are extended while ring+pinky are folded
    (used to detect the 'scroll' gesture). `landmarks` is the list of
    normalized (x, y) pairs."""
    index_up = landmarks[INDEX_TIP][1] < landmarks[INDEX_PIP][1]
    middle_up = landmarks[MIDDLE_TIP][1] < landmarks[MIDDLE_PIP][1]
    ring_up = landmarks[RING_TIP][1] < landmarks[RING_PIP][1]
    pinky_up = landmarks[PINKY_TIP][1] < landmarks[PINKY_PIP][1]
    return index_up and middle_up and not ring_up and not pinky_up


def draw_hand_overlay(frame, lm_norm, w, h):
    """Draw a clean skeletal overlay: thin white lines following the
    MediaPipe hand topology plus a small dot on each of the 21 landmarks.
    All landmarks are red except the index fingertip (landmark 8, green)
    and the thumb fingertip (landmark 4, pink/magenta). Normalized
    coordinates are scaled to the display frame (which may be a different
    resolution than the MediaPipe input)."""
    pts = [(int(x * w), int(y * h)) for x, y in lm_norm]

    for a, b in mp_hands.HAND_CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], CONNECTION_COLOR, CONNECTION_THICKNESS)
    for i, pt in enumerate(pts):
        if i == INDEX_TIP:
            color = (0, 255, 0)        # green (BGR)
        elif i == THUMB_TIP:
            color = (255, 0, 255)      # pink/magenta (BGR)
        else:
            color = LANDMARK_COLOR     # red (BGR)
        cv2.circle(frame, pt, LANDMARK_RADIUS, color, cv2.FILLED)


def main():
    cap = cv2.VideoCapture(CAM_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    if not cap.isOpened():
        print("Could not open webcam. Check CAM_INDEX or camera permissions.")
        return

    controller = MouseController()
    left_pinch = PinchTracker(CLICK_THRESHOLD, PINCH_RELEASE_THRESHOLD)

    # Cached hand data, reused on the frames where we skip MediaPipe.
    cached_norm = None  # last detected normalized landmarks (21 (x,y) pairs)

    # Per-stage profiling timers (ms per frame).
    stats = {"proc_ms": 0.0, "cv_ms": 0.0, "gest_ms": 0.0,
             "auto_ms": 0.0, "track_n": 0, "n": 0}
    prev_time = time.perf_counter()
    frame_idx = 0

    print("Hand Mouse running. Press 'q' in the video window to quit.")
    print(f"MediaPipe: model_complexity={MODEL_COMPLEXITY}, "
          f"input={PROCESS_WIDTH}x{PROCESS_HEIGHT}, "
          f"every {TRACK_EVERY_N_FRAMES} frame(s)")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("Failed to read frame from webcam.")
            break

        frame = cv2.flip(frame, 1)  # mirror for natural movement
        h, w, _ = frame.shape

        # ------------------------------------------------------------
        # 1) MediaPipe inference (every Nth frame, at reduced resolution)
        # ------------------------------------------------------------
        if frame_idx % TRACK_EVERY_N_FRAMES == 0:
            t0 = time.perf_counter()
            if (PROCESS_WIDTH, PROCESS_HEIGHT) != (w, h):
                small = cv2.resize(frame, (PROCESS_WIDTH, PROCESS_HEIGHT))
            else:
                small = frame
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            result = hands.process(rgb)
            if result.multi_hand_landmarks:
                hand_raw = result.multi_hand_landmarks[0]
                cached_norm = get_landmarks_normalized(hand_raw)
            else:
                cached_norm = None
            stats["proc_ms"] += (time.perf_counter() - t0) * 1000
            stats["track_n"] += 1

        lm = cached_norm

        # ------------------------------------------------------------
        # 2) Gesture recognition (pure math, microseconds)
        # ------------------------------------------------------------
        t1 = time.perf_counter()
        label = ""
        left_pinched = False
        scroll_mode = False
        right_pinch_dist = float("inf")
        if lm is not None:
            index_pt = lm[INDEX_TIP]
            thumb_pt = lm[THUMB_TIP]
            middle_pt = lm[MIDDLE_TIP]

            left_pinch_dist = dist_norm(thumb_pt, index_pt)
            right_pinch_dist = dist_norm(thumb_pt, middle_pt)
            left_pinched = left_pinch.update(left_pinch_dist)

            scroll_mode = (
                fingers_up(lm) and left_pinch_dist > CLICK_THRESHOLD * 1.5
            )
        stats["gest_ms"] += (time.perf_counter() - t1) * 1000

        # ------------------------------------------------------------
        # 3) Mouse control (Win32, non-blocking)
        # ------------------------------------------------------------
        t2 = time.perf_counter()
        auto_before = controller.auto_ms
        if lm is not None:
            if scroll_mode:
                controller.scroll(lm[INDEX_TIP][1])
                label = "SCROLL"
            else:
                controller.reset_scroll()
                controller.move(lm[INDEX_TIP][0], lm[INDEX_TIP][1])

                if left_pinched:
                    controller.start_drag()
                    label = "LEFT/DRAG"
                else:
                    controller.stop_drag()

                if right_pinch_dist < CLICK_THRESHOLD and not controller.dragging:
                    controller.try_click("right")
                    label = "RIGHT CLICK"
        else:
            controller.stop_drag()
            controller.reset_scroll()
        stats["auto_ms"] += controller.auto_ms - auto_before

        # ------------------------------------------------------------
        # 4) Drawing (OpenCV)
        # ------------------------------------------------------------
        t3 = time.perf_counter()
        cv2.rectangle(
            frame,
            (FRAME_MARGIN, FRAME_MARGIN),
            (w - FRAME_MARGIN, h - FRAME_MARGIN),
            (255, 0, 0),
            2,
        )

        if lm is not None:
            draw_hand_overlay(frame, lm, w, h)

        if label:
            color = {
                "SCROLL": (0, 255, 255),
                "LEFT/DRAG": (0, 255, 0),
                "RIGHT CLICK": (0, 0, 255),
            }.get(label, (255, 255, 255))
            cv2.putText(frame, label, (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)

        curr_time = time.perf_counter()
        fps = 1 / (curr_time - prev_time) if prev_time else 0
        prev_time = curr_time
        cv2.putText(frame, f"FPS: {int(fps)}", (w - 120, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
        if stats["n"] >= 1:
            ap = stats["proc_ms"] / max(stats["track_n"], 1)
            ac = stats["cv_ms"] / stats["n"]
            ag = stats["gest_ms"] / stats["n"]
            aa = stats["auto_ms"] / stats["n"]
            cv2.putText(
                frame,
                f"MP:{ap:.0f} CV:{ac:.0f} GT:{ag:.1f} MO:{aa:.2f}",
                (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (200, 200, 200), 1,
            )

        cv2.imshow("Hand Mouse (press q to quit)", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

        # ------------------------------------------------------------
        # 5) Periodic console report (the bottleneck breakdown).
        # ------------------------------------------------------------
        stats["cv_ms"] += (time.perf_counter() - t3) * 1000
        stats["n"] += 1
        if stats["n"] == PROFILE_EVERY_N_FRAMES:
            ap = stats["proc_ms"] / max(stats["track_n"], 1)
            ac = stats["cv_ms"] / stats["n"]
            ag = stats["gest_ms"] / stats["n"]
            aa = stats["auto_ms"] / stats["n"]
            print(
                f"per frame avg (ms): MediaPipe={ap:.1f}  "
                f"OpenCV/draw={ac:.1f}  gesture={ag:.2f}  "
                f"mouse(Win32)={aa:.2f}  loop FPS={fps:.0f}"
            )
            stats = {"proc_ms": 0.0, "cv_ms": 0.0, "gest_ms": 0.0,
                     "auto_ms": 0.0, "track_n": 0, "n": 0}

        frame_idx += 1

    controller.shutdown()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
