@echo off
rem ---------------------------------------------------------------------------
rem  Builds "STL Library.exe" and the install wizard (STL-Library-Setup.exe).
rem  Put this file, stl_library.pyw, app.ico and STL_Library.iss in one folder,
rem  then double-click this file.
rem  Needs: Python (with the "py" launcher) and, for the wizard, Inno Setup 6.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

echo.
echo [1/3] Installing build tools...
py -m pip install --upgrade pyinstaller pillow numpy || goto :fail

echo.
echo [2/3] Building the app (this takes a minute or two)...
py -m PyInstaller --noconfirm --clean --onedir --windowed --name "STL Library" --icon app.ico stl_library.pyw || goto :fail

echo.
echo [3/3] Building the install wizard...
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%P set "ISCC=%%~P"

if not defined ISCC (
  echo.
  echo The app was built: dist\STL Library\STL Library.exe
  echo To also create the install wizard, install Inno Setup 6 from
  echo   https://jrsoftware.org/isdl.php
  echo and run this file again.
  pause
  exit /b 0
)

"%ISCC%" STL_Library.iss || goto :fail

echo.
echo Done. Your installer is: installer\STL-Library-Setup.exe
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
