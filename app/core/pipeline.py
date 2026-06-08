"""처리 흐름 — run_once() + CLI 진입점 (6장).

웹·APScheduler·OS cron 이 공통으로 호출하는 단일 진입점.
CLI 실행: python -m app.core.pipeline
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .. import config
from . import diff, extract, fetch, store
from .models import PricingSnapshot


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CompanyResult:
    company: str
    status: str  # ok | unchanged | error | low_confidence
    message: str = ""
    changes: int = 0


@dataclass
class RunResult:
    started_at: str
    finished_at: str = ""
    results: list[CompanyResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status in ("ok", "unchanged"))

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == "error")


def _seed_companies_from_yaml() -> None:
    """DB 의 companies 가 비어 있을 때 companies.yaml 로 1회 시드.

    YAML 은 '초기 목록' 역할만 한다. 이후 추가/삭제는 DB(웹 UI)가 주도한다.
    YAML 이 없으면 조용히 건너뛴다(웹에서 직접 추가하면 됨).
    """
    path = Path(config.COMPANIES_FILE).expanduser()
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for entry in data.get("companies", []):
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        store.add_company(
            name=name,
            homepage=entry.get("homepage"),
            pricing_url=entry.get("pricing_url"),
        )


def load_companies() -> list[dict]:
    """모니터링 대상을 DB 에서 읽는다(최초엔 YAML 로 시드).

    웹 UI 의 추가/삭제가 DB 에 반영되므로, 어느 환경에서나 동일하게 동작한다.
    """
    store.init_db()
    if store.count_companies() == 0:
        _seed_companies_from_yaml()
    rows = store.list_companies(active_only=True)
    if not rows:
        raise ValueError(
            "모니터링할 업체가 없습니다. 웹의 '업체 관리'에서 추가하거나 "
            "companies.yaml 을 채우세요."
        )
    return [
        {
            "name": r["name"],
            "homepage": r["homepage"],
            "pricing_url": r["pricing_url"],
        }
        for r in rows
    ]


def _resolve_pricing_url(entry: dict) -> str:
    url = entry.get("pricing_url")
    if url:
        return url
    homepage = entry.get("homepage")
    if homepage:
        return homepage.rstrip("/") + "/pricing"
    raise ValueError(
        f"{entry.get('name')}: pricing_url 또는 homepage 중 하나는 필요합니다."
    )


def _process_company(entry: dict) -> CompanyResult:
    """업체 1개 수집·추출·검증·diff·저장. 예외는 호출자가 run_log 로 기록."""
    company = entry["name"]
    source_url = _resolve_pricing_url(entry)
    collected_at = _utcnow_iso()

    # b. 페이지 렌더링 → 본문 텍스트
    page_text = fetch.fetch_page_text(source_url)

    # c. raw_text_hash — 직전과 동일하면 추출 스킵
    raw_hash = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    prev_row = store.latest_snapshot_row(company)
    if prev_row and prev_row["raw_text_hash"] == raw_hash:
        return CompanyResult(company, "unchanged", "본문 동일 — 추출 생략")

    # d. Claude 추출 → e. Pydantic 검증 (실패 시 1회 재시도)
    snapshot: PricingSnapshot | None = None
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            raw = extract.extract_pricing(
                company=company,
                source_url=source_url,
                collected_at=collected_at,
                page_text=page_text,
            )
            snapshot = PricingSnapshot.model_validate(raw)
            break
        except Exception as exc:  # noqa: BLE001  (추출/검증 실패 모두 포함)
            last_err = exc
    if snapshot is None:
        raise RuntimeError(f"추출/검증 실패(2회): {last_err}")

    # f. currency != USD → confidence=low (임의 환산 금지)
    note = ""
    if snapshot.currency.upper() != "USD":
        snapshot.extraction_confidence = "low"
        note = f"비-USD({snapshot.currency}) 감지 → 검수 필요"

    # 3. 직전 스냅샷과 diff
    prev_snapshot = (
        PricingSnapshot.from_payload_json(prev_row["payload_json"])
        if prev_row
        else None
    )
    detected = diff.diff_snapshots(company, prev_snapshot, snapshot)

    # 4. 스냅샷 저장 + changes 기록
    store.insert_snapshot(
        company=company,
        source_url=source_url,
        collected_at=collected_at,
        currency=snapshot.currency,
        raw_text_hash=raw_hash,
        payload_json=snapshot.to_payload_json(),
        confidence=snapshot.extraction_confidence,
    )
    for ch in detected:
        store.insert_change(
            company=company,
            detected_at=collected_at,
            change_type=ch.change_type,
            tier_name=ch.tier_name,
            field=ch.field,
            old_value=ch.old_value,
            new_value=ch.new_value,
            summary=ch.summary,
        )

    status = "low_confidence" if snapshot.extraction_confidence == "low" else "ok"
    msg = note or (f"{len(detected)}건 변동" if detected else "변동 없음")
    return CompanyResult(company, status, msg, changes=len(detected))


def run_once() -> RunResult:
    """전체 업체를 1회 수집. 단일 진입점."""
    store.init_db()
    run = RunResult(started_at=_utcnow_iso())

    companies = load_companies()
    for entry in companies:
        company = entry.get("name", "?")
        run_id = store.start_run(_utcnow_iso(), company=company)
        try:
            result = _process_company(entry)
            store.finish_run(
                run_id,
                run_finished_at=_utcnow_iso(),
                status=result.status,
                error_message=None if result.status != "error" else result.message,
            )
        except Exception as exc:  # noqa: BLE001
            # 추출 실패/low: 직전 스냅샷 유지(저장 안 함) + 에러 로그
            result = CompanyResult(company, "error", str(exc))
            store.finish_run(
                run_id,
                run_finished_at=_utcnow_iso(),
                status="error",
                error_message=str(exc),
            )
        run.results.append(result)

    run.finished_at = _utcnow_iso()
    return run


def main() -> int:
    # Windows 콘솔(cp949)에서 한글/em-dash 출력이 깨지지 않도록 UTF-8 강제
    for stream in (sys.stdout, sys.stderr):
        reconfig = getattr(stream, "reconfigure", None)
        if reconfig:
            try:
                reconfig(encoding="utf-8")
            except (ValueError, OSError):
                pass

    print("=== Pricing Monitor: run_once ===")
    print(f"DB_PATH      = {config.DB_PATH}")
    print(f"MODEL        = {config.ANTHROPIC_MODEL}")
    print(f"COMPANIES    = {config.COMPANIES_FILE}")
    print("-" * 40)
    run = run_once()
    for r in run.results:
        print(f"[{r.status:>14}] {r.company:<20} {r.message}")
    print("-" * 40)
    print(f"완료: 성공 {run.ok_count} / 에러 {run.error_count} "
          f"({run.started_at} → {run.finished_at})")
    return 1 if run.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
