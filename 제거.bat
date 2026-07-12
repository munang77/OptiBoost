@echo off
chcp 65001 >nul
echo OptiBoost를 제거합니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path $env:LOCALAPPDATA 'Programs\OptiBoost\uninstall.ps1'; if(Test-Path $p){ $c=Get-Content -Raw -Encoding UTF8 $p; Invoke-Expression $c } else { Write-Host '설치되어 있지 않습니다.' }"
echo.
pause
