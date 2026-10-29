@echo off
cd /d "%~dp0"
chcp 65001 >nul
title AI Free Gateway - Cap Nhat Phien Ban Moi

echo ==========================================================
echo       CAP NHAT PHIEN BAN MOI (AI FREE GATEWAY)
echo ==========================================================
echo.

REM 1. Kiem tra Git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [LOI] Khong tim thay Git tren may tinh!
    echo Vui long cai dat Git tai: https://git-scm.com/
    pause
    exit /b 1
)

REM 2. Tam dung Gateway neu dang chay de tranh lock file
echo [*] Dang kiem tra va tam dung tien trinh Gateway cu...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
    "if ($conn) { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue }"
if exist ".gateway.pid" (
    del /f /q ".gateway.pid" >nul 2>&1
)

REM 3. Keo ma nguon moi nhat tu GitHub
echo [*] Dang tai code moi nhat tu GitHub (git pull)...
git pull origin main
if %errorlevel% neq 0 (
    echo.
    echo [CANH BAO] Khong the tai ban cap nhat tu GitHub!
    echo Vui long kiem tra ket noi Internet hoac xung dot code.
    pause
    exit /b 1
)

REM 4. Cap nhat thu vien neu requirements co thay doi
echo [*] Dang kiem tra va dong bo thu vien...
if exist ".venv\Scripts\python.exe" (
    .\.venv\Scripts\python -m pip install -r requirements.txt --quiet
)

echo.
echo ==========================================================
echo  [THANH CONG] Da cap nhat Gateway len phien ban moi nhat!
echo ==========================================================
echo.
echo [*] Dang khoi dong lai Gateway voi code moi...
start "" "start.bat"

