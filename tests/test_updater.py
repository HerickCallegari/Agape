import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agape_app.updater import (
    ReleaseInfo,
    download_installer,
    latest_release,
    update_asset_name,
    update_launch_command,
    version_tuple,
)
from agape_app.version import (
    MACOS_UNIVERSAL_UPDATE_ASSET_NAME,
    WINDOWS_UPDATE_ASSET_NAME,
)


class UpdaterVersionTests(unittest.TestCase):
    def test_versions_are_compared_numerically(self):
        self.assertGreater(version_tuple("1.10.0"), version_tuple("1.9.9"))

    def test_optional_v_prefix_is_accepted(self):
        self.assertEqual(version_tuple("v2.3.4"), (2, 3, 4))

    def test_invalid_version_is_rejected(self):
        with self.assertRaises(ValueError):
            version_tuple("1.2")


class UpdaterPlatformTests(unittest.TestCase):
    def test_windows_keeps_legacy_installer_name(self):
        self.assertEqual(update_asset_name("win32", "AMD64"), WINDOWS_UPDATE_ASSET_NAME)

    def test_apple_silicon_uses_universal_dmg(self):
        self.assertEqual(update_asset_name("darwin", "arm64"), MACOS_UNIVERSAL_UPDATE_ASSET_NAME)

    def test_intel_mac_uses_universal_dmg(self):
        self.assertEqual(update_asset_name("darwin", "x86_64"), MACOS_UNIVERSAL_UPDATE_ASSET_NAME)

    def test_unsupported_mac_architecture_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Arquitetura de Mac não suportada"):
            update_asset_name("darwin", "powerpc")

    def test_unsupported_platform_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "não são suportadas"):
            update_asset_name("linux", "x86_64")

    def test_windows_update_command_preserves_inno_setup_flags(self):
        self.assertEqual(
            update_launch_command("C:/Temp/ClinicaAgape-Setup.exe", "win32"),
            [
                "C:/Temp/ClinicaAgape-Setup.exe",
                "/SILENT",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
            ],
        )

    def test_macos_update_command_opens_dmg_without_shell(self):
        self.assertEqual(
            update_launch_command("/tmp/ClinicaAgape-macOS-universal.dmg", "darwin"),
            ["/usr/bin/open", "/tmp/ClinicaAgape-macOS-universal.dmg"],
        )


class FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeDownloadResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self.payload


class UpdaterReleaseTests(unittest.TestCase):
    @patch("agape_app.updater.httpx.get")
    def test_latest_release_selects_universal_macos_asset_and_checksum(self, mocked_get):
        mocked_get.return_value = FakeJsonResponse(
            {
                "tag_name": "v1.0.18",
                "body": "Suporte ao macOS",
                "assets": [
                    {"name": WINDOWS_UPDATE_ASSET_NAME, "browser_download_url": "https://example.test/windows"},
                    {
                        "name": MACOS_UNIVERSAL_UPDATE_ASSET_NAME,
                        "browser_download_url": "https://example.test/macos-universal",
                    },
                    {
                        "name": f"{MACOS_UNIVERSAL_UPDATE_ASSET_NAME}.sha256",
                        "browser_download_url": "https://example.test/macos-universal-sha",
                    },
                ],
            }
        )

        release = latest_release("1.0.17", platform_name="darwin", machine="arm64")

        self.assertIsNotNone(release)
        self.assertEqual(release.asset_name, MACOS_UNIVERSAL_UPDATE_ASSET_NAME)
        self.assertEqual(release.installer_url, "https://example.test/macos-universal")
        self.assertEqual(release.checksum_url, "https://example.test/macos-universal-sha")

    @patch("agape_app.updater.httpx.get")
    def test_missing_platform_asset_has_clear_error(self, mocked_get):
        mocked_get.return_value = FakeJsonResponse({"tag_name": "v1.0.18", "assets": []})

        with self.assertRaisesRegex(RuntimeError, MACOS_UNIVERSAL_UPDATE_ASSET_NAME):
            latest_release("1.0.17", platform_name="darwin", machine="x86_64")

    @patch("agape_app.updater.httpx.get")
    def test_current_release_returns_none_before_asset_lookup(self, mocked_get):
        mocked_get.return_value = FakeJsonResponse({"tag_name": "v1.0.17", "assets": []})

        self.assertIsNone(latest_release("1.0.17", platform_name="win32"))

    @patch("agape_app.updater.httpx.get")
    def test_unsupported_platform_does_not_call_github(self, mocked_get):
        with self.assertRaises(RuntimeError):
            latest_release("1.0.17", platform_name="linux", machine="x86_64")
        mocked_get.assert_not_called()

    def test_download_uses_release_asset_name_and_validates_checksum(self):
        payload = b"macOS dmg content"
        checksum = hashlib.sha256(payload).hexdigest()
        checksum_response = MagicMock()
        checksum_response.text = f"{checksum}  {MACOS_UNIVERSAL_UPDATE_ASSET_NAME}"
        checksum_response.raise_for_status.return_value = None
        release = ReleaseInfo(
            version="1.0.18",
            installer_url="https://example.test/installer",
            checksum_url="https://example.test/checksum",
            notes="",
            asset_name=MACOS_UNIVERSAL_UPDATE_ASSET_NAME,
        )

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            with (
                patch("agape_app.updater.tempfile.gettempdir", return_value=temp_dir),
                patch("agape_app.updater.httpx.get", return_value=checksum_response),
                patch("agape_app.updater.httpx.stream", return_value=FakeDownloadResponse(payload)),
            ):
                target = download_installer(release)

            self.assertEqual(target.name, MACOS_UNIVERSAL_UPDATE_ASSET_NAME)
            self.assertEqual(target.read_bytes(), payload)
            self.assertTrue(str(target).startswith(str(Path(temp_dir))))

    def test_download_removes_file_when_checksum_does_not_match(self):
        checksum_response = MagicMock()
        checksum_response.text = f"{'0' * 64}  {MACOS_UNIVERSAL_UPDATE_ASSET_NAME}"
        checksum_response.raise_for_status.return_value = None
        release = ReleaseInfo(
            version="1.0.18",
            installer_url="https://example.test/installer",
            checksum_url="https://example.test/checksum",
            notes="",
            asset_name=MACOS_UNIVERSAL_UPDATE_ASSET_NAME,
        )

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            with (
                patch("agape_app.updater.tempfile.gettempdir", return_value=temp_dir),
                patch("agape_app.updater.httpx.get", return_value=checksum_response),
                patch("agape_app.updater.httpx.stream", return_value=FakeDownloadResponse(b"altered")),
            ):
                with self.assertRaisesRegex(RuntimeError, "integridade"):
                    download_installer(release)
            target = Path(temp_dir) / "ClinicaAgape" / release.version / release.asset_name
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
