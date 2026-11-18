# claude auto restater 설치 (Windows PowerShell)
# 사용: PowerShell 에서  irm https://raw.githubusercontent.com/youtuber04/claude-auto-restater/main/install.ps1 | iex
# 하는 일: 스킬 폴더를 ~/.claude/skills/claude auto restater 에 받고(git 없으면 ZIP), setup.py 로 상태표시줄·훅을 settings.json 에 등록한다.
$ErrorActionPreference = 'Stop'
$repo = 'https://github.com/youtuber04/claude-auto-restater'
$dest = Join-Path $HOME '.claude\skills\claude auto restater'
New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null

if (Get-Command git -ErrorAction SilentlyContinue) {
  if (Test-Path (Join-Path $dest '.git')) { git -C $dest pull --ff-only | Out-Host }
  else {
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    git clone --depth 1 $repo $dest | Out-Host
  }
} else {
  $zip = Join-Path $env:TEMP 'claude-auto-restater.zip'
  $tmp = Join-Path $env:TEMP 'claude-auto-restater-unzip'
  Invoke-WebRequest "$repo/archive/refs/heads/main.zip" -OutFile $zip
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
  Expand-Archive $zip $tmp -Force
  New-Item -ItemType Directory -Force $dest | Out-Null
  Copy-Item (Join-Path $tmp 'claude-auto-restater-main\*') $dest -Recurse -Force
}

# 파이썬 3 찾기: py 런처 → python3 → python (Microsoft Store 빈 껍데기는 --version 이 실패해 걸러진다)
$py = $null
foreach ($cand in @(@('py', '-3'), @('python3'), @('python'))) {
  try {
    $exe = (Get-Command $cand[0] -ErrorAction Stop).Source
    $v = & $exe @($cand[1..($cand.Length - 1)]) --version 2>$null
    if ($LASTEXITCODE -eq 0 -and "$v" -match 'Python 3') { $py = $cand; break }
  } catch {}
}
if (-not $py) {
  Write-Host '파이썬 3이 필요합니다. https://www.python.org/downloads/ 에서 설치할 때 "Add python.exe to PATH" 를 켜고 다시 실행하세요.'
  exit 1
}
$exe = (Get-Command $py[0]).Source
$args = @($py[1..($py.Length - 1)]) + @('-X', 'utf8', (Join-Path $dest 'scripts\setup.py'))
& $exe @args
Write-Host ''
Write-Host '설치 끝. 터미널에서 claude 를 (다시) 켠 뒤  "claude auto restater 켜고 <작업>"  이라고 입력하세요.'
