from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values


PUBLIC_KEYS = ("SUPABASE_URL", "SUPABASE_ANON_KEY")
FORBIDDEN_KEYS = ("SUPABASE_SERVICE_ROLE_KEY",)


def public_env_values(source: Path) -> dict[str, str]:
    if not source.is_file():
        raise ValueError(f"Arquivo de configuração não encontrado: {source}")

    parsed = dotenv_values(source)
    forbidden = [key for key in FORBIDDEN_KEYS if key in parsed]
    if forbidden:
        raise ValueError(f"{', '.join(forbidden)} não pode ser incluída no aplicativo desktop.")

    values: dict[str, str] = {}
    missing: list[str] = []
    for key in PUBLIC_KEYS:
        value = str(parsed.get(key) or "").strip()
        if not value:
            missing.append(key)
            continue
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} contém uma quebra de linha inválida.")
        values[key] = value

    if missing:
        raise ValueError(f"Configuração pública ausente: {', '.join(missing)}.")
    return values


def write_public_env(source: Path, target: Path) -> None:
    values = public_env_values(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{key}={values[key]}\n" for key in PUBLIC_KEYS)
    target.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gera um .env de distribuição contendo apenas a configuração pública do Supabase."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args(argv)

    try:
        write_public_env(args.source, args.target)
    except (OSError, ValueError) as exc:
        print(f"Erro ao preparar configuração pública: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
