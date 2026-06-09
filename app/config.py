"""❷ 환경 흡수 계층 (config).

Render ↔ 로컬 PC 의 모든 차이를 이 한 곳에서만 흡수한다.
core/ 와 web/ 은 환경변수를 직접 읽지 않고 반드시 이 모듈을 통해서만 참조한다.
→ 환경이 바뀌어도 코드 변경 없이 .env 값만 갈아끼우면 된다.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv 미설치 환경에서도 동작
    pass


def _get(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Claude API ───────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = _get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ── 저장소 ───────────────────────────────────────────────────
# 전체 데이터는 이 SQLite 파일 1개. 환경 이전은 이 파일 복사로 끝난다.
DB_PATH: str = _get("DB_PATH", "./data/pricing.db")

# ── 입력 ─────────────────────────────────────────────────────
COMPANIES_FILE: str = _get("COMPANIES_FILE", "./companies.yaml")

# ── 스케줄 ───────────────────────────────────────────────────
#   internal = 웹 프로세스 내 APScheduler (Render 상시)
#   external = OS 스케줄러 → CLI (로컬 PC)
SCHEDULER_MODE: str = _get("SCHEDULER_MODE", "external").lower()
SCHEDULE_DAY_OF_WEEK: str = _get("SCHEDULE_DAY_OF_WEEK", "mon")
SCHEDULE_HOUR: int = _get_int("SCHEDULE_HOUR", 9)
SCHEDULE_MINUTE: int = _get_int("SCHEDULE_MINUTE", 0)
SCHEDULE_TIMEZONE: str = _get("SCHEDULE_TIMEZONE", "America/New_York")

# ── US / USD 강제 (8장) ──────────────────────────────────────
LOCALE: str = _get("LOCALE", "en-US")
TIMEZONE_ID: str = _get("TIMEZONE_ID", "America/New_York")
ACCEPT_LANGUAGE: str = _get("ACCEPT_LANGUAGE", "en-US,en;q=0.9")

# ── 웹 서버 ──────────────────────────────────────────────────
PORT: int = _get_int("PORT", 8000)

# ── Fetch 동작 ───────────────────────────────────────────────
FETCH_TIMEOUT_MS: int = _get_int("FETCH_TIMEOUT_MS", 25000)
FETCH_MAX_RETRIES: int = _get_int("FETCH_MAX_RETRIES", 2)


def ensure_db_dir() -> str:
    """DB_PATH 의 부모 디렉토리를 보장하고 절대 경로를 돌려준다."""
    p = Path(DB_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)
