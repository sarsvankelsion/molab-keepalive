@echo off
chcp 65001 >nul
cd /d "%~dp0"
python keepalive.py >> keepalive.out.log 2>&1
