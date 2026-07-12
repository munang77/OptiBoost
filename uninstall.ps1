$ErrorActionPreference = 'SilentlyContinue'
$inst = Join-Path $env:LOCALAPPDATA 'Programs\OptiBoost'

# 실행 중이면 종료
Get-Process OptiBoost -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 400

# 예약 자동청소 제거
schtasks /delete /tn PCOptimizer_WeeklyClean /f 2>$null | Out-Null

# 바로가기 제거
Remove-Item (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\OptiBoost.lnk') -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'OptiBoost.lnk') -Force

# 프로그램 추가/제거 등록 해제
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OptiBoost' -Recurse -Force

# 설치 폴더는 실행 중 잠길 수 있어, 분리된 프로세스로 잠시 후 삭제
Start-Process cmd -ArgumentList '/c', ('ping 127.0.0.1 -n 2 >nul & rmdir /s /q "' + $inst + '"') -WindowStyle Hidden

try {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show('OptiBoost가 제거되었습니다.', '제거 완료') | Out-Null
} catch {}
