param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
Set-Location $PSScriptRoot

function Assert-NativeSuccess {
    param([Parameter(Mandatory = $true)][string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation falhou com código de saída $LASTEXITCODE."
    }
}

$versionSource = Get-Content "agape_app\version.py" -Raw
if ($versionSource -notmatch "APP_VERSION\s*=\s*`"$([regex]::Escape($Version))`"") {
    throw "Atualize APP_VERSION em agape_app\version.py para $Version antes de publicar."
}

$gitStatus = git status --porcelain
Assert-NativeSuccess "Verificação do repositório Git"
if ($gitStatus) {
    throw "Existem alterações sem commit. Faça o commit antes de publicar."
}

git show-ref --verify --quiet "refs/tags/v$Version"
$tagLookupExitCode = $LASTEXITCODE
if ($tagLookupExitCode -eq 0) {
    throw "A tag v$Version já existe localmente. Use uma versão nova."
}
if ($tagLookupExitCode -ne 1) {
    throw "Não foi possível verificar se a tag v$Version já existe (código $tagLookupExitCode)."
}

python -m unittest discover -s tests -p "test_*.py"
Assert-NativeSuccess "Execução dos testes"

git tag "v$Version"
Assert-NativeSuccess "Criação da tag v$Version"

git push --atomic origin HEAD "v$Version"
Assert-NativeSuccess "Envio atômico da branch e da tag v$Version"

Write-Host "Versão v$Version enviada. Acompanhe o build na aba Actions do GitHub."
