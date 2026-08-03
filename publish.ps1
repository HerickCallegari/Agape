param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$versionSource = Get-Content "agape_app\version.py" -Raw
if ($versionSource -notmatch "APP_VERSION\s*=\s*`"$([regex]::Escape($Version))`"") {
    throw "Atualize APP_VERSION em agape_app\version.py para $Version antes de publicar."
}

if (git status --porcelain) {
    throw "Existem alterações sem commit. Faça o commit antes de publicar."
}

python -m unittest discover -s tests -p "test_*.py"
git tag "v$Version"
git push origin HEAD
git push origin "v$Version"

Write-Host "Versão v$Version enviada. Acompanhe o build na aba Actions do GitHub."
