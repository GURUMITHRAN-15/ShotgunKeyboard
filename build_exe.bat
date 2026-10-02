@echo off
REM Builds dist\ShotgunKeyboard.exe (run from the project folder, inside your virtual environment)
python generate_sound.py || goto :fail
pyinstaller --noconfirm --clean --onefile --windowed --name ShotgunKeyboard --icon=NONE ^
  --add-data "sounds;sounds" ^
  --hidden-import pystray._win32 --hidden-import keyboard._winkeyboard --hidden-import keyboard._winmouse ^
  --collect-all sounddevice --collect-all _sounddevice_data ^
  shotgunkeyboard.py || goto :fail
echo.
echo Done: dist\ShotgunKeyboard.exe
exit /b 0
:fail
echo Build failed - see messages above.
exit /b 1
