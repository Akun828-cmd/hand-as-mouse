@echo off
setlocal
cd /d "%~dp0"

rem Locate Inno Setup 6's compiler (machine-wide or per-user install).
set "ISCC="
for %%p in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if exist %%p set "ISCC=%%~p"
)

echo === 1/6 Create/use venv ===
if not exist "venv\Scripts\python.exe" (
    py -3.12 -m venv venv || goto :error
)
call "venv\Scripts\activate.bat"

echo === 2/6 Install dependencies + PyInstaller ===
python -m pip install --upgrade pip || goto :error
pip install -r requirements.txt pyinstaller || goto :error

echo === 3/6 Clean previous builds ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo === 4/6 Build with PyInstaller (onedir) ===
pyinstaller HandMouse.spec --noconfirm || goto :error
echo.
echo Built: dist\HandMouse\HandMouse.exe
echo Test it now, then re-run this script to build the installer.

if not defined ISCC (
    echo === 5/6 Inno Setup NOT FOUND ===
    echo Install it, then re-run this script:
    echo     winget install -e --id JRSoftware.InnoSetup
    goto :end
)

echo === 5/6 Compile installer with Inno Setup ===
"%ISCC%" "installer\HandMouse.iss" || goto :error

echo.
echo DONE: installer\HandMouseSetup.exe
goto :end

:error
echo BUILD FAILED.
exit /b 1

:end
endlocal