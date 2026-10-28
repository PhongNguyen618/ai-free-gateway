@echo off
chcp 65001 >nul
title Dung AI Free Gateway

echo ==========================================================
echo        DUNG TIEN TRINH AI FREE GATEWAY
echo ==========================================================
echo.

echo [*] Dang tim va tat tien trinh AI Free Gateway (port 8000)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$conns = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue; if ($conns) { $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue; Write-Host \"[OK] Da dung tien trinh dang chay cong 8000 (PID: $p)\" } } else { Write-Host '[*] Khong co tien trinh nao dang chay o cong 8000.' }"

echo.
echo [OK] Hoan tat!
pause
