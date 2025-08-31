# app/services/token_cache.py
import json
import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROJ_ROOT = Path(__file__).resolve().parents[2]  # auto_trader/
CACHE_DIR = PROJ_ROOT / "tmp"
CACHE_FILE = CACHE_DIR / "kis_token_cache.json"

LIFETIME = 24 * 3600  # 24 h


def _ensure_cache_dir() -> None:
    """캐시 디렉토리가 존재하고 적절한 권한을 가지는지 확인"""
    try:
        CACHE_DIR.mkdir(exist_ok=True, mode=0o755)
        # 디렉토리 권한 확인 및 설정
        if CACHE_DIR.exists():
            os.chmod(CACHE_DIR, 0o755)
    except (OSError, PermissionError) as e:
        logger.warning(f"캐시 디렉토리 생성/권한 설정 실패: {e}")


def load_token() -> str | None:
    """토큰 캐시에서 유효한 토큰을 로드"""
    try:
        _ensure_cache_dir()
        
        if not CACHE_FILE.exists():
            logger.info("토큰 캐시 파일이 존재하지 않음")
            return None
            
        data = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        
        if time.time() - data["issued_at"] < LIFETIME - 3600:  # 1 h 여유
            logger.info("캐시된 토큰 사용")
            return data["access_token"]
        else:
            logger.info("캐시된 토큰이 만료됨")
            return None
            
    except (json.JSONDecodeError, KeyError, OSError, PermissionError) as e:
        logger.warning(f"토큰 로드 실패: {e}")
        return None


def save_token(token: str) -> None:
    """토큰을 캐시 파일에 저장"""
    try:
        _ensure_cache_dir()
        
        data = {
            "access_token": token, 
            "issued_at": time.time()
        }
        
        # 임시 파일에 먼저 쓰고 원자적으로 이동
        temp_file = CACHE_FILE.with_suffix('.tmp')
        temp_file.write_text(json.dumps(data, indent=2), encoding='utf-8')
        
        # 파일 권한 설정 (소유자만 읽기/쓰기)
        os.chmod(temp_file, 0o600)
        
        # 원자적으로 파일 이동
        temp_file.replace(CACHE_FILE)
        
        logger.info("토큰이 캐시에 저장됨")
        
    except (OSError, PermissionError) as e:
        logger.error(f"토큰 저장 실패: {e}")
        # 실패해도 애플리케이션이 중단되지 않도록 함
