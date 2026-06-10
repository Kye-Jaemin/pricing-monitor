"""SQLite 저장소 — WAL 모드, 짧은 커넥션 (5장).

전체 데이터는 config.DB_PATH 의 SQLite 파일 1개. 환경 이전은 파일 복사로 끝난다.
웹 읽기 + 스케줄러 쓰기 동시 접근을 위해 WAL 모드를 켠다.

업체는 companies(엔티티) + company_sources(1..N개 소스)로 관리한다.
한 업체가 웹 / App Store / Play Store 등 여러 소스를 가질 수 있고,
스냅샷·변동 감지는 소스(source_url)별로 독립 추적한다.
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
    icon_url     TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS company_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name  TEXT NOT NULL,
    source_type   TEXT NOT NULL DEFAULT 'web',   -- web | apple | google | other
    url           TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(company_name, url)
);
CREATE INDEX IF NOT EXISTS idx_sources_company ON company_sources(company_name);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company        TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    source_type    TEXT NOT NULL DEFAULT 'web',
    collected_at   TEXT NOT NULL,
    currency       TEXT NOT NULL,
    raw_text_hash  TEXT NOT NULL,
    raw_text       TEXT,
    payload_json   TEXT NOT NULL,
    confidence     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_company ON snapshots(company, id);
CREATE INDEX IF NOT EXISTS idx_snapshots_source ON snapshots(company, source_url, id);

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

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS feature_categories (
    feature   TEXT PRIMARY KEY,
    category  TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'ai'   -- ai | user
);

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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """구버전 스키마를 현재 구조로 이전(데이터 보존)."""
    # snapshots.source_type 컬럼 보강
    if "source_type" not in _columns(conn, "snapshots"):
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN source_type TEXT NOT NULL DEFAULT 'web'"
        )
    # 구버전 'google'(Play Store) → 'google_play' 로 이름 통일
    for tbl in ("company_sources", "snapshots"):
        if "source_type" in _columns(conn, tbl):
            conn.execute(
                f"UPDATE {tbl} SET source_type='google_play' WHERE source_type='google'"
            )
    # companies.icon_url 컬럼 보강
    if "icon_url" not in _columns(conn, "companies"):
        conn.execute("ALTER TABLE companies ADD COLUMN icon_url TEXT")
    # snapshots.raw_text 컬럼 보강(디버그용 원문)
    if "raw_text" not in _columns(conn, "snapshots"):
        conn.execute("ALTER TABLE snapshots ADD COLUMN raw_text TEXT")
    # 구버전 companies(homepage/pricing_url) → company_sources 로 1회 이전
    ccols = _columns(conn, "companies")
    if "pricing_url" in ccols:
        n = conn.execute("SELECT COUNT(*) FROM company_sources").fetchone()[0]
        if n == 0:
            for row in conn.execute(
                "SELECT name, homepage, pricing_url FROM companies"
            ).fetchall():
                url = row["pricing_url"] or (
                    (row["homepage"].rstrip("/") + "/pricing")
                    if row["homepage"]
                    else None
                )
                if url:
                    conn.execute(
                        """INSERT OR IGNORE INTO company_sources
                           (company_name, source_type, url) VALUES (?, 'web', ?)""",
                        (row["name"], url),
                    )


def init_db() -> None:
    """테이블/인덱스 생성 + 마이그레이션 + WAL 모드 적용."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# ── companies (업체 엔티티) ──────────────────────────────────
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


def add_company(name: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (name,))


def set_company_icon(name: str, icon_url: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE companies SET icon_url=? WHERE name=?", (icon_url, name)
        )


def delete_company(name: str) -> None:
    """업체 전체 삭제: 소스 + 스냅샷 + 변동 이력 + 업체 엔티티 모두 제거."""
    with connect() as conn:
        conn.execute("DELETE FROM company_sources WHERE company_name=?", (name,))
        conn.execute("DELETE FROM snapshots WHERE company=?", (name,))
        conn.execute("DELETE FROM changes WHERE company=?", (name,))
        conn.execute("DELETE FROM companies WHERE name=?", (name,))


# ── company_sources (업체별 소스 URL) ────────────────────────
def list_sources(
    company: Optional[str] = None, active_only: bool = True
) -> list[sqlite3.Row]:
    with connect() as conn:
        conds, params = [], []
        if company:
            conds.append("company_name=?")
            params.append(company)
        if active_only:
            conds.append("active=1")
        q = "SELECT * FROM company_sources"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY company_name COLLATE NOCASE, id"
        return conn.execute(q, params).fetchall()


def add_source(*, company: str, source_type: str, url: str) -> None:
    """업체(없으면 생성)에 소스 추가. 같은 URL 이면 타입 갱신 + 재활성화."""
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (company,))
        conn.execute(
            """INSERT INTO company_sources (company_name, source_type, url, active)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(company_name, url) DO UPDATE SET
                   source_type=excluded.source_type, active=1""",
            (company, source_type, url),
        )


