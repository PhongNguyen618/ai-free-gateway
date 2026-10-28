@echo off
chcp 65001 >nul
title AI Free Gateway - Cai Dat Khoi Dong Cung Windows

echo ==========================================================
echo    CAI DAT KHOI DONG CUNG WINDOWS (AUTOSTART)
echo ==========================================================
echo.

set "SCRIPT_DIR=%~dp0"
set "TARGET_VBS=%SCRIPT_DIR%start_background.vbs"
set "SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AIFreeGateway.lnk"

echo [*] Dang tao loi tat khoi dong ngam trong thu muc Startup...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = 'wscript.exe'; $s.Arguments = '\"%TARGET_VBS%\"'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Description = 'AI Free Gateway Local Proxy'; $s.Save()"

if exist "%SHORTCUT_PATH%" (
    echo [OK] DA CAI DAT THANH CONG!
    echo.
    echo Tu bay gio moi khi mo may tinh Windows:
    echo - AI Free Gateway se tu dong chay ngam (khong hien cua so CMD).
    echo - Cline / Cursor / IDE co the goi API tai http://127.0.0.1:8000/v1 ngay lap tuc.
    echo - Dashboard quan ly luon san sang tai: http://127.0.0.1:8000/
    echo.
    echo Neu muon tat chay ngam, hay chay file "stop.bat".
    echo Neu muon huy khoi dong cung Windows, chi can chay file "uninstall_autostart.bat".
) else (
    echo [LOI] Khong the tao shortcut khoi dong tu dong.
)

echo.
pause
