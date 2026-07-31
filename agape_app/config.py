import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_anon_key: str

    @property
    def is_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)


def load_settings() -> Settings:
    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    if supabase_url.endswith("/rest/v1"):
        supabase_url = supabase_url.removesuffix("/rest/v1").rstrip("/")
    return Settings(
        supabase_url=supabase_url,
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", "").strip(),
    )
