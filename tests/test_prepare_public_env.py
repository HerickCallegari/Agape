import tempfile
import unittest
from pathlib import Path

from prepare_public_env import public_env_values, write_public_env


class PublicEnvTests(unittest.TestCase):
    def test_writer_keeps_only_public_supabase_values(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            source = Path(temp_dir) / "source.env"
            target = Path(temp_dir) / "bundle" / ".env"
            source.write_text(
                "SUPABASE_URL=https://example.supabase.co\n"
                "SUPABASE_ANON_KEY=public-anon-key\n"
                "UNRELATED_SECRET=must-not-be-bundled\n",
                encoding="utf-8",
            )

            write_public_env(source, target)

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "SUPABASE_URL=https://example.supabase.co\nSUPABASE_ANON_KEY=public-anon-key\n",
            )

    def test_service_role_is_rejected_with_export_syntax(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            source = Path(temp_dir) / ".env"
            source.write_text(
                "SUPABASE_URL=https://example.supabase.co\n"
                "SUPABASE_ANON_KEY=public-anon-key\n"
                "export SUPABASE_SERVICE_ROLE_KEY=private-key\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "SUPABASE_SERVICE_ROLE_KEY"):
                public_env_values(source)

    def test_missing_public_value_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            source = Path(temp_dir) / ".env"
            source.write_text("SUPABASE_URL=https://example.supabase.co\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "SUPABASE_ANON_KEY"):
                public_env_values(source)

    def test_missing_source_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            with self.assertRaisesRegex(ValueError, "não encontrado"):
                public_env_values(Path(temp_dir) / "missing.env")


if __name__ == "__main__":
    unittest.main()
