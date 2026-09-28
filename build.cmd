@echo off
rem Builds Re-Search.exe into dist\ next to this file. Double-click to run.
cd /d "%~dp0"
echo Using:
python -c "import sys; print('Python', sys.version.split()[0])"
echo.
python -m pip install --quiet --upgrade pyinstaller pywebview
if errorlevel 1 (
  echo Could not install PyInstaller / pywebview - see the messages above.
  pause
  exit /b 1
)
python -m PyInstaller --onefile --windowed --icon re-search.ico --collect-all webview --name Re-Search --clean --noconfirm re_search.py
echo.
if exist dist\Re-Search.exe (
  echo Built: %~dp0dist\Re-Search.exe
) else (
  echo Build failed - see the messages above.
)
pause
