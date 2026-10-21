from __future__ import annotations

import hashlib
import platform
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .version import (
    MACOS_UNIVERSAL_UPDATE_ASSET_NAME,
    UPDATE_ASSET_NAME,
    UPDATE_REPOSITORY,
    WINDOWS_UPDATE_ASSET_NAME,
)


VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    installer_url: str
    checksum_url: str
    notes: str
    asset_name: str = UPDATE_ASSET_NAME


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Versão inválida: {value}")
    return tuple(int(part) for part in match.groups())


def update_asset_name(platform_name: str | None = None, machine: str | None = None) -> str:
    current_platform = platform_name or sys.platform
    if current_platform == "win32":
        return WINDOWS_UPDATE_ASSET_NAME
    if current_platform == "darwin":
        current_machine = (machine or platform.machine()).strip().lower()
        if current_machine in {"arm64", "aarch64", "x86_64", "amd64", "x64"}:
            return MACOS_UNIVERSAL_UPDATE_ASSET_NAME
        raise RuntimeError(f"Arquitetura de Mac não suportada para atualização: {current_machine or 'desconhecida'}.")
    raise RuntimeError(f"Atualizações automáticas não são suportadas nesta plataforma: {current_platform}.")


def update_launch_command(installer_path: str | Path, platform_name: str | None = None) -> list[str]:
    current_platform = platform_name or sys.platform
    path = str(installer_path)
    if current_platform == "win32":
        return [path, "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    if current_platform == "darwin":
        return ["/usr/bin/open", path]
    raise RuntimeError(f"Instalação de atualizações não é suportada nesta plataforma: {current_platform}.")


def latest_release(
    current_version: str,
    repository: str = UPDATE_REPOSITORY,
    platform_name: str | None = None,
    machine: str | None = None,
) -> ReleaseInfo | None:
    asset_name = update_asset_name(platform_name, machine)
    response = httpx.get(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=8,
        follow_redirects=True,
    )
    response.raise_for_status()
    release = response.json()
    release_version = str(release.get("tag_name") or "").removeprefix("v")
    if version_tuple(release_version) <= version_tuple(current_version):
        return None

    assets = {asset.get("name"): asset.get("browser_download_url") for asset in release.get("assets") or []}
    installer_url = assets.get(asset_name)
    checksum_url = assets.get(f"{asset_name}.sha256")
    if not installer_url or not checksum_url:
        raise RuntimeError(
            f"A versão publicada não contém o instalador compatível ({asset_name}) "
            "e sua verificação de integridade."
        )
    return ReleaseInfo(
        version=release_version,
        installer_url=installer_url,
        checksum_url=checksum_url,
        notes=str(release.get("body") or ""),
        asset_name=asset_name,
    )


def download_installer(release: ReleaseInfo) -> Path:
    target_dir = Path(tempfile.gettempdir()) / "ClinicaAgape" / release.version
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(release.asset_name).name

    checksum_response = httpx.get(release.checksum_url, timeout=15, follow_redirects=True)
    checksum_response.raise_for_status()
    expected_checksum = checksum_response.text.strip().split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_checksum):
        raise RuntimeError("A verificação de integridade publicada é inválida.")

    digest = hashlib.sha256()
    with httpx.stream("GET", release.installer_url, timeout=120, follow_redirects=True) as response:
        response.raise_for_status()
        with target.open("wb") as installer_file:
            for chunk in response.iter_bytes():
                installer_file.write(chunk)
                digest.update(chunk)

    if digest.hexdigest().lower() != expected_checksum:
        target.unlink(missing_ok=True)
        raise RuntimeError("O instalador baixado não passou na verificação de integridade.")
    return target
