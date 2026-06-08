"""SQLite 저장소 — WAL 모드, 짧은 커넥션 (5장).

전체 데이터는 config.DB_PATH 의 SQLite 파일 1개. 환경 이전은 파일 복사로 끝난다.
웹 읽기 + 스케줄러 쓰기 동시 접근을 위해 WAL 모드를 켠다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    homepage     TEXT,
    pricing_url  TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company        TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    collected_at   TEXT NOT NULL,
    currency       TEXT NOT NULL,
    raw_text_hash  TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    confidence     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_company ON snapshots(company, id);

CREATE TABLE IF NOT EXISTS changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company      TEXT NOT NULL,
    detected_at  TEXT NOT NULL,
    change_type  TEXT NOT NULL,
    tier_name    TEXT,
    field        TEXT,
    old_value    TEXT,
    new_value    TEXT,
    summary      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_company ON changes(company, id);

CREATE TABLE IF NOT EXISTS run_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at   TEXT NOT NULL,
    run_finished_at  TEXT,
    company          TEXT,
    status           TEXT NOT NULL,
    error_message    TEXT
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """짧게 열고 닫는 커넥션. WAL 모드 적용."""
    path = config.ensure_db_dir()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """테이블/인덱스 생성 + WAL 모드 적용."""
    with connect() as conn:
        conn.executescript(SCHEMA)


# ── companies (모니터링 대상 목록) ───────────────────────────
def list_companies(active_only: bool = True) -> list[sqlite3.Row]:
    with connect() as conn:
        q = "SELECT * FROM companies"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY name COLLATE NOCASE"
        return conn.execute(q).fetchall()


def count_companies() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


def add_company(
    *, name: str, homepage: Optional[str], pricing_url: Optional[str]
) -> None:
    """업체 추가 또는 갱신(같은 이름이면 URL 갱신 + 재활성화)."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO companies (name, homepage, pricing_url, active)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET
                   homepage=excluded.homepage,
                   pricing_url=excluded.pricing_url,
                   active=1""",
            (name, homepage, pricing_url),
        )


def delete_company(name: str) -> None:
    """모니터링 목록에서 제거. 과거 스냅샷/변동 이력은 보존된다."""
    with connect() as conn:
        conn.execute("DELETE FROM companies WHERE name=?", (name,))


# ── snapshots ────────────────────────────────────────────────
def latest_snapshot_row(company: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM snapshots WHERE company=? ORDER BY id DESC LIMIT 1",
            (company,),
        ).fetchone()


def insert_snapshot(
    *,
    company: str,
    source_url: str,
    collected_at: str,
    currency: str,
    raw_text_hash: str,
    payload_json: str,
    confidence: str,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO snapshots
               (company, source_url, collected_at, currency,
                raw_text_hash, payload_json, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (company, source_url, collected_at, currency,
             raw_text_hash, payload_json, confidence),
        )
        return cur.lastrowid


def all_latest_snapshots() -> list[sqlite3.Row]:
    """각 업체의 최신 스냅샷 1건씩."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT s.* FROM snapshots s
            JOIN (
                SELECT company, MAX(id) AS max_id
                FROM snapshots GROUP BY company
            ) m ON s.company = m.company AND s.id = m.max_id
            ORDER BY s.company COLLATE NOCASE
            """
        ).fetchall()


def snapshots_for_company(company: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM snapshots WHERE company=? ORDER BY id ASC",
            (company,),
        ).fetchall()


# ── changes ──────────────────────────────────────────────────
def insert_change(
    *,
    company: str,
    detected_at: str,
    change_type: str,
    tier_name: Optional[str],
    field: Optional[str],
    old_value: Optional[str],
    new_value: Optional[str],
    summary: str,
) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO changes
               (company, detected_at, change_type, tier_name,
                field, old_value, new_value, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (company, detected_at, change_type, tier_name,
             field, old_value, new_value, summary),
        )


def recent_changes(
    *, company: Optional[str] = None, limit: int = 200
) -> list[sqlite3.Row]:
    with connect() as conn:
        if company:
            return conn.execute(
                "SELECT * FROM changes WHERE company=? ORDER BY id DESC LIMIT ?",
                (company, limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def changes_since(iso_ts: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM changes WHERE detected_at >= ? ORDER BY id DESC",
            (iso_ts,),
        ).fetchall()


# ── run_logs ─────────────────────────────────────────────────
def start_run(run_started_at: str, company: Optional[str] = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO run_logs (run_started_at, company, status) VALUES (?, ?, ?)",
            (run_started_at, company, "running"),
        )
        return cur.lastrowid


def finish_run(
    run_id: int,
    *,
    run_finished_at: str,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE run_logs
               SET run_finished_at=?, status=?, error_message=?
               WHERE id=?""",
            (run_finished_at, status, error_message, run_id),
        )


def recent_runs(limit: int = 100) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
