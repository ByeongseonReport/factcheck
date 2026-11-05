@echo off
rem Double-click to start the fact-check tool. Keys are read from .env in this folder.
rem Closing this window stops the tool.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 goto nopython

python -c "import anthropic" >nul 2>&1
if errorlevel 1 (
    echo Installing required package - first run only...
    python -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Notepad will open. Paste your TAVILY and CLAUDE keys after the = signs,
    echo save, and close Notepad. The tool starts after that.
    notepad ".env"
)

python server.py
if errorlevel 1 goto failed
goto :eof

:nopython
echo Python is not installed. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup. See README.md.
pause
goto :eof

:failed
pause
