"""DB → 화면용 데이터 변환 (10장). 프레임워크 무관.

웹 라우트는 얇게 — 모든 화면용 가공 로직은 여기에 둔다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import store
from .models import PricingSnapshot


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _week_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── 1. 현황 (/) ──────────────────────────────────────────────
def overview() -> dict:
    """전체 업체 × 티어 최신 가격표 + 이번 주 변동 배지 수."""
    rows = store.all_latest_snapshots()
    recent = store.changes_since(_week_ago_iso())

    change_count: dict[str, int] = {}
    for c in recent:
        change_count[c["company"]] = change_count.get(c["company"], 0) + 1

    companies = []
    for row in rows:
        snap = PricingSnapshot.from_payload_json(row["payload_json"])
        companies.append(
            {
                "company": snap.company,
                "source_url": snap.source_url,
                "collected_at": row["collected_at"],
                "currency": snap.currency,
                "confidence": row["confidence"],
                "recent_changes": change_count.get(snap.company, 0),
                "tiers": [
                    {
                        "name": t.name,
                        "monthly_price": t.monthly_price,
                        "annual_price_per_month": t.annual_price_per_month,
                        "billing_unit": t.billing_unit,
                        "price_note": t.price_note,
                    }
                    for t in snap.tiers
                ],
            }
        )
    return {
        "companies": companies,
        "total_recent_changes": len(recent),
    }


# ── 2. 업체 상세 (/company/<name>) ───────────────────────────
def company_detail(name: str) -> dict | None:
    rows = store.snapshots_for_company(name)
    if not rows:
        return None

    latest = PricingSnapshot.from_payload_json(rows[-1]["payload_json"])

    # 가격 추이: 티어별 시계열 (Chart.js 용)
    labels = [r["collected_at"] for r in rows]
    series: dict[str, list] = {}
    for r in rows:
        snap = PricingSnapshot.from_payload_json(r["payload_json"])
        present = {t.name: t.monthly_price for t in snap.tiers}
        for tier_name in present:
            series.setdefault(tier_name, [])
    # 각 라벨(회차)마다 모든 티어 값을 정렬해 채운다(없으면 None)
    for tier_name in series:
        series[tier_name] = []
    for r in rows:
        snap = PricingSnapshot.from_payload_json(r["payload_json"])
        present = {t.name: t.monthly_price for t in snap.tiers}
        for tier_name in series:
            series[tier_name].append(present.get(tier_name))

    chart = {
        "labels": labels,
        "datasets": [
            {"label": tier_name, "data": values}
            for tier_name, values in series.items()
        ],
    }

    return {
        "company": latest.company,
        "source_url": latest.source_url,
        "currency": latest.currency,
        "collected_at": rows[-1]["collected_at"],
        "confidence": rows[-1]["confidence"],
        "tiers": [
            {
                "name": t.name,
                "monthly_price": t.monthly_price,
                "annual_price_per_month": t.annual_price_per_month,
                "billing_unit": t.billing_unit,
                "price_note": t.price_note,
                "features": t.features,
                "limits": t.limits,
            }
            for t in latest.tiers
        ],
        "chart": chart,
        "snapshot_count": len(rows),
    }


# ── 3. 변동 로그 (/changes) ──────────────────────────────────
def changes_view(company: str | None = None) -> dict:
    rows = store.recent_changes(company=company, limit=300)
    items = [
        {
            "company": r["company"],
            "detected_at": r["detected_at"],
            "change_type": r["change_type"],
            "tier_name": r["tier_name"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "summary": r["summary"],
        }
        for r in rows
    ]
    all_companies = sorted({r["company"] for r in store.all_latest_snapshots()})
    return {"changes": items, "companies": all_companies, "selected": company}


# ── 4. 수집 상태 (/runs) ─────────────────────────────────────
def runs_view() -> dict:
    rows = store.recent_runs(limit=100)
    items = [
        {
            "company": r["company"],
            "run_started_at": r["run_started_at"],
            "run_finished_at": r["run_finished_at"],
            "status": r["status"],
            "error_message": r["error_message"],
        }
        for r in rows
    ]
    return {"runs": items}


# ── 내부 API 용 ──────────────────────────────────────────────
def latest_snapshots_api() -> list[dict]:
    rows = store.all_latest_snapshots()
    return [
        PricingSnapshot.from_payload_json(r["payload_json"]).model_dump() for r in rows
    ]


def company_history_api(name: str) -> dict | None:
    detail = company_detail(name)
    if detail is None:
        return None
    return {"company": detail["company"], "chart": detail["chart"]}
