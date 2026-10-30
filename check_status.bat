@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Kiem tra trang thai AI Free Gateway

echo ==========================================================
echo        KIEM TRA TRANG THAI AI FREE GATEWAY
echo ==========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
    "if ($listening) {" ^
    "    $pidVal = $listening[0].OwningProcess;" ^
    "    Write-Host \"[OK] Gateway DANG CHAY tren cong 8000 (PID: $pidVal)\" -ForegroundColor Green;" ^
    "    try {" ^
    "        $resp = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/ready' -TimeoutSec 3 -ErrorAction Stop;" ^
    "        Write-Host \"[OK] Trang thai san sang: $($resp.status) (Initialized: $($resp.initialized), Catalog: $($resp.catalog_usable))\" -ForegroundColor Green;" ^
    "    } catch {" ^
    "        Write-Host \"[!] Cong 8000 dang mo nhung /ready chua san sang: $($_.Exception.Message)\" -ForegroundColor Yellow;" ^
    "    }" ^
    "    Write-Host '';" ^
    "    Write-Host '  - Web Dashboard:     http://127.0.0.1:8000/' -ForegroundColor Cyan;" ^
    "    Write-Host '  - OpenAI Base URL:   http://127.0.0.1:8000/v1' -ForegroundColor Cyan;" ^
    "    Write-Host '  - Swagger API Docs:  http://127.0.0.1:8000/docs' -ForegroundColor Cyan;" ^
    "} else {" ^
    "    Write-Host '[X] Gateway HIEN DANG KHONG CHAY (Cong 8000 chua duoc mo).' -ForegroundColor Red;" ^
    "    Write-Host '    Ban co the bat bang: start.bat (xem log) hoac start_background.vbs (chay ngam).' -ForegroundColor Yellow;" ^
    "}"

echo.
pause

