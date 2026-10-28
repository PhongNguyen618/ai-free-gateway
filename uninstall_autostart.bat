@echo off
chcp 65001 >nul
title AI Free Gateway - Huy Khoi Dong Cung Windows

echo ==========================================================
echo    HUY KHOI DONG CUNG WINDOWS
echo ==========================================================
echo.

set "SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AIFreeGateway.lnk"

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    echo [OK] Da go bo AI Free Gateway khoi thu muc khoi dong cung Windows!
) else (
    echo [*] AI Free Gateway hien chua duoc cai dat khoi dong cung Windows.
)

echo.
pause
