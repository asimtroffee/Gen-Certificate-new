import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

_client: Client | None = None
_initialized = False


def init_supabase() -> bool:
    global _client, _initialized
    if _initialized:
        return True

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("Supabase not configured: set SUPABASE_URL and SUPABASE_ANON_KEY")
        return False

    try:
        _client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        _initialized = True
        return True
    except Exception as e:
        print(f"Supabase init failed: {e}")
        return False


def get_client() -> Client | None:
    if not init_supabase():
        return None
    return _client


def get_public_url(bucket: str, path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"
