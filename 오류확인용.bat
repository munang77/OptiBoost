@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "PCOptimizer.pyw"
echo.
echo (창이 바로 닫히면 위 메시지를 확인하세요)
pause
