# Hand Gesture Mouse Controller

Control your computer's mouse using hand gestures tracked by your webcam,
powered by [MediaPipe](https://developers.google.com/mediapipe) hand
tracking and [PyAutoGUI](https://pyautogui.readthedocs.io/) for OS-level
mouse control.

## Setup

1. Make sure you have Python 3.8–3.11 installed (MediaPipe doesn't yet
   support every latest Python version — check their docs if `pip install`
   fails).
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run it:

   ```bash
   python hand_mouse.py
   ```

4. Grant camera permission if your OS prompts for it. On macOS you may
   also need to grant "Accessibility" permission to your terminal/IDE so
   PyAutoGUI can control the mouse (System Settings → Privacy & Security →
   Accessibility).

## Gestures

| Gesture | Action |
|---|---|
| Move index fingertip | Move cursor |
| Pinch thumb + index | Left click (hold and move = drag) |
| Pinch thumb + middle | Right click |
| Raise index + middle finger ("peace sign") and move up/down | Scroll |

Press **q** in the camera window to quit.

## Tuning

All the constants that affect feel are at the top of `hand_mouse.py`:

- `CLICK_THRESHOLD` — how close fingertips must get to register a pinch/click.
  Lower it if clicks trigger too easily, raise it if pinches aren't detected.
- `PINCH_RELEASE_THRESHOLD` — how far apart fingertips must be before the pinch
  releases. The gap between it and `CLICK_THRESHOLD` is hysteresis that stops
  clicks flickering when you hover near the threshold.
- `SMOOTHING` — higher values smooth out jittery cursor movement but add lag.
- `FRAME_MARGIN` — the "active zone" inside the camera view that maps to
  your full screen. A bigger margin means you don't have to reach the edges
  of the frame to hit screen edges.
- `SCROLL_SENSITIVITY` — how fast scrolling responds to hand movement.

## How it works

1. OpenCV grabs frames from the webcam.
2. MediaPipe Hands detects 21 hand landmarks per frame.
3. The index fingertip position is mapped from camera coordinates to
   screen coordinates (with smoothing) and used to move the cursor via
   PyAutoGUI.
4. Distances between specific fingertip landmarks (thumb–index,
   thumb–middle) are used to detect pinch gestures for clicking/dragging.
5. An index+middle "peace sign" (with ring + pinky folded) with vertical hand
   movement is interpreted as a scroll gesture.

## Troubleshooting

- **No hand detected**: make sure there's good lighting and your hand is
  fully in frame.
- **Cursor is jittery**: increase `SMOOTHING`, or improve lighting (poor
  tracking causes landmark jitter).
- **Clicks trigger too often**: increase `CLICK_THRESHOLD` down (stricter)
  or increase `CLICK_COOLDOWN`.
- **`pyautogui` can't move the mouse on macOS**: grant Accessibility
  permissions as noted above.
