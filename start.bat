@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo   MOLAB GPU CLOUD VPS ^& TELEGRAM BOT - KHOI DONG SERVER
echo ========================================================

echo [1/3] Dang don dep tien trinh cu (neu co)...
taskkill /F /IM pythonw.exe >nul 2>&1
taskkill /F /IM chrome-headless-shell.exe >nul 2>&1
ping 127.0.0.1 -n 3 >nul

del /F /Q "profile\Singleton*" >nul 2>&1
for /d %%D in ("profiles\*") do del /F /Q "%%D\Singleton*" >nul 2>&1

echo [2/3] Dang bat ngam Bot Telegram ^& Molab Keepalive (24/7)...
start "" pythonw.exe keepalive.py

echo [3/3] Da khoi dong thanh cong! Bot dang ket noi Molab...
echo       Xem log tai: keepalive.log
echo       Thong tin SSH: last_ssh.txt
echo ========================================================
ping 127.0.0.1 -n 4 >nul
