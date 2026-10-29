@echo off
cd /d "%~dp0"
chcp 65001 >nul
title AI Free Gateway - Zero-Trust Local Proxy

echo ==========================================================
echo        AI FREE GATEWAY - LOCAL ZERO-TRUST PROXY
echo ==========================================================
echo.

REM 1. Kiem tra Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [LOI] Khong tim thay Python tren he thong!
    echo Vui long cai dat Python tu 3.11 tro len va tick vao "Add Python to PATH".
    echo Tai Python tai: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 2. Kiem tra va tao thu muc .venv neu chua co
if not exist ".venv\Scripts\python.exe" (
    echo [*] Dang khoi tao moi truong ao .venv...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [LOI] Khong the tao moi truong ao .venv!
        pause
        exit /b 1
    )
    echo [OK] Da tao thanh cong thu muc .venv!
)

REM 3. Kiem tra cac thu vien can thiet
echo [*] Dang kiem tra cac thu vien can thiet...
.\.venv\Scripts\python -c "import fastapi, uvicorn, httpx, aiosqlite, yaml, pydantic, pydantic_settings, dotenv" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Phat hien thieu thu vien, dang tu dong tai ve .venv tu requirements.txt...
    .\.venv\Scripts\python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [LOI] Cai dat thu vien that bai! Vui long kiem tra ket noi mang.
        pause
        exit /b 1
    )
    echo [OK] Da cai dat xong tat ca thu vien can thiet!
) else (
    echo [OK] Cac thu vien da day du san sang!
)

REM 4. Kiem tra file config
if not exist "config\.env" (
    if exist "config\.env.example" (
        echo [*] Dang tao file config\.env tu template .env.example...
        copy "config\.env.example" "config\.env" >nul
        echo [OK] Da tao file config\.env!
    )
)

REM 4.5. Kiem tra xem cong 8000 da co tien trinh chay chua
netstat -ano | findstr :8000 | findstr LISTENING >nul 2>&1
if %errorlevel% equ 0 (
    echo.
    echo ==========================================================
    echo  [THONG BAO] Gateway DA DANG CHAY tren cong 8000!
    echo  Ban khong can mo them cua so moi.
    echo  Hay mo trinh duyet tai: http://127.0.0.1:8000/
    echo ==========================================================
    echo.
    pause
    exit /b 0
)

REM 5. Khoi dong server
echo.
echo ==========================================================
echo  [SUCCESS] Server dang khoi dong tai:
echo  - Web Dashboard:     http://127.0.0.1:8000/
echo  - OpenAI Base URL:   http://127.0.0.1:8000/v1
echo  - Swagger API Docs:  http://127.0.0.1:8000/docs
echo ==========================================================
echo  Nhan Ctrl+C de dung server.
echo.

.\.venv\Scripts\python -m src.main

if %errorlevel% neq 0 (
    echo.
    echo [THONG BAO] Server da dung.
    pause
)
