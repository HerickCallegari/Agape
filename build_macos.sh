#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Este script precisa ser executado no macOS." >&2
  exit 1
fi

case "$(uname -m)" in
  arm64)
    detected_arch="arm64"
    binary_arch="arm64"
    ;;
  x86_64)
    detected_arch="x64"
    binary_arch="x86_64"
    ;;
  *)
    echo "Arquitetura de Mac não suportada: $(uname -m)" >&2
    exit 1
    ;;
esac

expected_arch="${1:-$detected_arch}"
if [[ "$expected_arch" != "$detected_arch" ]]; then
  echo "Runner incompatível: esperado $expected_arch, detectado $detected_arch." >&2
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo ".env não encontrado. O build de distribuição exige a configuração pública do Supabase." >&2
  exit 1
fi

umask 077
host_macos_major="$(sw_vers -productVersion | cut -d. -f1)"
minimum_macos="${AGAPE_MACOS_MIN_VERSION:-${host_macos_major}.0}"
if [[ ! "$minimum_macos" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "Versão mínima do macOS inválida: $minimum_macos" >&2
  exit 1
fi
if [[ "${minimum_macos%%.*}" != "$host_macos_major" ]]; then
  echo "Para garantir compatibilidade, gere no macOS ${minimum_macos%%.*}; o host atual é macOS $host_macos_major." >&2
  exit 1
fi
export AGAPE_MACOS_MIN_VERSION="$minimum_macos"
export AGAPE_MACOS_TARGET_ARCH="$binary_arch"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/clinica-agape-build.XXXXXX")"
cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

public_env="$work_dir/.env"
python prepare_public_env.py .env "$public_env"
export AGAPE_BUNDLE_ENV="$public_env"

python -m PyInstaller --noconfirm --clean ClinicaAgape-macOS.spec

app_bundle="dist/ClinicaAgape.app"
app_executable="$app_bundle/Contents/MacOS/ClinicaAgape"
bundled_env="$app_bundle/Contents/Resources/.env"
if [[ ! -x "$app_executable" ]]; then
  echo "O bundle macOS não contém o executável esperado." >&2
  exit 1
fi
if [[ ! -f "$bundled_env" ]]; then
  echo "O bundle macOS não contém a configuração pública saneada." >&2
  exit 1
fi

validate_native_binary() {
  local binary="$1"
  local arches
  arches="$(lipo -archs "$binary")"
  if [[ " $arches " != *" $binary_arch "* ]]; then
    echo "Binário sem a arquitetura $binary_arch: $binary ($arches)." >&2
    return 1
  fi
}

validate_native_binary "$app_executable"
while IFS= read -r -d '' candidate; do
  if file -b "$candidate" | grep -q '^Mach-O'; then
    validate_native_binary "$candidate"
  fi
done < <(find "$app_bundle" -type f -print0)

# Assinatura ad-hoc: garante integridade estrutural, mas não substitui Developer ID/notarização.
codesign --force --deep --sign - "$app_bundle"
codesign --verify --deep --strict "$app_bundle"

asset_name="ClinicaAgape-macOS-${detected_arch}.dmg"
mkdir -p release
stage_dir="$work_dir/dmg"
mkdir -p "$stage_dir"

ditto "$app_bundle" "$stage_dir/ClinicaAgape.app"
codesign --verify --deep --strict "$stage_dir/ClinicaAgape.app"
ln -s /Applications "$stage_dir/Aplicativos"
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
)

tar -czf "release/ClinicaAgape-macOS-${detected_arch}.app.tar.gz" -C dist ClinicaAgape.app

echo "Pacote gerado em release/$asset_name"
