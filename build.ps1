param([string]$Python = "$PSScriptRoot\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot '.pyinstaller-cache'
    if (-not (Test-Path -LiteralPath $Python)) { throw '请先按 README 建立 .venv，或传入 -Python 指定 64 位 Python。' }
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw '离线测试失败。' }
    & $Python -m PyInstaller --noconfirm --distpath dist --workpath build WechatMomentExtractor.spec
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    Copy-Item -LiteralPath README.md -Destination dist/README.md -Force
    Copy-Item -LiteralPath README.en.md -Destination dist/README.en.md -Force
    Copy-Item -LiteralPath LICENSE -Destination dist/LICENSE -Force
    Copy-Item -LiteralPath NOTICE -Destination dist/NOTICE -Force
    Write-Host '已生成 dist/WechatMomentExtractor.exe'
} finally { Pop-Location }
