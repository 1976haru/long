Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)

$root = (Get-Location).Path
$outRoot = Join-Path $root "release_friend_test"
$pack = Join-Path $outRoot "YouTube_Playlist_Studio_Friend_Test_v0.3"
$zip = Join-Path $outRoot "YouTube_Playlist_Studio_Friend_Test_v0.3.zip"
$distExe = Join-Path $root "dist\YouTube_Playlist_Studio_Friend_Test_v0.3.exe"

if (Test-Path $pack) { Remove-Item $pack -Recurse -Force }
if (Test-Path $zip) { Remove-Item $zip -Force }
New-Item -ItemType Directory -Force -Path $pack | Out-Null

$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = @("py","-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = @("python") }
else { throw "Python을 찾을 수 없습니다." }

function Run-Python([string[]]$args) {
    if ($py.Count -eq 2) { & $py[0] $py[1] @args }
    else { & $py[0] @args }
    if ($LASTEXITCODE -ne 0) { throw "Python 명령 실패: $($args -join ' ')" }
}

Run-Python @("-m","pip","install","--upgrade","pyinstaller","tzdata")
Run-Python @("-m","app.manual_html")

Run-Python @(
  "-m","PyInstaller","--noconfirm","--clean","--onefile","--windowed",
  "--name","YouTube_Playlist_Studio_Friend_Test_v0.3",
  "--add-data","cloud\long_live_worker.py;cloud",
  "--add-data","deploy\linux;deploy\linux",
  "--add-data","docs\*.html;docs",
  "friend_main.py"
)

if (-not (Test-Path $distExe)) { throw "빌드 EXE를 찾을 수 없습니다: $distExe" }

Copy-Item $distExe $pack
Copy-Item "friend_test\00_처음에_읽기.txt" $pack
Copy-Item "friend_test\테스트결과_보내기.txt" $pack
Copy-Item "friend_test\01_처음_한번만_실행.bat" $pack
Copy-Item "friend_test\02_프로그램_실행.bat" $pack

# 사용자가 직접 준비할 것이 없도록 10초 테스트 MP4를 만든다.
$ffmpeg = $null
$settingFile = Join-Path $env:APPDATA "PlaylistLongVideoMaker\settings.json"
if (Test-Path $settingFile) {
    try {
        $j = Get-Content $settingFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.ffmpeg_path -and (Test-Path $j.ffmpeg_path)) { $ffmpeg = $j.ffmpeg_path }
    } catch {}
}
if (-not $ffmpeg) {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { $ffmpeg = $cmd.Source }
}
if ($ffmpeg) {
    $sample = Join-Path $pack "TEST_SAMPLE_10SEC.mp4"
    & $ffmpeg -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -f lavfi -i "sine=frequency=440:sample_rate=48000" -t 10 -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 60 -c:a aac -b:a 128k -ar 48000 -ac 2 $sample
    if ($LASTEXITCODE -ne 0) { Write-Warning "테스트 MP4 생성 실패. EXE 패키지는 계속 만듭니다." }
} else {
    Write-Warning "FFmpeg를 찾지 못해 TEST_SAMPLE_10SEC.mp4는 넣지 못했습니다."
}

Compress-Archive -Path (Join-Path $pack "*") -DestinationPath $zip -CompressionLevel Optimal
Write-Host ""
Write-Host "친구 테스트판 완성:"
Write-Host $zip
