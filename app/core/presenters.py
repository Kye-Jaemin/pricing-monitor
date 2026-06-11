"""DB → 화면용 데이터 변환 (10장). 프레임워크 무관.

웹 라우트는 얇게 — 모든 화면용 가공 로직은 여기에 둔다.
업체는 1..N개의 소스(웹/App Store/Play Store)를 가지며, 화면에서는 출처별로 묶어 보여준다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import store
from .models import SOURCE_TYPE_LABELS, SOURCE_TYPES, PricingSnapshot

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}
_STORE_HOSTS = ("apple.com", "play.google.com", "google.com")
PRIORITY_SETTING_KEY = "source_priority"


def _priority_key(company: str | None) -> str:
    return f"priority:{company}" if company else PRIORITY_SETTING_KEY


def get_priority_order(company: str | None = None) -> list[str]:
    """출처 우선순위 순서. 업체 지정 시 해당 업체 설정 → 없으면 전역 → 기본값."""
    raw = store.get_setting(_priority_key(company))
    if not raw and company:
        raw = store.get_setting(PRIORITY_SETTING_KEY)  # 전역으로 폴백
    order: list[str] = []
    if raw:
        try:
            order = [t for t in json.loads(raw) if t in SOURCE_TYPES]
        except (ValueError, TypeError):
            order = []
    for t in SOURCE_TYPES:
        if t not in order:
            order.append(t)
    return order


def set_priority_order(order: list[str], company: str | None = None) -> None:
    cleaned = [t for t in order if t in SOURCE_TYPES]
    store.set_setting(_priority_key(company), json.dumps(cleaned))


def has_company_priority(company: str) -> bool:
    return store.get_setting(_priority_key(company)) is not None


def _priority_map(company: str | None = None) -> dict[str, int]:
    return {t: i for i, t in enumerate(get_priority_order(company))}


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


def _pick_primary(rows: list, pmap: dict | None = None) -> object:
    """우선순위 기반 대표 출처 선정.

    선정 순서(작을수록 우선):
      1) 실제 티어 데이터가 있는 소스 (빈 결과보다 우선)
      2) USD + 신뢰도 low 아님(=usable)
      3) 사용자가 설정한 출처 우선순위
    """
    if pmap is None:
        pmap = _priority_map()

    def key(r):
        snap = PricingSnapshot.from_payload_json(r["payload_json"])
        has_tiers = len(snap.tiers) > 0
        usable = snap.currency.upper() == "USD" and r["confidence"] != "low"
        return (
            0 if has_tiers else 1,
            0 if usable else 1,
            pmap.get(r["source_type"], 99),
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

    # 설정된 소스 전체(스냅샷이 없어도) — 미수집 소스를 현황에 표시하기 위함
    sources_by_company: dict[str, list] = {}
    for s in store.list_sources(active_only=True):
        sources_by_company.setdefault(s["company_name"], []).append(s)

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
        pmap = _priority_map(company_name)
        primary = _pick_primary(srows, pmap)
        snap = PricingSnapshot.from_payload_json(primary["payload_json"])
        primary_tiers = _tier_dicts(snap)

        others = sorted(
            (r for r in srows if r["source_url"] != primary["source_url"]),
            key=lambda r: pmap.get(r["source_type"], 99),
        )

        # 비대표 소스도 현황에 함께 노출(티어가 비어도 표시 — 구글 검색 등 누락 방지)
        extra_sources = []
        free_trial = snap.free_trial
        snap_types = {r["source_type"] for r in srows}
        for r in others:
            osnap = PricingSnapshot.from_payload_json(r["payload_json"])
            if not free_trial and osnap.free_trial:
                free_trial = osnap.free_trial
            tiers = _tier_dicts(osnap)
            extra_sources.append(
                {
                    "source_type": r["source_type"],
                    "source_label": _src_label(r["source_type"]),
                    "source_url": r["source_url"],
                    "confidence": r["confidence"],
                    "currency": osnap.currency,
                    "status": "ok" if tiers else "empty",
                    "tiers": tiers,
                }
            )

        # 설정돼 있으나 아직 스냅샷이 없는 소스 → '미수집'으로 표시
        for cfg in sources_by_company.get(company_name, []):
            if cfg["source_type"] not in snap_types:
                extra_sources.append(
                    {
                        "source_type": cfg["source_type"],
                        "source_label": _src_label(cfg["source_type"]),
                        "source_url": cfg["url"],
                        "confidence": None,
                        "currency": None,
                        "status": "uncollected",
                        "tiers": [],
                    }
                )

        extra_sources.sort(key=lambda e: pmap.get(e["source_type"], 99))

        has_free_tier = any(t["is_free"] for t in primary_tiers) or any(
            t["is_free"] for es in extra_sources for t in es["tiers"]
        )

        # 업체별 대표 출처 선택용 옵션(설정된 소스 타입, 없으면 스냅샷 타입)
        opt_types: list[str] = []
        for cfg in sources_by_company.get(company_name, []):
            if cfg["source_type"] not in opt_types:
                opt_types.append(cfg["source_type"])
        if not opt_types:
            for r in srows:
                if r["source_type"] not in opt_types:
                    opt_types.append(r["source_type"])
        opt_types.sort(key=lambda tp: pmap.get(tp, 99))
        source_options = [{"type": tp, "label": _src_label(tp)} for tp in opt_types]

        companies.append(
            {
                "company": company_name,
                "icon": _company_icon(
                    icon_map.get(company_name), [r["source_url"] for r in srows]
                ),
                "source_options": source_options,
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
    priority = [{"type": t, "label": _src_label(t)} for t in get_priority_order()]
    return {
        "companies": companies,
        "total_recent_changes": len(recent),
        "priority": priority,
    }


# ── 2. 업체 상세 (/company/<name>) ───────────────────────────
def company_detail(name: str) -> dict | None:
    all_rows = store.snapshots_for_company(name)
    latest_rows = store.latest_snapshots_for_company(name)
    if not latest_rows:
        return None

    pmap = _priority_map(name)
    primary = _pick_primary(latest_rows, pmap)
    primary_url = primary["source_url"]

    # 출처별 현재 티어 블록 (우선순위 순서, 대표 표시)
    ordered_rows = sorted(
        latest_rows, key=lambda r: pmap.get(r["source_type"], 99)
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
        "priority": [
            {"type": t, "label": _src_label(t)} for t in get_priority_order(name)
        ],
        "has_custom_priority": has_company_priority(name),
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


def _effective_category(feature: str, cat_map: dict[str, str]) -> str:
    """기능의 카테고리: 저장된 매핑(AI/사용자) 우선, 없으면 키워드 휴리스틱."""
    from . import compare as cmp

    if feature in cat_map:
        return cat_map[feature]
    return cmp.categorize_feature(feature)[0]


def _company_paid_data(name: str):
    """업체의 (산점도 점들, 유료 기능 목록, 최저 유료 월가격).

    같은 플랜의 결제주기 티어(Monthly/Annual 등)는 (월, 연환산) 한 점으로 병합한다.
    """
    from . import compare as cmp

    rows = store.latest_snapshots_for_company(name)
    pts, feats, seen = [], [], set()
    cheapest = None
    for row in rows:
        snap = PricingSnapshot.from_payload_json(row["payload_json"])

        # 결제주기 변형 티어 병합 → 한 점, 나머지는 각자
        merged_pts, remaining = cmp.merge_billing_points(snap.tiers)
        plot = merged_pts + [
            {
                "x": (t.monthly_price if t.monthly_price is not None
                      else t.annual_price_per_month),
                "y": (t.annual_price_per_month if t.annual_price_per_month is not None
                      else t.monthly_price),
                "tier": t.name,
            }
            for t in remaining
            if (t.monthly_price is not None or t.annual_price_per_month is not None)
        ]
        for p in plot:
            key = (p["x"], p["y"], p["tier"])
            if key not in seen:
                seen.add(key)
                pts.append(p)

        for t in snap.tiers:
            m, a = t.monthly_price, t.annual_price_per_month
            is_free = (m == 0) or ("free" in t.name.lower()) or ("무료" in t.name)
            if not is_free:
                eff = a if a is not None else m
                if eff is not None and eff > 0:
                    cheapest = eff if cheapest is None else min(cheapest, eff)
                for f in t.features:
                    if f not in feats:
                        feats.append(f)
    return pts, feats, cheapest


# ── 업체간 비교 (/compare) ───────────────────────────────────
def compare(names: list[str]) -> dict:
    """선택 업체 비교: 가격 산점도 + 업체별(카테고리·기능·월가격) + 카테고리 매트릭스.

    카테고리는 저장된 동적 매핑(AI/사용자)을 우선 사용하고, 없으면 키워드 추정.
    """
    from . import compare as cmp

    cat_map = store.get_feature_categories()
    icon_map = {c["name"]: c["icon_url"] for c in store.list_companies(active_only=False)}

    chosen = []
    scatter = []
    per_company = []          # 업체별 카테고리·기능·월가격
    matrix: dict[str, dict[str, int]] = {}
    scores: dict[str, int] = {}
    all_features: set[str] = set()

    for name in names:
        if not store.latest_snapshots_for_company(name):
            continue
        pts, feats, cheapest = _company_paid_data(name)
        scatter.append({"label": name, "data": pts})

        cat_feats: dict[str, list[str]] = {}
        for f in feats:
            c = _effective_category(f, cat_map)
            cat_feats.setdefault(c, []).append(f)
            matrix.setdefault(c, {})[name] = matrix.get(c, {}).get(name, 0) + 1
            all_features.add(f)

        per_company.append(
            {
                "company": name,
                "icon": _company_icon(icon_map.get(name), []),
                "monthly_price": cheapest,
                "categories": [
                    {"category": c, "features": fs}
                    for c, fs in sorted(cat_feats.items())
                ],
            }
        )
        scores[name] = sum(
            cmp.WEIGHTS.get(c, cmp.DEFAULT_WEIGHT) * len(fs)
            for c, fs in cat_feats.items()
        )
        chosen.append(name)

    used_cats = sorted(
        matrix.keys(),
        key=lambda c: (-cmp.WEIGHTS.get(c, cmp.DEFAULT_WEIGHT), c),
    )
    matrix_rows = [
        {
            "category": c,
            "counts": {n: matrix[c].get(n, 0) for n in chosen},
        }
        for c in used_cats
    ]
    ranking = sorted(
        ({"company": n, "score": scores[n], "icon": _company_icon(icon_map.get(n), [])}
         for n in chosen),
        key=lambda x: x["score"],
        reverse=True,
    )
    editable = [
        {"feature": f, "category": _effective_category(f, cat_map),
         "assigned": f in cat_map}
        for f in sorted(all_features)
    ]

    return {
        "companies": chosen,
        "scatter": {"datasets": scatter},
        "per_company": per_company,
        "matrix": matrix_rows,
        "ranking": ranking,
        "editable": editable,
        "all_companies": sorted(c["name"] for c in store.list_companies(active_only=True)),
    }


def distinct_paid_features(names: list[str]) -> list[str]:
    """선택 업체들의 유료 기능 합집합(AI 카테고리 분류용)."""
    out: list[str] = []
    for name in names:
        _, feats, _ = _company_paid_data(name)
        for f in feats:
            if f not in out:
                out.append(f)
    return out


# ── 저장된 비교 카드 (수동 저장 + 저장 시점 고정) ─────────────
def save_comparison(names: list[str], title: str = "") -> int | None:
    """현재 비교 결과를 저장 시점 그대로 카드로 저장. 유효 업체가 없으면 None."""
    data = compare(names)
    chosen = data.get("companies") or []
    if not chosen:
        return None
    title = (title or "").strip() or " · ".join(chosen)
    return store.save_comparison_card(
        title=title,
        companies_json=json.dumps(chosen, ensure_ascii=False),
        payload_json=json.dumps(data, ensure_ascii=False),
    )


def saved_comparison_cards() -> list[dict]:
    """저장된 비교 카드 목록(최신순) — 화면 카드용 요약."""
    out: list[dict] = []
    for row in store.list_comparison_cards():
        try:
            companies = json.loads(row["companies_json"])
        except (ValueError, TypeError):
            companies = []
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "companies": companies,
                "created_at": row["created_at"],
            }
        )
    return out


def load_comparison_card(card_id: int) -> dict | None:
    """저장된 카드의 고정 스냅샷 데이터를 화면용으로 복원. 없으면 None."""
    row = store.get_comparison_card(card_id)
    if row is None:
        return None
    try:
        data = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return None
    # 선택 목록(체크박스)은 현재 업체 기준으로 갱신해 새 비교 시작이 가능하도록.
    data["all_companies"] = sorted(
        c["name"] for c in store.list_companies(active_only=True)
    )
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "data": data,
    }


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
