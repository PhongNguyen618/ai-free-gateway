@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Dung AI Free Gateway

echo ==========================================================
echo        DUNG TIEN TRINH AI FREE GATEWAY
echo ==========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$gwStopped = $false;" ^
    "$pidFile = '.gateway.pid';" ^
    "if (Test-Path $pidFile) {" ^
    "    $filePid = (Get-Content $pidFile -ErrorAction SilentlyContinue).Trim();" ^
    "    if ($filePid) {" ^
    "        $proc = Get-CimInstance Win32_Process -Filter \"ProcessId = $filePid\" -ErrorAction SilentlyContinue;" ^
    "        if ($proc -and ($proc.CommandLine -match 'ai-free-gateway' -or $proc.CommandLine -match 'src\.main')) {" ^
    "            Stop-Process -Id $filePid -Force -ErrorAction SilentlyContinue;" ^
    "            Write-Host \"[OK] Da dung tien trinh AI Free Gateway tu PID file (PID: $filePid)\" -ForegroundColor Green;" ^
    "            $gwStopped = $true;" ^
    "        }" ^
    "    }" ^
    "    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue;" ^
    "};" ^
    "$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
    "if ($listening) {" ^
    "    foreach ($conn in $listening) {" ^
    "        $targetPid = $conn.OwningProcess;" ^
    "        if ($targetPid -gt 0) {" ^
    "            $proc = Get-CimInstance Win32_Process -Filter \"ProcessId = $targetPid\" -ErrorAction SilentlyContinue;" ^
    "            if ($proc -and ($proc.CommandLine -match 'ai-free-gateway' -or $proc.CommandLine -match 'src\.main')) {" ^
    "                Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue;" ^
    "                Write-Host \"[OK] Da giai phong cong 8000 va dung tien trinh Gateway (PID: $targetPid)\" -ForegroundColor Green;" ^
    "                $gwStopped = $true;" ^
    "            }" ^
    "        }" ^
    "    }" ^
    "};" ^
    "Remove-Item '.gateway.lock' -Force -ErrorAction SilentlyContinue;" ^
    "Remove-Item '.gateway.instance.json' -Force -ErrorAction SilentlyContinue;" ^
    "if (-not $gwStopped) {" ^
    "    Write-Host '[*] Gateway da dung hoac khong co tien trinh nao chay tren cong 8000.' -ForegroundColor Yellow;" ^
    "} else {" ^
    "    Write-Host '[OK] Da tat gateway va giai phong cong 8000 thanh cong!' -ForegroundColor Green;" ^
    "}"

echo.
pause
