@echo off
chcp 65001 > nul
title Stock Alert Pro - ระบบติดตามและแจ้งเตือนหุ้น

echo =======================================================================
echo    Stock Alert Pro - ระบบติดตามราคาและแจ้งเตือนหุ้น 1% (Mobile Ready)
echo =======================================================================
echo.
echo กำลังเริ่มต้นระบบและเปิด Cloudflare Tunnel...
echo.
echo 💻 เข้าใช้งานจากคอมพิวเตอร์:   http://localhost:8000
echo 📶 เข้าใช้งานจาก Wi-Fi:        http://192.168.192.118:8000
echo 📱 เข้าใช้งานผ่าน 4G/5G:       ดูลิงก์ HTTPS หรือสแกน QR Code บนหน้าเว็บ
echo.

:: Start browser after 2 seconds in background
start "" cmd /c "timeout /t 2 > nul & start http://localhost:8000"

:: Start Uvicorn Server
.\venv\Scripts\uvicorn.exe backend.app:app --host 0.0.0.0 --port 8000

pause