def delete_source(source_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM company_sources WHERE id=?", (source_id,))


def delete_source_data(company: str, source_type: str) -> None:
    """업체의 특정 출처 삭제: 해당 출처의 스냅샷 + 소스 설정 제거."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM snapshots WHERE company=? AND source_type=?",
            (company, source_type),
        )
        conn.execute(
            "DELETE FROM company_sources WHERE company_name=? AND source_type=?",
            (company, source_type),
        )


# ── snapshots (소스별) ───────────────────────────────────────
def latest_snapshot_row(company: str, source_url: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM snapshots
               WHERE company=? AND source_url=? ORDER BY id DESC LIMIT 1""",
            (company, source_url),
        ).fetchone()


def insert_snapshot(
    *,
    company: str,
    source_url: str,
    source_type: str,
    collected_at: str,
    currency: str,
    raw_text_hash: str,
    payload_json: str,
    confidence: str,
    raw_text: Optional[str] = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO snapshots
               (company, source_url, source_type, collected_at, currency,
                raw_text_hash, raw_text, payload_json, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (company, source_url, source_type, collected_at, currency,
             raw_text_hash, raw_text, payload_json, confidence),
        )
        return cur.lastrowid


def latest_snapshots_for_company(company: str) -> list[sqlite3.Row]:
    """한 업체의 소스별 최신 스냅샷."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT s.* FROM snapshots s
            JOIN (
                SELECT source_url, MAX(id) AS max_id
                FROM snapshots WHERE company=? GROUP BY source_url
            ) m ON s.source_url = m.source_url AND s.id = m.max_id
            ORDER BY s.source_type, s.source_url
            """,
            (company,),
        ).fetchall()


def all_latest_by_source() -> list[sqlite3.Row]:
    """전체 (업체 × 소스)별 최신 스냅샷."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT s.* FROM snapshots s
            JOIN (
                SELECT company, source_url, MAX(id) AS max_id
                FROM snapshots GROUP BY company, source_url
            ) m ON s.company = m.company AND s.source_url = m.source_url
               AND s.id = m.max_id
            ORDER BY s.company COLLATE NOCASE, s.source_type
            """
        ).fetchall()


def prune_snapshots(company: str, source_url: str, keep: int) -> None:
    """(업체, 소스)별 최근 keep개만 남기고 오래된 스냅샷 삭제(무한 누적 방지)."""
    if keep <= 0:
        return
    with connect() as conn:
        conn.execute(
            """DELETE FROM snapshots
               WHERE company=? AND source_url=? AND id NOT IN (
                   SELECT id FROM snapshots
                   WHERE company=? AND source_url=? ORDER BY id DESC LIMIT ?
               )""",
            (company, source_url, company, source_url, keep),
        )


def snapshots_for_company(company: str) -> list[sqlite3.Row]:
    """한 업체의 모든 스냅샷(소스 무관, 시간순) — 추이 차트용."""
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


# ── settings (키-값 환경설정) ────────────────────────────────
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )


# ── feature_categories (기능→카테고리 매핑) ──────────────────
def get_feature_categories() -> dict[str, str]:
    with connect() as conn:
        return {
            r["feature"]: r["category"]
            for r in conn.execute("SELECT feature, category FROM feature_categories")
        }


def get_feature_category_rows() -> dict[str, tuple]:
    """feature -> (category, source)."""
    with connect() as conn:
        return {
            r["feature"]: (r["category"], r["source"])
            for r in conn.execute(
                "SELECT feature, category, source FROM feature_categories"
            )
        }


def set_feature_category(feature: str, category: str, source: str = "user") -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO feature_categories (feature, category, source)
               VALUES (?, ?, ?)
               ON CONFLICT(feature) DO UPDATE SET
                   category=excluded.category, source=excluded.source""",
            (feature, category, source),
        )


def recent_runs(limit: int = 100) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ── 데이터 삭제 (수집 상태 화면의 옵션) ──────────────────────
def delete_run_log(run_id: int) -> None:
    """수집 실행 기록 1건 삭제."""
    with connect() as conn:
        conn.execute("DELETE FROM run_logs WHERE id=?", (run_id,))


def clear_run_logs() -> None:
    """수집 실행 기록만 비운다(스냅샷·변동 이력은 유지)."""
    with connect() as conn:
        conn.execute("DELETE FROM run_logs")


def clear_collected_data() -> None:
    """수집 결과 전체 초기화: 스냅샷·변동·실행기록 삭제.

    업체/소스 목록(companies, company_sources)은 유지한다.
    """
    with connect() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM changes")
        conn.execute("DELETE FROM run_logs")
