@echo off
rem Double-click to start the fact-check tool. Keys are read from .env in this folder.
rem Closing this window stops the tool.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python server.py
if errorlevel 1 pause
