#!/bin/bash
set -euo pipefail

report_unexpected_error() {
  local status=$?
  local line="${BASH_LINENO[0]:-desconhecida}"
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::error title=Falha ao gerar DMG universal::Comando inesperadamente encerrado na linha $line (código $status)."
  fi
  exit "$status"
}
trap report_unexpected_error ERR

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

universal_app="$work_dir/ClinicaAgape.app"
ditto "$arm_app" "$universal_app"

index=0
while IFS= read -r -d '' arm_binary; do
  if ! file -b "$arm_binary" | grep -q '^Mach-O'; then
    continue
  fi

  relative_path="${arm_binary#"$arm_app"/}"
  x64_binary="$x64_app/$relative_path"
  if [[ ! -f "$x64_binary" ]] || ! file -b "$x64_binary" | grep -q '^Mach-O'; then
    fail "Binário Intel correspondente não encontrado: $relative_path"
  fi

  arm_arches="$(lipo -archs "$arm_binary")"
  x64_arches="$(lipo -archs "$x64_binary")"
  if [[ " $arm_arches " != *" arm64 "* ]]; then
    fail "Slice ARM ausente em $relative_path: $arm_arches"
  fi
  if [[ " $x64_arches " != *" x86_64 "* ]]; then
    fail "Slice Intel ausente em $relative_path: $x64_arches"
  fi

  arm_slice="$work_dir/slice-$index-arm64"
  x64_slice="$work_dir/slice-$index-x86_64"
  merged_binary="$work_dir/slice-$index-universal2"
  lipo "$arm_binary" -thin arm64 -output "$arm_slice"
  lipo "$x64_binary" -thin x86_64 -output "$x64_slice"
  lipo -create "$arm_slice" "$x64_slice" -output "$merged_binary"
  chmod "$(stat -f '%Lp' "$arm_binary")" "$merged_binary"
  mv "$merged_binary" "$universal_app/$relative_path"
  index=$((index + 1))
done < <(find "$arm_app" -type f -print0)

while IFS= read -r -d '' x64_binary; do
  if ! file -b "$x64_binary" | grep -q '^Mach-O'; then
    continue
  fi
  relative_path="${x64_binary#"$x64_app"/}"
  arm_binary="$arm_app/$relative_path"
  if [[ ! -f "$arm_binary" ]] || ! file -b "$arm_binary" | grep -q '^Mach-O'; then
    fail "Binário ARM correspondente não encontrado: $relative_path"
  fi
done < <(find "$x64_app" -type f -print0)

# Recursos não executáveis podem variar entre wheels de arquiteturas distintas.
# O bundle ARM é a base; inclua recursos exclusivos do Intel sem substituir
# binários ou arquivos compartilhados. Todo Mach-O continua obrigado a existir
# nos dois bundles e é unido explicitamente acima.
while IFS= read -r -d '' x64_item; do
  relative_path="${x64_item#"$x64_app"/}"
  universal_item="$universal_app/$relative_path"

  if [[ -L "$x64_item" ]]; then
    if [[ -L "$universal_item" ]]; then
      x64_target="$(readlink "$x64_item")"
      universal_target="$(readlink "$universal_item")"
      if [[ "$x64_target" != "$universal_target" ]]; then
        fail "Links simbólicos divergentes em $relative_path."
      fi
      continue
    fi
    if [[ -e "$universal_item" ]]; then
      fail "Conflito de tipo ao incluir recurso Intel: $relative_path"
    fi
    mkdir -p "$(dirname "$universal_item")"
    ln -s "$(readlink "$x64_item")" "$universal_item"
    continue
  fi

  if [[ -L "$universal_item" ]]; then
    fail "Conflito de tipo ao incluir recurso Intel: $relative_path"
  fi
  if [[ -e "$universal_item" ]]; then
    continue
  fi
  if file -b "$x64_item" | grep -q '^Mach-O'; then
    fail "Binário Intel sem correspondente no bundle ARM: $relative_path"
  fi
  mkdir -p "$(dirname "$universal_item")"
  ditto "$x64_item" "$universal_item"
done < <(find "$x64_app" \( -type f -o -type l \) -print0)

while IFS= read -r -d '' candidate; do
  if ! file -b "$candidate" | grep -q '^Mach-O'; then
    continue
  fi
  arches="$(lipo -archs "$candidate")"
  if [[ " $arches " != *" arm64 "* || " $arches " != *" x86_64 "* ]]; then
    fail "Binário universal incompleto: $candidate ($arches)."
  fi
done < <(find "$universal_app" -type f -print0)

# A união dos slices invalida as assinaturas ad-hoc originais; assine o bundle final.
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

  # Clientes anteriores à adoção do universal2 procuram estes nomes. As cópias
  # preservam o caminho de atualização, mas contêm exatamente o mesmo DMG.
  for compatibility_name in ClinicaAgape-macOS-arm64.dmg ClinicaAgape-macOS-x64.dmg; do
    cp "$asset_name" "$compatibility_name"
    shasum -a 256 "$compatibility_name" > "$compatibility_name.sha256"
  done
)

echo "Pacote universal gerado em release/$asset_name"
