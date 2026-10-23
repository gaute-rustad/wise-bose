@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%~dp0bot.py"
pause
