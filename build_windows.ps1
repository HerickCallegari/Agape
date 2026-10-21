$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
Set-Location $PSScriptRoot

function Assert-NativeSuccess {
  param([Parameter(Mandatory = $true)][string]$Operation)
  if ($LASTEXITCODE -ne 0) {
    throw "$Operation falhou com codigo de saida $LASTEXITCODE."
  }
}

python -m pip install -r requirements.txt
Assert-NativeSuccess "Instalacao das dependencias"

python -m PyInstaller --noconfirm ClinicaAgape.spec
Assert-NativeSuccess "Build do executavel Windows"

if (-not (Test-Path "dist\ClinicaAgape\ClinicaAgape.exe")) {
  throw "O build nao gerou dist\ClinicaAgape\ClinicaAgape.exe."
}

if (Test-Path ".env") {
  python prepare_public_env.py ".env" "dist\ClinicaAgape\.env"
  Assert-NativeSuccess "Preparacao da configuracao publica"
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
  Assert-NativeSuccess "Geracao do instalador Windows"
  if (-not (Test-Path "release\ClinicaAgape-Setup.exe")) {
    throw "O Inno Setup nao gerou release\ClinicaAgape-Setup.exe."
  }
  Write-Host "Instalador gerado em release\ClinicaAgape-Setup.exe"
} else {
  Write-Warning "Inno Setup 6 nao encontrado. O executavel foi gerado, mas o instalador nao."
}
