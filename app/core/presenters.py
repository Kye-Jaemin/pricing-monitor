"""DB → 화면용 데이터 변환 (10장). 프레임워크 무관.

웹 라우트는 얇게 — 모든 화면용 가공 로직은 여기에 둔다.
업체는 1..N개의 소스(웹/App Store/Play Store)를 가지며, 화면에서는 출처별로 묶어 보여준다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import store
from .models import SOURCE_TYPE_LABELS, PricingSnapshot

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _week_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _worst_confidence(confs: list[str]) -> str:
    if not confs:
        return "—"
    return min(confs, key=lambda c: _CONF_RANK.get(c, 1))


def _src_label(source_type: str) -> str:
    return SOURCE_TYPE_LABELS.get(source_type, source_type)


# ── 1. 현황 (/) ──────────────────────────────────────────────
def overview() -> dict:
    """전체 업체 × (소스별) 최신 가격표 + 이번 주 변동 배지 수."""
    rows = store.all_latest_by_source()
    recent = store.changes_since(_week_ago_iso())

    change_count: dict[str, int] = {}
    for c in recent:
        change_count[c["company"]] = change_count.get(c["company"], 0) + 1

    # 업체별로 소스 스냅샷을 모은다
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["company"], []).append(row)

    companies = []
    for company_name in sorted(grouped, key=str.lower):
        srows = grouped[company_name]
        tiers, confs, currencies = [], [], set()
        latest_collected = ""
        for row in srows:
            snap = PricingSnapshot.from_payload_json(row["payload_json"])
            confs.append(row["confidence"])
            currencies.add(snap.currency)
            latest_collected = max(latest_collected, row["collected_at"])
            for t in snap.tiers:
                tiers.append(
                    {
                        "name": t.name,
                        "monthly_price": t.monthly_price,
                        "annual_price_per_month": t.annual_price_per_month,
                        "billing_unit": t.billing_unit,
                        "price_note": t.price_note,
                        "source_type": row["source_type"],
                        "source_label": _src_label(row["source_type"]),
                    }
                )
        companies.append(
            {
                "company": company_name,
                "collected_at": latest_collected,
                "currency": ", ".join(sorted(currencies)) if currencies else "—",
                "confidence": _worst_confidence(confs),
                "recent_changes": change_count.get(company_name, 0),
                "source_count": len(srows),
                "multi_source": len(srows) > 1,
                "tiers": tiers,
            }
        )
    return {"companies": companies, "total_recent_changes": len(recent)}


# ── 2. 업체 상세 (/company/<name>) ───────────────────────────
def company_detail(name: str) -> dict | None:
    all_rows = store.snapshots_for_company(name)
    latest_rows = store.latest_snapshots_for_company(name)
    if not latest_rows:
        return None

    # 출처별 현재 티어 블록
    sources = []
    for row in latest_rows:
        snap = PricingSnapshot.from_payload_json(row["payload_json"])
        sources.append(
            {
                "source_type": row["source_type"],
                "source_label": _src_label(row["source_type"]),
                "source_url": row["source_url"],
                "currency": snap.currency,
                "confidence": row["confidence"],
                "collected_at": row["collected_at"],
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
                    for t in snap.tiers
                ],
            }
        )

    # 가격 추이: "티어 · 출처" 시계열 (Chart.js)
    multi = len({r["source_type"] for r in latest_rows}) > 1
    labels = [r["collected_at"] for r in all_rows]
    series: dict[str, list] = {}
    for r in all_rows:
        snap = PricingSnapshot.from_payload_json(r["payload_json"])
        suffix = f" · {_src_label(r['source_type'])}" if multi else ""
        for t in snap.tiers:
            series.setdefault(f"{t.name}{suffix}", [])
    for r in all_rows:
        snap = PricingSnapshot.from_payload_json(r["payload_json"])
        suffix = f" · {_src_label(r['source_type'])}" if multi else ""
        present = {f"{t.name}{suffix}": t.monthly_price for t in snap.tiers}
        for key in series:
            series[key].append(present.get(key))

    chart = {
        "labels": labels,
        "datasets": [{"label": k, "data": v} for k, v in series.items()],
    }

    return {
        "company": name,
        "sources": sources,
        "chart": chart,
        "snapshot_count": len(all_rows),
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
    all_companies = sorted(r["name"] for r in store.list_companies(active_only=True))
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


# ── 업체 관리 (/companies) ───────────────────────────────────
def companies_admin() -> dict:
    companies = []
    for c in store.list_companies(active_only=True):
        srcs = store.list_sources(company=c["name"], active_only=True)
        companies.append(
            {
                "name": c["name"],
                "created_at": c["created_at"],
                "sources": [
                    {
                        "id": s["id"],
                        "source_type": s["source_type"],
                        "source_label": _src_label(s["source_type"]),
                        "url": s["url"],
                        "created_at": s["created_at"],
                    }
                    for s in srcs
                ],
            }
        )
    return {"companies": companies}


# ── 내부 API 용 ──────────────────────────────────────────────
def latest_snapshots_api() -> list[dict]:
    rows = store.all_latest_by_source()
    out = []
    for r in rows:
        d = PricingSnapshot.from_payload_json(r["payload_json"]).model_dump()
        d["source_type"] = r["source_type"]
        out.append(d)
    return out


def company_history_api(name: str) -> dict | None:
    detail = company_detail(name)
    if detail is None:
        return None
    return {"company": detail["company"], "chart": detail["chart"]}
