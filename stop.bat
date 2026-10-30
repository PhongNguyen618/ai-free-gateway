@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Dung AI Free Gateway

echo ==========================================================
echo        DUNG TIEN TRINH AI FREE GATEWAY
echo ==========================================================
echo.

if not exist ".gateway.pid" (
    echo [*] Khong tim thay file .gateway.pid. Gateway co the chua duoc bat hoac da dung.
    pause
    exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$pidFile = '.gateway.pid';" ^
    "if (Test-Path $pidFile) {" ^
    "    $gwPid = (Get-Content $pidFile).Trim();" ^
    "    if ($gwPid) {" ^
    "        $proc = Get-CimInstance Win32_Process -Filter \"ProcessId = $gwPid\" -ErrorAction SilentlyContinue;" ^
    "        if ($proc -and ($proc.CommandLine -match 'ai-free-gateway' -or $proc.CommandLine -match 'src\.main')) {" ^
    "            Stop-Process -Id $gwPid -Force -ErrorAction SilentlyContinue;" ^
    "            Remove-Item $pidFile -Force -ErrorAction SilentlyContinue;" ^
    "            Write-Host \"[OK] Da dung an toan tien trinh AI Free Gateway (PID: $gwPid)\";" ^
    "        } else {" ^
    "            Write-Host \"[*] Tien trinh PID $gwPid khong ton tai hoac khong phai AI Free Gateway. Xoa file PID cu.\";" ^
    "            Remove-Item $pidFile -Force -ErrorAction SilentlyContinue;" ^
    "        }" ^
    "    }" ^
    "}"

echo.
echo [OK] Hoan tat!
pause
