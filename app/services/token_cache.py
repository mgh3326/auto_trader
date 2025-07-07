# app/services/token_cache.py
from pathlib import Path
import json, time, os

PROJ_ROOT = Path(__file__).resolve().parents[2]  # auto_trader/
CACHE_DIR = PROJ_ROOT / "tmp"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE = CACHE_DIR / "kis_token_cache.json"

LIFETIME = 24 * 3600  # 24 h


def load_token() -> str | None:
    if not CACHE_FILE.exists():
        return None
    data = json.loads(CACHE_FILE.read_text())
    if time.time() - data["issued_at"] < LIFETIME - 3600:  # 1 h 여유
        return data["access_token"]
    return None


def save_token(token: str) -> None:
    CACHE_FILE.write_text(
        json.dumps({"access_token": token, "issued_at": time.time()}))
    os.chmod(CACHE_FILE, 0o600)  # 읽기 / 쓰기 = 소유자만
