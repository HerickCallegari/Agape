#!/bin/bash
set -euo pipefail

report_unexpected_error() {
  local status="$1"
  local line="$2"
  local command="$3"
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::error title=Falha ao gerar DMG universal::Comando '$command' encerrado na linha $line (código $status)."
  fi
  exit "$status"
}
trap 'report_unexpected_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

fail() {
  local message="$1"
  echo "$message" >&2
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::error title=Falha ao gerar DMG universal::$message"
  fi
  exit 1
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  fail "Este script precisa ser executado no macOS."
fi

if [[ "$#" -ne 2 ]]; then
  fail "Uso: $0 CAMINHO_APP_ARM64 CAMINHO_APP_X64"
fi

arm_app="$1"
x64_app="$2"
for app in "$arm_app" "$x64_app"; do
  if [[ ! -d "$app/Contents/MacOS" || ! -f "$app/Contents/Info.plist" ]]; then
    fail "Bundle macOS inválido: $app"
  fi
done

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/clinica-agape-universal.XXXXXX")"
cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

for plist_key in CFBundleIdentifier CFBundleShortVersionString CFBundleExecutable LSMinimumSystemVersion; do
  arm_value="$(/usr/libexec/PlistBuddy -c "Print :$plist_key" "$arm_app/Contents/Info.plist")"
  x64_value="$(/usr/libexec/PlistBuddy -c "Print :$plist_key" "$x64_app/Contents/Info.plist")"
  if [[ "$arm_value" != "$x64_value" ]]; then
    fail "Os bundles divergem em $plist_key: ARM=$arm_value, Intel=$x64_value."
  fi
done

if ! cmp -s "$arm_app/Contents/Resources/.env" "$x64_app/Contents/Resources/.env"; then
  fail "A configuração pública incluída nos bundles ARM e Intel é diferente."
fi

validate_bundle_architecture() {
  local app="$1"
  local expected_arch="$2"
  local candidate
  local arches

  while IFS= read -r -d '' candidate; do
    if ! file -b "$candidate" | grep -q '^Mach-O'; then
      continue
    fi
    arches="$(lipo -archs "$candidate")"
    if [[ " $arches " != *" $expected_arch "* ]]; then
      fail "Binário sem a arquitetura $expected_arch: $candidate ($arches)."
    fi
  done < <(find "$app" -type f -print0)
}

validate_bundle_architecture "$arm_app" arm64
validate_bundle_architecture "$x64_app" x86_64

universal_app="$work_dir/ClinicaAgape.app"
mkdir -p \
  "$universal_app/Contents/MacOS" \
  "$universal_app/Contents/Resources/native/arm64" \
  "$universal_app/Contents/Resources/native/x86_64"

ditto "$arm_app" "$universal_app/Contents/Resources/native/arm64/ClinicaAgape.app"
ditto "$x64_app" "$universal_app/Contents/Resources/native/x86_64/ClinicaAgape.app"
ditto "$arm_app/Contents/Info.plist" "$universal_app/Contents/Info.plist"

icon_name="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$arm_app/Contents/Info.plist" 2>/dev/null || true)"
if [[ -n "$icon_name" && -f "$arm_app/Contents/Resources/$icon_name" ]]; then
  ditto "$arm_app/Contents/Resources/$icon_name" "$universal_app/Contents/Resources/$icon_name"
fi

minimum_macos="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$universal_app/Contents/Info.plist")"
launcher="$universal_app/Contents/MacOS/ClinicaAgape"
clang \
  -arch arm64 \
  -arch x86_64 \
  -mmacosx-version-min="$minimum_macos" \
  macos_launcher.c \
  -o "$launcher"

launcher_arches="$(lipo -archs "$launcher")"
if [[ " $launcher_arches " != *" arm64 "* || " $launcher_arches " != *" x86_64 "* ]]; then
  fail "O launcher não contém as duas arquiteturas: $launcher_arches."
fi

# O launcher é universal2; os aplicativos PyInstaller permanecem nativos e
# intactos, evitando corromper os arquivos PKG anexados aos executáveis.
codesign --force --deep --sign - "$universal_app"
codesign --verify --deep --strict "$universal_app"

mkdir -p release
stage_dir="$work_dir/dmg"
mkdir -p "$stage_dir"
ditto "$universal_app" "$stage_dir/ClinicaAgape.app"
codesign --verify --deep --strict "$stage_dir/ClinicaAgape.app"
ln -s /Applications "$stage_dir/Aplicativos"

asset_name="ClinicaAgape-macOS-universal.dmg"
hdiutil create \
  -volname "Clínica Ágape" \
  -srcfolder "$stage_dir" \
  -ov \
  -format UDZO \
  "release/$asset_name"
hdiutil verify "release/$asset_name"

(
  cd release
  shasum -a 256 "$asset_name" > "$asset_name.sha256"

  # Clientes anteriores à adoção do pacote único procuram estes nomes. As
  # cópias preservam o caminho de atualização e contêm o mesmo DMG.
  for compatibility_name in ClinicaAgape-macOS-arm64.dmg ClinicaAgape-macOS-x64.dmg; do
    cp "$asset_name" "$compatibility_name"
    shasum -a 256 "$compatibility_name" > "$compatibility_name.sha256"
  done
)

echo "Pacote universal gerado em release/$asset_name"
