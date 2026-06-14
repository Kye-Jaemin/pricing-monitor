"""처리 흐름 — run_once() + CLI 진입점 (6장).

웹·APScheduler·OS cron 이 공통으로 호출하는 단일 진입점.
CLI 실행: python -m app.core.pipeline

업체마다 1..N개의 소스(웹/App Store/Play Store)를 가질 수 있고,
각 소스를 독립적으로 수집·추출·diff 한다.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .. import config
from . import diff, extract, fetch, store
from .models import SOURCE_PRIORITY, SOURCE_TYPE_LABELS, PricingSnapshot


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SourceResult:
    company: str
    source_type: str
    status: str  # ok | unchanged | error | low_confidence
    message: str = ""
    changes: int = 0

    @property
    def label(self) -> str:
        return f"{self.company} · {SOURCE_TYPE_LABELS.get(self.source_type, self.source_type)}"


@dataclass
class RunResult:
    started_at: str
    finished_at: str = ""
    results: list[SourceResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status in ("ok", "unchanged"))

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == "error")


def _seed_companies_from_yaml() -> None:
    """DB 의 companies 가 비어 있을 때 companies.yaml 로 1회 시드.

    YAML 은 '초기 목록' 역할만 한다. 이후 추가/삭제는 DB(웹 UI)가 주도한다.
    각 업체의 pricing_url(없으면 homepage/pricing)을 'web' 소스로 등록한다.
    """
    path = Path(config.COMPANIES_FILE).expanduser()
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for entry in data.get("companies", []):
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        url = entry.get("pricing_url")
        if not url and entry.get("homepage"):
            url = entry["homepage"].rstrip("/") + "/pricing"
        store.add_company(name)
        if url:
            store.add_source(company=name, source_type="web", url=url)


def load_companies() -> list[dict]:
    """모니터링 대상을 DB 에서 읽는다(최초엔 YAML 로 시드).

    반환: [{name, sources: [{type, url}, ...]}, ...]
    """
    store.init_db()
    if store.count_companies() == 0:
        _seed_companies_from_yaml()

    companies = store.list_companies(active_only=True)
    if not companies:
        raise ValueError(
            "모니터링할 업체가 없습니다. 웹의 '업체 관리'에서 추가하거나 "
            "companies.yaml 을 채우세요."
        )

    result = []
    for c in companies:
        srcs = store.list_sources(company=c["name"], active_only=True)
        # 우선순위 순서로 수집(공식 홈페이지 > 구글 검색 > App Store > Play Store)
        ordered = sorted(
            srcs, key=lambda s: SOURCE_PRIORITY.get(s["source_type"], 9)
        )
        result.append(
            {
                "name": c["name"],
                "sources": [
                    {"id": s["id"], "type": s["source_type"], "url": s["url"]}
                    for s in ordered
                ],
            }
        )
    return result


def _process_source(
    company: str, source: dict, *, multi_source: bool
) -> SourceResult:
    """업체의 소스 1개를 수집·추출·검증·diff·저장. 예외는 호출자가 run_log 로 기록."""
    source_type = source["type"]
    # US/USD 스토어프론트 보정 후 이 URL 을 일관되게 키로 사용
    source_url = fetch.normalize_us_url(source_type, source["url"])
    collected_at = _utcnow_iso()
    label = SOURCE_TYPE_LABELS.get(source_type, source_type)

    # b. 페이지 렌더링 → 본문 텍스트
    #    구글 검색은 헤드리스 봇 차단이 심해, SerpAPI 키가 있으면 그걸로 가져온다.
    if source_type == "google_search" and config.SERPAPI_KEY:
        page_text = fetch.fetch_google_via_serpapi(source_url)
    else:
        page_text = fetch.fetch_page_text(source_url)

    # c. raw_text_hash — 직전과 동일하면 추출 스킵 (소스별)
    #    단, 직전 결과가 비었거나 신뢰도 low 면 본문이 같아도 재추출한다
    #    (추출 지침 개선분이 반영되도록).
    raw_hash = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    prev_row = store.latest_snapshot_row(company, source_url)
    if prev_row and prev_row["raw_text_hash"] == raw_hash:
        prev_snap_same = PricingSnapshot.from_payload_json(prev_row["payload_json"])
        if prev_row["confidence"] != "low" and prev_snap_same.tiers:
            return SourceResult(
                company, source_type, "unchanged", "본문 동일 — 추출 생략"
            )

    # d. Claude 추출 → e. Pydantic 검증 (실패 시 1회 재시도)
    snapshot: PricingSnapshot | None = None
    last_err: Exception | None = None
    for _ in range(2):
        try:
            raw = extract.extract_pricing(
                company=company,
                source_url=source_url,
                collected_at=collected_at,
                page_text=page_text,
                source_type=source_type,
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

    # 3. 직전 스냅샷과 diff (같은 소스끼리)
    prev_snapshot = (
        PricingSnapshot.from_payload_json(prev_row["payload_json"])
        if prev_row
        else None
    )
    detected = diff.diff_snapshots(
        company,
        prev_snapshot,
        snapshot,
        source_label=label if multi_source else "",
    )

    # 4. 스냅샷 저장 + changes 기록
    store.insert_snapshot(
        company=company,
        source_url=source_url,
        source_type=source_type,
        collected_at=collected_at,
        currency=snapshot.currency,
        raw_text_hash=raw_hash,
        raw_text=page_text[:50000],  # 디버그용 원문(상한)
        payload_json=snapshot.to_payload_json(),
        confidence=snapshot.extraction_confidence,
    )
    store.prune_snapshots(company, source_url, config.SNAPSHOT_RETENTION)
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
    return SourceResult(company, source_type, status, msg, changes=len(detected))


def _is_stale(company: str, source: dict, cutoff_iso: str) -> bool:
    """해당 소스의 최근 수집이 cutoff 이전이거나(=오래됨) 미수집이면 True."""
    url = fetch.normalize_us_url(source["type"], source["url"])
    prev = store.latest_snapshot_row(company, url)
    if prev is None:
        return True  # 한 번도 수집 안 함 → 대상
    return (prev["collected_at"] or "") <= cutoff_iso


def run_once(progress_cb=None, source_ids=None, stale_days=None) -> RunResult:
    """업체 × 소스를 1회 수집. 단일 진입점.

    progress_cb(done:int, total:int, current:str) 로 진행 상황 보고.
    source_ids 가 주어지면 해당 소스만 수집(부분 수집), None 이면 전체.
    stale_days 가 주어지면 '최근 수집이 그 일수를 넘긴(또는 미수집)' 소스만 수집.
    """
    store.init_db()
    run = RunResult(started_at=_utcnow_iso())

    companies = load_companies()
    if source_ids is not None:
        sid_set = {int(x) for x in source_ids}
        companies = [
            {"name": c["name"],
             "sources": [s for s in c["sources"] if s.get("id") in sid_set]}
            for c in companies
        ]
        companies = [c for c in companies if c["sources"]]

    if stale_days and stale_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        companies = [
            {"name": c["name"],
             "sources": [s for s in c["sources"] if _is_stale(c["name"], s, cutoff)]}
            for c in companies
        ]
        companies = [c for c in companies if c["sources"]]

    total = sum(max(len(c["sources"]), 1) for c in companies)
    done = 0

    def report(current: str) -> None:
        if progress_cb:
            try:
                progress_cb(done, total, current)
            except Exception:  # noqa: BLE001
                pass

    report("")

    for entry in companies:
        company = entry["name"]
        sources = entry["sources"]
        multi = len(sources) > 1

        if not sources:
            run.results.append(
                SourceResult(company, "web", "error", "등록된 소스 URL 이 없습니다.")
            )
            done += 1
            report(company)
            continue

        for source in sources:
            source_type = source["type"]
            report(f"{company} · {SOURCE_TYPE_LABELS.get(source_type, source_type)}")
            run_id = store.start_run(_utcnow_iso(), company=company)
            try:
                result = _process_source(company, source, multi_source=multi)
                store.finish_run(
                    run_id,
                    run_finished_at=_utcnow_iso(),
                    status=result.status,
                    error_message=(
                        None if result.status != "error" else result.message
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                # 추출 실패/low: 직전 스냅샷 유지(저장 안 함) + 에러 로그
                result = SourceResult(company, source_type, "error", str(exc))
                store.finish_run(
                    run_id,
                    run_finished_at=_utcnow_iso(),
                    status="error",
                    error_message=str(exc),
                )
            run.results.append(result)
            done += 1
            report("")

    run.finished_at = _utcnow_iso()
    report("")
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
    print("-" * 48)
    run = run_once()
    for r in run.results:
        print(f"[{r.status:>14}] {r.label:<26} {r.message}")
    print("-" * 48)
    print(f"완료: 성공 {run.ok_count} / 에러 {run.error_count} "
          f"({run.started_at} → {run.finished_at})")
    return 1 if run.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
