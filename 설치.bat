@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo OptiBoost를 설치합니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$SRC='%~dp0'; $c=Get-Content -Raw -Encoding UTF8 ($SRC+'install.ps1'); Invoke-Expression $c"
echo.
pause
