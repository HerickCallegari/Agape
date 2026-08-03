$ErrorActionPreference = "Stop"

python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm ClinicaAgape.spec

if (Test-Path ".env") {
  Copy-Item ".env" "dist\ClinicaAgape\.env" -Force
} else {
  Write-Warning "Arquivo .env nao encontrado. O aplicativo gerado precisara ser configurado antes do uso."
}

Write-Host "Executavel gerado em dist\\ClinicaAgape\\ClinicaAgape.exe"

$versionSource = Get-Content "agape_app\version.py" -Raw
if ($versionSource -notmatch 'APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"') {
  throw "APP_VERSION invalida em agape_app\version.py."
}
$version = $Matches[1]
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
  & $iscc "/DMyAppVersion=$version" installer.iss
  Write-Host "Instalador gerado em release\ClinicaAgape-Setup.exe"
} else {
  Write-Warning "Inno Setup 6 nao encontrado. O executavel foi gerado, mas o instalador nao."
}
