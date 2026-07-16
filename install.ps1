$ErrorActionPreference = 'Stop'
if (-not $SRC) { $SRC = (Split-Path -Parent $MyInvocation.MyCommand.Path) + '\' }
$inst = Join-Path $env:LOCALAPPDATA 'Programs\OptiBoost'

Write-Host '=== OptiBoost 설치 ==='

# 기존 실행 종료
Get-Process OptiBoost -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

New-Item -ItemType Directory -Force -Path $inst | Out-Null

# 실행파일 결정: 단독 exe 우선(여러 위치 탐색), 없으면 파이썬 스크립트
$exeSrc = $null
foreach ($cand in @('dist\OptiBoost.exe', 'OptiBoost.exe', 'dist\PCOptimizer.exe', 'PCOptimizer.exe')) {
    $p = Join-Path $SRC $cand
    if (Test-Path $p) { $exeSrc = $p; break }
}
if ($exeSrc) {
    Copy-Item $exeSrc (Join-Path $inst 'OptiBoost.exe') -Force
    $target = Join-Path $inst 'OptiBoost.exe'
    $arguments = ''
    Write-Host '단독 실행파일(exe) 방식으로 설치 (파이썬 불필요)'
} else {
    Copy-Item (Join-Path $SRC 'PCOptimizer.pyw') (Join-Path $inst 'OptiBoost.pyw') -Force
    $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pyw) { $pyw = 'pythonw' }
    $target = $pyw
    $arguments = '"' + (Join-Path $inst 'OptiBoost.pyw') + '"'
    Write-Host '파이썬 스크립트 방식으로 설치'
}

# 부속 파일 복사
Copy-Item (Join-Path $SRC 'icon.ico') (Join-Path $inst 'icon.ico') -Force
if (Test-Path (Join-Path $SRC '사용법.txt')) {
    Copy-Item (Join-Path $SRC '사용법.txt') (Join-Path $inst '사용법.txt') -Force
}
Copy-Item (Join-Path $SRC 'uninstall.ps1') (Join-Path $inst 'uninstall.ps1') -Force

$icon = Join-Path $inst 'icon.ico'

function New-Shortcut($path) {
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $target
    if ($arguments) { $s.Arguments = $arguments }
    $s.WorkingDirectory = $inst
    $s.IconLocation = $icon
    $s.Description = 'OptiBoost'
    $s.Save()
}

# 시작 메뉴 + 바탕화면 바로가기
$startDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Shortcut (Join-Path $startDir 'OptiBoost.lnk')
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'OptiBoost.lnk')

# 프로그램 추가/제거(앱 및 기능) 등록
$uk = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OptiBoost'
New-Item -Path $uk -Force | Out-Null
$unPath = Join-Path $inst 'uninstall.ps1'
$un = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$c=Get-Content -Raw -Encoding UTF8 ''{0}''; iex $c"' -f $unPath
Set-ItemProperty $uk 'DisplayName' 'OptiBoost'
Set-ItemProperty $uk 'DisplayVersion' '2.2'
Set-ItemProperty $uk 'Publisher' 'OptiBoost'
Set-ItemProperty $uk 'DisplayIcon' $icon
Set-ItemProperty $uk 'InstallLocation' $inst
Set-ItemProperty $uk 'UninstallString' $un
Set-ItemProperty $uk 'NoModify' 1 -Type DWord
Set-ItemProperty $uk 'NoRepair' 1 -Type DWord
$sz = [int]((Get-ChildItem $inst -Recurse | Measure-Object Length -Sum).Sum / 1024)
Set-ItemProperty $uk 'EstimatedSize' $sz -Type DWord

Write-Host ''
Write-Host ('설치 완료 → ' + $inst)
Write-Host '시작 메뉴와 바탕화면에 "OptiBoost" 바로가기를 만들었습니다.'
Write-Host '제거는 [설정 > 앱 > 설치된 앱]에서 "OptiBoost"를 제거하면 됩니다.'
