"""DB → 화면용 데이터 변환 (10장). 프레임워크 무관.

웹 라우트는 얇게 — 모든 화면용 가공 로직은 여기에 둔다.
업체는 1..N개의 소스(웹/App Store/Play Store)를 가지며, 화면에서는 출처별로 묶어 보여준다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import store
from .models import SOURCE_PRIORITY, SOURCE_TYPE_LABELS, PricingSnapshot

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}
_STORE_HOSTS = ("apple.com", "play.google.com", "google.com")


def _favicon(url: str) -> str | None:
    """도메인 파비콘(구글 파비콘 서비스) URL."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    return f"https://www.google.com/s2/favicons?domain={host}&sz=64"


def _company_icon(icon_url: str | None, source_urls: list[str]) -> str | None:
    """업체 아이콘: 저장된 icon_url(앱 아이콘) 우선, 없으면 브랜드 도메인 파비콘."""
    if icon_url:
        return icon_url
    # 스토어/검색이 아닌 브랜드 도메인을 우선
    for u in source_urls:
        host = urlparse(u).netloc.lower() if u else ""
        if host and not any(s in host for s in _STORE_HOSTS):
            return _favicon(u)
    for u in source_urls:
        if u:
            fav = _favicon(u)
            if fav:
                return fav
    return None


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


def _pick_primary(rows: list) -> object:
    """우선순위 기반 대표 출처 선정.

    선정 순서(작을수록 우선):
      1) 실제 티어 데이터가 있는 소스 (빈 결과보다 우선)
      2) USD + 신뢰도 low 아님(=usable)
      3) 소스 우선순위 (공식 홈페이지 > 구글 검색 > App Store > Play Store)
    → 공식 홈페이지가 비었거나 실패하면, 데이터가 있는 구글 검색 결과가 대표가 된다.
    """
    def key(r):
        snap = PricingSnapshot.from_payload_json(r["payload_json"])
        has_tiers = len(snap.tiers) > 0
        usable = snap.currency.upper() == "USD" and r["confidence"] != "low"
        return (
            0 if has_tiers else 1,
            0 if usable else 1,
            SOURCE_PRIORITY.get(r["source_type"], 9),
        )

    return sorted(rows, key=key)[0]


def _feature_matrix(tiers: list) -> dict:
    """무료/유료 티어별 기능 차이 비교표.

    전체 기능의 합집합을 행으로, 각 티어를 열로 두고 보유 여부를 표시한다.
    """
    all_feats: list[str] = []
    for t in tiers:
        for f in t.features:
            if f not in all_feats:
                all_feats.append(f)
    return {
        "features": all_feats,
        "tiers": [
            {
                "name": t.name,
                "monthly_price": t.monthly_price,
                "price_note": t.price_note,
                "is_free": (t.monthly_price == 0)
                or ("free" in t.name.lower())
                or ("무료" in t.name),
                "has": {f: (f in t.features) for f in all_feats},
            }
            for t in tiers
        ],
    }


# ── 1. 현황 (/) ──────────────────────────────────────────────
def overview() -> dict:
    """전체 업체의 대표 출처 가격표 + 이번 주 변동 배지 수.

    대표 출처는 우선순위(공식 홈페이지 > 구글 검색 > App Store > Play Store)로
    선정하고, 나머지 출처는 보조로 명시한다.
    """
    rows = store.all_latest_by_source()
    recent = store.changes_since(_week_ago_iso())

    change_count: dict[str, int] = {}
    for c in recent:
        change_count[c["company"]] = change_count.get(c["company"], 0) + 1

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["company"], []).append(row)

    icon_map = {c["name"]: c["icon_url"] for c in store.list_companies(active_only=False)}

    def _tier_dicts(snap):
        return [
            {
                "name": t.name,
                "monthly_price": t.monthly_price,
                "annual_price_per_month": t.annual_price_per_month,
                "billing_unit": t.billing_unit,
                "price_note": t.price_note,
                "is_free": (t.monthly_price == 0)
                or ("free" in t.name.lower())
                or ("무료" in t.name),
            }
            for t in snap.tiers
        ]

    companies = []
    for company_name in sorted(grouped, key=str.lower):
        srows = grouped[company_name]
        primary = _pick_primary(srows)
        snap = PricingSnapshot.from_payload_json(primary["payload_json"])
        primary_tiers = _tier_dicts(snap)

        others = sorted(
            (r for r in srows if r["source_url"] != primary["source_url"]),
            key=lambda r: SOURCE_PRIORITY.get(r["source_type"], 9),
        )

        # 비대표 소스도 현황에 함께 노출(티어가 비어도 표시 — 구글 검색 등 누락 방지)
        extra_sources = []
        free_trial = snap.free_trial
        for r in others:
            osnap = PricingSnapshot.from_payload_json(r["payload_json"])
            if not free_trial and osnap.free_trial:
                free_trial = osnap.free_trial
            extra_sources.append(
                {
                    "source_type": r["source_type"],
                    "source_label": _src_label(r["source_type"]),
                    "source_url": r["source_url"],
                    "confidence": r["confidence"],
                    "currency": osnap.currency,
                    "tiers": _tier_dicts(osnap),
                }
            )

        has_free_tier = any(t["is_free"] for t in primary_tiers) or any(
            t["is_free"] for es in extra_sources for t in es["tiers"]
        )

        companies.append(
            {
                "company": company_name,
                "icon": _company_icon(
                    icon_map.get(company_name), [r["source_url"] for r in srows]
                ),
                "primary_source_type": primary["source_type"],
                "primary_source_label": _src_label(primary["source_type"]),
                "primary_source_url": primary["source_url"],
                "collected_at": primary["collected_at"],
                "currency": snap.currency,
                "confidence": primary["confidence"],
                "recent_changes": change_count.get(company_name, 0),
                "source_count": len(srows),
                "other_sources": [_src_label(r["source_type"]) for r in others],
                "free_trial": free_trial,
                "has_free_tier": has_free_tier,
                "tiers": primary_tiers,
                "extra_sources": extra_sources,
            }
        )
    return {"companies": companies, "total_recent_changes": len(recent)}


# ── 2. 업체 상세 (/company/<name>) ───────────────────────────
def company_detail(name: str) -> dict | None:
    all_rows = store.snapshots_for_company(name)
    latest_rows = store.latest_snapshots_for_company(name)
    if not latest_rows:
        return None

    primary = _pick_primary(latest_rows)
    primary_url = primary["source_url"]

    # 출처별 현재 티어 블록 (우선순위 순서, 대표 표시)
    ordered_rows = sorted(
        latest_rows, key=lambda r: SOURCE_PRIORITY.get(r["source_type"], 9)
    )
    sources = []
    for row in ordered_rows:
        snap = PricingSnapshot.from_payload_json(row["payload_json"])
        sources.append(
            {
                "source_type": row["source_type"],
                "source_label": _src_label(row["source_type"]),
                "source_url": row["source_url"],
                "is_primary": row["source_url"] == primary_url,
                "currency": snap.currency,
                "confidence": row["confidence"],
                "collected_at": row["collected_at"],
                "free_trial": snap.free_trial,
                "feature_matrix": _feature_matrix(snap.tiers),
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

    comp = next(
        (c for c in store.list_companies(active_only=False) if c["name"] == name), None
    )
    icon = _company_icon(
        comp["icon_url"] if comp else None,
        [r["source_url"] for r in latest_rows],
    )

    return {
        "company": name,
        "icon": icon,
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
            "id": r["id"],
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
                "icon": _company_icon(c["icon_url"], [s["url"] for s in srcs]),
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
