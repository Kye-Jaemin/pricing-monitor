"""DB → 화면용 데이터 변환 (10장). 프레임워크 무관.

웹 라우트는 얇게 — 모든 화면용 가공 로직은 여기에 둔다.
업체는 1..N개의 소스(웹/App Store/Play Store)를 가지며, 화면에서는 출처별로 묶어 보여준다.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from .. import config
from . import store
from .models import SOURCE_TYPE_LABELS, SOURCE_TYPES, PricingSnapshot

# 기능 텍스트 정규화에서 무시할 수식어/조사(같은 기능의 표현 차이를 흡수)
_FEATURE_FILLER = {
    "a", "an", "the", "with", "for", "and", "or", "of", "to", "your", "you",
    "all", "any", "new", "get", "unlimited", "advanced", "premium", "pro",
    "plus", "basic", "full", "complete", "extra", "additional", "powered",
    "enabled", "included", "per", "more", "access",
    # 수량/상한 게이팅 수식어(같은 기능의 '제한판' 표현 차이를 흡수)
    "capped", "limited", "metered", "standard", "lite", "starter", "essential",
}


def _stem(t: str) -> str:
    """경량 어간 추출: -ing/-er/-ion/-ment/-s 등 어미 제거 + 끝 중복자음 정리.
    scan/scanner/scanning → scan, track/tracker/tracking → track 처럼 묶기 위함."""
    for suf in ("ings", "ing", "ers", "er", "ions", "ion", "ments", "ment", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)]
            break
    if len(t) > 3 and t[-1] == t[-2] and t[-1] not in "aeiou":
        t = t[:-1]               # scann → scan, trackk → track
    return t


def _normalize_feature(text: str) -> str:
    """기능 텍스트를 정규화한 canonical 키. 대소문자·괄호·구두점·수식어·어순·
    어형(복수/-ing/-er 등) 차이를 흡수해, 표현이 달라도 같은 기능이면 같은 키."""
    s = (text or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)                # 괄호 주석 제거
    s = re.sub(r"[^a-z0-9가-힣\s]", " ", s)        # 구두점 → 공백
    toks = []
    for t in s.split():
        if t in _FEATURE_FILLER:
            continue
        toks.append(_stem(t))
    return " ".join(sorted(set(toks))) or s.strip()

# "이전 티어/플랜의 모든 기능 포함" 류 안내 문구(실제 기능이 아님)
#   포함을 뜻하는 명사: features / capabilities / plans (예: 'All Pro plan capabilities')
_INCL_NOUN = r"(?:features?|capabilit(?:y|ies)|plans?)"
_INCLUSION_RE = re.compile(
    r"everything\s+(in|from|plus|else)\b"             # everything in/from Pro …
    r"|includ\w*\s+(all|everything)\b"                # includes all / including everything
    r"|all\b.{0,40}\b" + _INCL_NOUN + r"\b.{0,25}\b(includ\w+|plus)\b"  # All X features included
    r"|^all\b.{0,40}\b" + _INCL_NOUN + r"\b[\s.)\]]*$"  # 'All Pro plan capabilities' (동사 없는 번들)
    r"|\ball\s+" + _INCL_NOUN + r"\b\s*(of|in|from|across)\b"  # 'All features of AI Ultra $100 tier'
    r"|\b(all|everything)\b.{0,30}\b(previous|prior|lower|preceding)\b"  # all previous tier
    r"|모든\s*기능.{0,12}포함"                         # 모든 기능 … 포함
    r"|포함.{0,12}모든\s*기능"                         # … 모든 기능 포함
    r"|이전.{0,12}(티어|플랜|요금제).{0,15}기능",       # 이전 티어/플랜 기능
    re.IGNORECASE,
)


def _is_inclusion_phrase(f: str) -> bool:
    """'이전 티어의 모든 기능 포함'처럼 상위 티어가 하위 티어를 포함한다는
    안내 문구인지 판별(기능 포지셔닝/분석에서 제외하기 위함)."""
    return bool(_INCLUSION_RE.search(f or ""))


# 플랜/티어 '이름'만으로 이뤄진 항목(실제 기능이 아니라 '그 플랜 통째로 포함'을 뜻함)
#   예: 'Home Premium', 'Gold', 'Family Plan', 'All Pro features'
# 토큰이 전부 아래 집합에 속하면 플랜 이름으로 보고 분류에서 제외한다. 설명형 명사가
# 하나라도 섞이면(예: 'Premium support', 'Team chat') 실제 기능이므로 남긴다.
_PLAN_NAME_WORDS = {
    "plan", "plans", "tier", "tiers", "membership", "subscription", "edition",
    "package", "bundle", "version",
    "plus", "pro", "premium", "gold", "silver", "bronze", "platinum", "diamond",
    "basic", "standard", "starter", "essential", "essentials", "lite", "light",
    "advanced", "ultimate", "elite", "max", "mini", "deluxe",
    "home", "family", "personal", "individual", "business", "enterprise",
    "team", "teams", "group", "org", "organization",
    "free", "paid", "trial", "all", "everything", "included",
    "feature", "features", "unlimited", "full", "complete",
    "and", "the", "of", "for", "with", "your",
    # 한국어
    "플랜", "요금제", "티어", "멤버십", "구독", "프리미엄", "기본", "무료", "유료",
    "전체", "모든", "기능", "포함", "패키지",
}


def _is_plan_name(f: str) -> bool:
    """'Home Premium' / 'Gold' / 'Family Plan'처럼 플랜·티어 이름만으로 된
    항목인지 판별(실제 기능이 아니라 '그 플랜 포함'을 의미)."""
    s = re.sub(r"[^a-z0-9가-힣\s]", " ", (f or "").lower())
    toks = s.split()
    return bool(toks) and all(t in _PLAN_NAME_WORDS for t in toks)


# 광고 관련 기능(유료 혜택일 뿐 차별화 기능이 아님) — 'ad/ads/광고' 단어 경계 매칭
_AD_RE = re.compile(
    r"\bads?\b"            # ad / ads (단어)
    r"|\bad[-\s]?free\b"   # ad-free
    r"|advertis\w*"        # advertising / advertisement
    r"|광고",
    re.IGNORECASE,
)


def _is_ad_feature(f: str) -> bool:
    """'Ad-free' / 'No ads' / '광고 제거'처럼 광고 관련 기능인지 판별."""
    return bool(_AD_RE.search(f or ""))


# 무료 체험 안내(기능이 아니라 메타 정보) — 'free trial available' 등
_TRIAL_RE = re.compile(
    r"\bfree\s+trial\b"                       # free trial (available/included …)
    r"|\b\d+[-\s]?days?\b.{0,12}\btrial\b"    # 7-day trial / 14 day trial
    r"|\btrial\b.{0,12}\b(available|included|offered)\b"  # trial available/included
    r"|무료\s*체험"
    r"|무료\s*평가판"
    r"|체험판",
    re.IGNORECASE,
)


def _is_trial_feature(f: str) -> bool:
    """'Free trial available' / '무료 체험' 같은 체험 안내인지 판별(기능 아님)."""
    return bool(_TRIAL_RE.search(f or ""))


# 부정/부재 표현 — 'No advanced AI models'처럼 기능을 '제공 안 함'을 뜻하는 항목.
# 'No-code'(하이픈)는 실제 기능이므로 'no' 뒤 공백만 부정으로 본다.
_NEGATIVE_RE = re.compile(
    r"^\s*no\s+"                                   # No advanced AI models / No image generation
    r"|^\s*without\s+"                             # Without …
    r"|\bdoes(?:n['’]?t|\s+not)\b"                 # does not include … / doesn't …
    r"|\bdo(?:n['’]?t|\s+not)\b"                   # do not … / don't …
    r"|\bnot\s+(includ\w*|available|supported|offered|provided)\b"  # not include(d)/available …
    r"|\bexclud(es|ing|ed)\b"                      # excludes / excluding / excluded
    r"|\bno\s+access\b"
    r"|\b(unavailable|unsupported)\b"
    r"|미지원|미제공|미포함|지원하지\s*않|제공하지\s*않|포함하지\s*않"
    r"|지원\s*안\s*함|제공\s*안\s*함|포함\s*안\s*",
    re.IGNORECASE,
)


def _is_negative_feature(f: str) -> bool:
    """'No image generation' / '미지원'처럼 기능 '부재'를 뜻하는 항목인지 판별."""
    return bool(_NEGATIVE_RE.search(f or ""))


# 워터마크가 '붙는다'는 제약 표현(무료 출력에 워터마크)은 기능이 아님 → 제외.
# 단, '워터마크 제거/없음/무료'는 유료 기능이므로 유지한다.
_WATERMARK_OK_RE = re.compile(
    r"\b(no|without|remove[sd]?|removal|free)\b|watermark[-\s]?free|제거|없",
    re.IGNORECASE,
)


def _is_limitation_feature(f: str) -> bool:
    """'Watermarked outputs'처럼 제약(한계)을 뜻하는 항목인지 판별(워터마크 부착 등)."""
    s = f or ""
    if re.search(r"watermark|워터마크", s, re.IGNORECASE) and not _WATERMARK_OK_RE.search(s):
        return True
    return False


def _skip_feature(f: str) -> bool:
    """기능 포지셔닝/분석 집계에서 제외할 노이즈
    (포함 안내·광고·플랜 이름·체험 안내·부정/부재·제약 표현)."""
    return (
        _is_inclusion_phrase(f) or _is_ad_feature(f)
        or _is_plan_name(f) or _is_trial_feature(f)
        or _is_negative_feature(f) or _is_limitation_feature(f)
    )


CLASSIFY_THRESHOLD_KEY = "classify.cheap_usd"


def get_cheap_threshold() -> float:
    """커머디티/차별화 판정의 '저렴' 기준 가격(USD). DB 설정 우선, 없으면 env 기본값."""
    raw = store.get_setting(CLASSIFY_THRESHOLD_KEY)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return config.CLASSIFY_CHEAP_USD


def set_cheap_threshold(value: float) -> None:
    store.set_setting(CLASSIFY_THRESHOLD_KEY, str(float(value)))


BAND_WIDTH_KEY = "classify.band_usd"


def get_band_width() -> float:
    """가격대별/기능별 분석의 가격 묶음 단위(USD). DB 설정 우선, 없으면 env 기본값."""
    raw = store.get_setting(BAND_WIDTH_KEY)
    if raw is not None:
        try:
            v = float(raw)
            if v >= 1:
                return v
        except (TypeError, ValueError):
            pass
    return config.CLASSIFY_BAND_USD


def set_band_width(value: float) -> None:
    store.set_setting(BAND_WIDTH_KEY, str(float(value)))


COMMODITY_PEN_KEY = "classify.commodity_pen"  # 커머디티로 보는 최소 보급률(%)


def get_commodity_pen() -> int:
    """커머디티 판정의 최소 보급률(%). DB 설정 우선, 없으면 60."""
    raw = store.get_setting(COMMODITY_PEN_KEY)
    if raw is not None:
        try:
            v = int(float(raw))
            if 1 <= v <= 100:
                return v
        except (TypeError, ValueError):
            pass
    return 60


def set_commodity_pen(value) -> None:
    v = max(1, min(100, int(float(value))))
    store.set_setting(COMMODITY_PEN_KEY, str(v))


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


def _display_source_url(company: str, source_type: str, url: str) -> str:
    """표시용 '원본' URL — 한국 검색(KR) 업체의 google_search 는 실제 수집 로케일
    (hl=ko&gl=kr)로 보여준다. 저장 키는 그대로 두고 링크만 실제 수집과 일치시킴."""
    if source_type == "google_search" and store.get_setting("search.kr:" + company) == "1":
        from .fetch import localize_google_url
        return localize_google_url(url, "kr", "ko")
    return url


def _company_icon(icon_url: str | None, source_urls: list[str]) -> str | None:
    """업체 아이콘: 저장된 icon_url(앱 아이콘) 우선, 없으면 브랜드 도메인 파비콘.

    구글 검색/스토어(google.com·apple.com 등) 호스트는 파비콘 대상에서 제외한다
    (그 도메인의 파비콘 = 구글/애플 로고가 잘못 표시되는 문제 방지). 브랜드
    도메인이 없으면 None 을 돌려 호출측이 기본 점으로 폴백하게 한다.
    """
    if icon_url:
        return icon_url
    for u in source_urls:
        host = urlparse(u).netloc.lower() if u else ""
        if host and not any(s in host for s in _STORE_HOSTS):
            return _favicon(u)
    return None


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


# ── 업체 분류(업종/도메인) ───────────────────────────────────
def _category_context():
    """(분류 목록, {업체명: category_id}, {category_id: 이름}) 을 한 번에 만든다."""
    cats = store.list_company_categories()
    cat_list = [{"id": c["id"], "name": c["name"]} for c in cats]
    id_to_name = {c["id"]: c["name"] for c in cats}
    name_to_id = {
        c["name"]: c["category_id"] for c in store.list_companies(active_only=False)
    }
    return cat_list, name_to_id, id_to_name


# ── 1. 현황 (/) ──────────────────────────────────────────────
def overview() -> dict:
    """전체 업체의 대표 출처 최신 가격표.

    대표 출처는 우선순위(공식 홈페이지 > 구글 검색 > App Store > Play Store)로
    선정하고, 나머지 출처는 보조로 명시한다.
    """
    rows = store.all_latest_by_source()

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
                "primary_source_url": _display_source_url(
                    company_name, primary["source_type"], primary["source_url"]),
                "collected_at": primary["collected_at"],
                "currency": snap.currency,
                "confidence": primary["confidence"],
                "source_count": len(srows),
                "other_sources": [_src_label(r["source_type"]) for r in others],
                "free_trial": free_trial,
                "has_free_tier": has_free_tier,
                "tiers": primary_tiers,
                "extra_sources": extra_sources,
            }
        )
    # ── 업체 분류로 그룹화(정의 순서, 미분류는 마지막) + 필터 칩 ──
    cat_list, name_to_id, id_to_name = _category_context()
    flags = {c["name"]: c for c in store.list_companies(active_only=False)}
    for co in companies:
        cid = name_to_id.get(co["company"])
        co["category_id"] = cid
        co["category"] = id_to_name.get(cid)
        rec = flags.get(co["company"])
        co["is_bundle"] = bool(rec["is_bundle"]) if rec else False
        co["is_component"] = bool(rec["is_component"]) if rec else False

    # 번들: 제공업체 아래로 결합업체(구성요소)를 중첩. 중첩된 구성요소는 최상위에서 제외.
    prov_services = _bundle_provider_services()
    by_name = {co["company"]: co for co in companies}

    def _nest(members):
        mset = {m["company"] for m in members}
        claimed = set()
        for co in members:
            if co["is_bundle"]:
                svc = [by_name[n] for n in prov_services.get(co["company"], []) if n in mset]
                co["bundle_services"] = svc
                claimed.update(s["company"] for s in svc)
        visible = [co for co in members if not (co["is_component"] and co["company"] in claimed)]
        # 번들 제공업체(📦)를 그룹 상단으로 (그 외 순서는 유지 — 안정 정렬)
        visible.sort(key=lambda co: not co["is_bundle"])
        return visible

    groups = []
    for cat in cat_list:
        members = [co for co in companies if co["category_id"] == cat["id"]]
        if members:
            groups.append(
                {"id": cat["id"], "name": cat["name"], "companies": _nest(members)}
            )
    uncategorized = [co for co in companies if not co.get("category_id")]
    if uncategorized:
        groups.append({"id": None, "name": None, "companies": _nest(uncategorized)})

    category_chips = [
        {"id": g["id"], "name": g["name"], "count": len(g["companies"])}
        for g in groups
    ]

    priority = [{"type": t, "label": _src_label(t)} for t in get_priority_order()]
    return {
        "companies": companies,
        "groups": groups,
        "categories": category_chips,
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
                "source_url": _display_source_url(name, row["source_type"], row["source_url"]),
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
def changes_view(company: str | None = None, category: str | None = None) -> dict:
    cat_list, name_to_id, _id_to_name = _category_context()
    cat_id = int(category) if category and str(category).isdigit() else None
    # 선택 분류에 속한 업체명 집합(분류 미선택이면 None=전체)
    cat_companies = (
        {n for n, cid in name_to_id.items() if cid == cat_id}
        if cat_id is not None
        else None
    )
    # 분류와 업체를 함께 선택했는데 그 업체가 분류 밖이면 업체 필터는 무시
    eff_company = company
    if cat_companies is not None and company and company not in cat_companies:
        eff_company = None

    rows = store.recent_changes(company=eff_company, limit=300)
    items = []
    for r in rows:
        if cat_companies is not None and r["company"] not in cat_companies:
            continue
        stype = r["source_type"] if "source_type" in r.keys() else None
        items.append(
            {
                "company": r["company"],
                "source_type": stype,
                "source_label": _src_label(stype) if stype else None,
                "detected_at": r["detected_at"],
                "change_type": r["change_type"],
                "tier_name": r["tier_name"],
                "old_value": r["old_value"],
                "new_value": r["new_value"],
                "summary": r["summary"],
            }
        )
    all_companies = sorted(r["name"] for r in store.list_companies(active_only=True))
    if cat_companies is not None:
        all_companies = [c for c in all_companies if c in cat_companies]
    return {
        "changes": items,
        "companies": all_companies,
        "selected": eff_company,
        "categories": cat_list,
        "selected_category": cat_id,
    }


# ── 4. 수집 상태 (/runs) ─────────────────────────────────────
def runs_view() -> dict:
    rows = store.recent_runs(limit=100)
    _cat_list, name_to_id, id_to_name = _category_context()
    items = []
    for r in rows:
        stype = r["source_type"] if "source_type" in r.keys() else None
        items.append(
            {
                "id": r["id"],
                "company": r["company"],
                "source_type": stype,
                "source_label": _src_label(stype) if stype else None,
                "category": id_to_name.get(name_to_id.get(r["company"])),
                "run_started_at": r["run_started_at"],
                "run_finished_at": r["run_finished_at"],
                "status": r["status"],
                "error_message": r["error_message"],
            }
        )
    return {"runs": items}


# ── 업체 관리 (/companies) ───────────────────────────────────
def companies_admin() -> dict:
    cat_list, _name_to_id, id_to_name = _category_context()
    companies = []
    for c in store.list_companies(active_only=True):
        srcs = store.list_sources(company=c["name"], active_only=True)
        companies.append(
            {
                "name": c["name"],
                "created_at": c["created_at"],
                "icon": _company_icon(c["icon_url"], [s["url"] for s in srcs]),
                "category_id": c["category_id"],
                "category": id_to_name.get(c["category_id"]),
                "is_bundle": bool(c["is_bundle"]),
                "is_component": bool(c["is_component"]),
                "krsearch": store.get_setting("search.kr:" + c["name"]) == "1",
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
    # 필터 칩(분류별 개수). 분류는 빈 것도 보이게 전부 노출, 미분류는 있을 때만.
    category_chips = [
        {
            "id": cat["id"],
            "name": cat["name"],
            "count": sum(1 for co in companies if co["category_id"] == cat["id"]),
        }
        for cat in cat_list
    ]
    n_uncat = sum(1 for co in companies if not co["category_id"])
    if n_uncat:
        category_chips.append({"id": None, "name": None, "count": n_uncat})

    # 번들: 제공업체를 상단에 두고 그 결합 서비스(구성요소)를 한 단계 아래로 중첩.
    #   같은 분류 안에서만 묶고, 나머지 업체는 그 뒤에 나열한다.
    prov_services = _bundle_provider_services()

    def _emit_group(members, out):
        byname = {m["name"]: m for m in members}
        claimed = set()
        for co in members:
            if co["is_bundle"]:
                svcs = []
                for sn in prov_services.get(co["name"], []):
                    svc = byname.get(sn)
                    if svc is not None and sn not in claimed:
                        svcs.append(svc)
                        claimed.add(sn)
                co["services"] = svcs   # 제공업체 아래 접어둘 결합 서비스
                out.append(co)
        for co in members:
            if co["is_bundle"] or co["name"] in claimed:
                continue
            co["services"] = []
            out.append(co)

    ordered: list = []
    for cat in cat_list:
        _emit_group([co for co in companies if co["category_id"] == cat["id"]], ordered)
    _emit_group([co for co in companies if not co["category_id"]], ordered)

    return {
        "companies": ordered,
        "categories": cat_list,
        "category_chips": category_chips,
    }


def _primary_raw_text(name: str) -> tuple[str | None, str]:
    """업체 대표 출처의 수집 원문과 그 해시(스테일 감지용)."""
    rows = store.latest_snapshots_for_company(name)
    if not rows:
        return None, ""
    primary = _pick_primary(rows, _priority_map(name))
    rt = primary["raw_text"] if "raw_text" in primary.keys() else None
    sig = hashlib.sha1((rt or "").encode("utf-8")).hexdigest() if rt else ""
    return rt, sig


def _to_usd(amount, currency: str):
    """비-USD 금액을 근사 환율로 USD 환산. USD면 그대로, 환율 없으면 None."""
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    if cur == "USD":
        return round(float(amount), 2)
    rate = config.FX_PER_USD.get(cur)
    if not rate:
        return None
    return round(float(amount) / rate, 2)


_CUR_SYMBOL = {"KRW": "₩", "JPY": "¥", "EUR": "€", "GBP": "£", "USD": "$"}


def _fmt_money(amount, currency: str) -> str | None:
    """원 통화 표기. KRW/JPY는 정수+천단위 콤마, 그 외는 기호+값."""
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    sym = _CUR_SYMBOL.get(cur, cur + " ")
    if cur in ("KRW", "JPY"):
        return f"{sym}{int(round(float(amount))):,}"
    return f"{sym}{float(amount):g}"


def _standalone_usd_map() -> dict:
    """비번들 업체로 수집된 서비스의 '대표 정가'(최저 유료 월가, USD) 맵.
    번들 포함 서비스의 원가 매칭에 사용. 키는 정규화 업체명(표기 차이 흡수).
    수집 통화(예: KRW)를 USD로 변환해 저장한다."""
    out: dict[str, float] = {}
    for c in store.list_companies(active_only=True):
        if c["is_bundle"]:
            continue
        rows = store.latest_snapshots_for_company(c["name"])
        if not rows:
            continue
        try:
            snap = PricingSnapshot.from_payload_json(
                _pick_primary(rows, _priority_map(c["name"]))["payload_json"]
            )
            cur = (snap.currency or "USD").upper()
        except Exception:  # noqa: BLE001
            cur = "USD"
        prices = []
        for tr in _company_plan_tiers(c["name"]):
            if tr.get("is_free"):
                continue
            eff = tr["monthly"] if tr["monthly"] is not None else tr["annual"]
            u = _to_usd(eff, cur)
            if u is not None:
                prices.append(u)
        if prices:
            out[_normalize_feature(c["name"])] = min(prices)
    return out


_CAT_BUCKETS = [
    ("video", ["stream", "video", " tv", "tv ", "ott", "넷플", "영상", "콘텐츠", "movie", "vod", "watch"]),
    ("music", ["music", "음악", "spotify", "멜론", "audio", "podcast", "팟캐스트"]),
    ("cloud", ["cloud", "storage", "클라우드", "저장", "drive"]),
    ("game", ["game", "gaming", "게임"]),
    ("news", ["news", "뉴스", "magazine", "신문", "매거진"]),
    ("books", ["book", "webtoon", "웹툰", "도서", "ebook", "comic", "만화", "novel", "웹소설"]),
    ("shopping", ["shop", "commerce", "delivery", "쇼핑", "배송", "커머스", "배달"]),
    ("points", ["point", "reward", "포인트", "적립", "cashback", "캐시", "멤버십 포인트"]),
    ("fitness", ["fitness", "health", "운동", "헬스"]),
]


def _cat_bucket(category: str, name: str = "") -> str:
    """정가 합계에서 '같은 종류'를 묶기 위한 거친 카테고리 버킷.
    'Streaming Video'·'Streaming'·'OTT' 등이 한 버킷('video')으로 묶인다."""
    s = ((category or "") + " " + (name or "")).lower()
    for bucket, kws in _CAT_BUCKETS:
        if any(k in s for k in kws):
            return bucket
    return (category or "기타").strip().lower()


def _bundle_anchor_id(company: str, plan: str) -> str:
    """덤벨 행 → 해당 번들 요금제 상세 행으로 점프하기 위한 안정적 앵커 id."""
    base = re.sub(r"[^a-z0-9]+", "-", (str(company) + "-" + str(plan)).lower()).strip("-")
    return "bp-" + (base or "x")


# 같은 서비스의 '티어 변형'(Netflix Standard/Premium/4K 등)을 묶기 위한 브랜드 키.
def _brand_key(name: str) -> str:
    """서비스의 브랜드(선행 토큰) 키 — 같은 서비스의 티어 변형 묶기용.
    'Netflix Standard with Ads'/'Netflix Premium 4K (upgrade)' → 'netflix'."""
    s = re.sub(r"[^a-z0-9가-힣\s]", " ", (name or "").lower())
    toks = [t for t in s.split() if t]
    return toks[0] if toks else ""


def _match_standalone(service_name: str, smap: dict):
    """서비스 이름과 맞는 수집 정가를 찾는다. 정규화 키 동일 → 우선, 없으면
    정규화 부분일치(가장 긴 키)로 매칭(예: 'Max' ↔ 'HBO Max')."""
    key = _normalize_feature(service_name)
    if not key:
        return None
    if key in smap:
        return smap[key]
    best_k, best_price = "", None
    for k, price in smap.items():
        if len(k) >= 3 and (k in key or key in k) and len(k) > len(best_k):
            best_k, best_price = k, price
    return best_price


def _brand_favicon(name: str) -> str | None:
    """업체명 전체로 브랜드 도메인 파비콘 추정 — google_search 전용 등 아이콘 없는
    제공업체용. 'T-mobile'→tmobile.com, 'Naver'→naver.com. 영숫자 2자 미만이면 None."""
    slug = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if len(slug) < 2:
        return None
    return f"https://www.google.com/s2/favicons?domain={slug}.com&sz=64"


def _service_icon(service_name: str, comp_icons: dict) -> str | None:
    """서비스 아이콘: 매칭 업체의 실제 아이콘 우선, 없으면 서비스명 기반 파비콘 추정."""
    s = (service_name or "").lower()
    best_name, best_icon = "", None
    for cname, icon in comp_icons.items():
        if len(cname) >= 3 and (cname in s or s in cname) and len(cname) > len(best_name):
            best_name, best_icon = cname, icon
    if best_icon:
        return best_icon
    tokens = re.findall(r"[a-z0-9]+", s)
    if tokens:
        return f"https://www.google.com/s2/favicons?domain={tokens[0]}.com&sz=64"
    return None


def _bundle_anchor(cat_name: str | None) -> str | None:
    """분류명에서 앵커 서비스 추출. '번들-Netflix'/'Bundle: Netflix' → 'Netflix'."""
    if not cat_name:
        return None
    for sep in ("-", "—", ":", "·"):
        if sep in cat_name:
            a = cat_name.split(sep, 1)[1].strip()
            if a:
                return a
    return None


def bundle_view(names: list[str] | None = None) -> dict:
    """가격 분석(번들): is_bundle 업체를 분류(예: 번들-Netflix)별로 묶고,
    AI 구조화 추출(bundle_analysis) 결과로 가격 분포·연계 서비스 카테고리·
    조합을 분석한다. 앵커(예: Netflix)는 분류명에서 인식해 연계 집계에서 제외.

    names 가 주어지면 그 번들 업체만 분석(없으면 전체 번들 업체).
    """
    import math

    _cat_list, _name_to_id, id_to_name = _category_context()
    icon_map = {c["name"]: c["icon_url"] for c in store.list_companies(active_only=False)}
    src_map: dict[str, list[str]] = {}
    for s in store.list_sources(active_only=True):
        src_map.setdefault(s["company_name"], []).append(s["url"])
    smap = _standalone_usd_map()   # 비번들 수집 서비스의 정가(원가 매칭용)
    # 서비스 아이콘 매칭용: 실제 아이콘이 있는 업체만(google_search 전용은 보통 없음)
    comp_icons = {}
    for c in store.list_companies(active_only=False):
        ic = _company_icon(icon_map.get(c["name"]), src_map.get(c["name"], []))
        if ic:
            comp_icons[c["name"].lower()] = ic

    all_bundle = [c for c in store.list_companies(active_only=True) if c["is_bundle"]]
    all_names = sorted(c["name"] for c in all_bundle)
    name_set = set(all_names)
    sel = [n for n in (names or []) if n in name_set]
    active = set(sel) if sel else name_set
    bundle_companies = [c for c in all_bundle if c["name"] in active]
    cat_chips, company_cat = _company_category_picker(all_names)
    groups_map: dict = {}
    needs_analysis: list[str] = []
    for c in bundle_companies:
        name = c["name"]
        # 기본: 수집된 포함 서비스 정가를 전부 합산(수집 결과 그대로). Naver 같은
        # 택1 멤버십만 pickone 토글을 켜서 같은 종류는 하나만 센다.
        pickone = store.get_setting("bundle.pickone:" + name) == "1"
        krsearch = store.get_setting("search.kr:" + name) == "1"
        _rt, cur_sig = _primary_raw_text(name)
        row = store.get_bundle_analysis(name)
        analyzed = row is not None
        stale = analyzed and cur_sig and row["signature"] != cur_sig
        plans = []
        if analyzed:
            try:
                payload = json.loads(row["payload_json"]) or {}
                plans = payload.get("plans", []) or []
            except (ValueError, TypeError):
                plans = []
        if not analyzed or stale:
            needs_analysis.append(name)

        cid = c["category_id"]
        g = groups_map.get(cid)
        if g is None:
            g = groups_map[cid] = {
                "id": cid, "name": id_to_name.get(cid),
                "anchor": _bundle_anchor(id_to_name.get(cid)),
                "companies": [], "prices": [], "cat_count": {}, "cat_value": {},
                "dumbbell": [],
            }
        anchor = g["anchor"]
        co_icon = _company_icon(icon_map.get(name), src_map.get(name, [])) or _brand_favicon(name)
        # 사용자가 영구 제외한 요금제(이름)는 분석에서 통째로 뺀다.
        try:
            hidden_plans = set(json.loads(store.get_setting("bundle.hideplan:" + name) or "[]"))
        except (ValueError, TypeError):
            hidden_plans = set()
        g["hidden_n"] = g.get("hidden_n", 0) + len(hidden_plans)
        co_prices = []
        co_plans = []
        for p in plans:
            if (p.get("name") or "") in hidden_plans:
                continue  # 영구 제외된 요금제
            cur = (p.get("currency") or "USD").upper()
            m_usd = _to_usd(p.get("monthly"), cur)
            a_usd = _to_usd(p.get("annual"), cur)
            eff = m_usd if m_usd is not None else a_usd   # 분포·집계는 USD 기준
            if eff is not None:
                co_prices.append(eff)
                g["prices"].append({
                    "usd": eff, "company": name,
                    "plan": p.get("name"), "icon": co_icon,
                })
            # 연계 카테고리(앵커 제외) 빈도 집계.
            svcs = []
            for s in p.get("services", []):
                sname = (s.get("name") or "")
                is_anchor = bool(anchor and anchor.lower() in sname.lower())
                skey = _normalize_feature(sname)
                # 정가 우선순위: ①사용자 직접 입력(번들 통화) ②AI list_price ③수집 정가 매칭
                ov = store.get_setting("bundle.svc:" + name + ":" + skey)
                ov_num = re.sub(r"[^\d.]", "", ov) if ov else ""
                lp = None
                if ov_num:
                    lp = _to_usd(float(ov_num), cur)
                if lp is None:
                    lp = _to_usd(s.get("list_price"), cur)
                if lp is None:
                    lp = _match_standalone(sname, smap)
                svcs.append({
                    "name": sname, "category": s.get("category") or "기타",
                    "is_anchor": is_anchor, "choice": bool(s.get("choice")),
                    "list_usd": lp, "bucket": _cat_bucket(s.get("category") or "", sname),
                    "brand": _brand_key(sname), "tier_alt": False,
                    "key": skey, "manual": bool(ov), "override_raw": ov_num,
                    "icon": _service_icon(sname, comp_icons),
                })
                if is_anchor:
                    continue  # 앵커 자신은 연계 집계에서 제외
                cat = s.get("category") or "기타"
                g["cat_count"][cat] = g["cat_count"].get(cat, 0) + 1
            # ── 정가 합계 ─────────────────────────────────────────
            # 핵심: AI가 매긴 choice(택1) 표시를 그대로 존중.
            #   choice=true → 택1 대안(여럿 중 choose개만, 앵커 우선) — 나머지는 제외.
            #   choice=false → 항상 포함 → 전부 합산.
            # pickone 토글: AI가 택1을 놓쳐 모두 '항상 포함'으로 잡힌 경우 대비 —
            #   '항상 포함' 중에서도 같은 종류(버킷)는 하나만 센다(과대계상 방지).
            priced = [s for s in svcs if s["list_usd"] is not None]
            # 같은 서비스의 '티어 변형'(예: Netflix Standard/Premium/4K)은 하나만 받는
            # 것이므로 같은 (버킷,브랜드)끼리는 1개만 센다(기준=앵커 우선, 그다음 최저가
            # =기본 포함분). 다른 서비스(Hulu·Apple TV+ 등)는 브랜드가 달라 그대로 유지.
            #   → 같은 카테고리 안에서도 '택1 티어'와 '각각 포함'이 구분된다.
            tier_groups: dict = {}
            for s in priced:
                if s["choice"] or not s["brand"]:
                    continue
                tier_groups.setdefault((s["bucket"], s["brand"]), []).append(s)
            for members in tier_groups.values():
                if len(members) <= 1:
                    continue
                rep = min(members, key=lambda x: (not x["is_anchor"], x["list_usd"]))
                for s in members:
                    if s is not rep:
                        s["tier_alt"] = True   # 합계 제외(같은 서비스 상위 티어)
            fixed = [s for s in priced if not s["choice"] and not s["tier_alt"]]  # 항상 포함
            pool = [s for s in priced if s["choice"]]         # 택1 대안
            fixed_buckets: set = set()
            if pickone:
                best: dict = {}
                for s in fixed:
                    cur_b = best.get(s["bucket"])
                    rank = (s["is_anchor"], s["list_usd"])
                    if cur_b is None or rank > (cur_b["is_anchor"], cur_b["list_usd"]):
                        best[s["bucket"]] = s
                fixed = list(best.values())
                fixed_buckets = set(best.keys())
            k = p.get("choose") or 1
            chosen_pool = sorted(pool, key=lambda x: (not x["is_anchor"], -x["list_usd"]))[:k]
            # 카테고리별 평균 정가($/건): 정가 합계에 실제 반영되는 구성요소를
            #   카테고리로 묶어 정가 합/출현수를 모은다(앵커 제외, 택1/pickone 반영분).
            #   빈도와 독립적인 '건당 가치'를 보여주려 합산이 아닌 평균을 쓴다.
            for s in fixed + chosen_pool:
                if s["is_anchor"]:
                    continue
                cv = s.get("category") or "기타"
                e = g["cat_value"].get(cv)
                if e is None:
                    e = g["cat_value"][cv] = {"sum": 0.0, "n": 0}
                e["sum"] += s["list_usd"]
                e["n"] += 1
            parts = (
                [{"name": s["name"], "usd": round(s["list_usd"], 2), "choice": False,
                  "key": s["key"], "manual": s["manual"], "override_raw": s["override_raw"],
                  "icon": _service_icon(s["name"], comp_icons)}
                 for s in sorted(fixed, key=lambda x: (not x["is_anchor"], -x["list_usd"]))]
                + [{"name": s["name"], "usd": round(s["list_usd"], 2), "choice": True,
                    "key": s["key"], "manual": s["manual"], "override_raw": s["override_raw"],
                    "icon": _service_icon(s["name"], comp_icons)} for s in chosen_pool]
            )
            standalone = round(sum(pt["usd"] for pt in parts), 2) if parts else None
            # '정가 미확인': 가격 미상이면서 '항상 포함(choice=false)'인 것만.
            #   택1 대안의 미상 가격은 (어차피 하나만 고르므로) 합계/미확인에서 제외.
            unpriced, seen_un = [], set()
            for s in svcs:
                if s["list_usd"] is not None or not s["name"] or s["choice"]:
                    continue
                if pickone and s["bucket"] in fixed_buckets:
                    continue  # 택1 모드: 같은 종류에 대표가 있으면 그 종류 미상은 숨김
                if s["name"] in seen_un:
                    continue
                seen_un.add(s["name"])
                unpriced.append({"name": s["name"], "key": s["key"]})
            savings_pct = None
            if standalone and eff is not None and standalone > 0:
                savings_pct = round((standalone - eff) / standalone * 100)
            co_plans.append({
                "name": p.get("name"), "provider": p.get("provider"),
                "currency": cur,
                "monthly": p.get("monthly"), "annual": p.get("annual"),
                "monthly_usd": m_usd, "annual_usd": a_usd,
                "monthly_orig": _fmt_money(p.get("monthly"), cur) if cur != "USD" else None,
                "annual_orig": _fmt_money(p.get("annual"), cur) if cur != "USD" else None,
                "choose": p.get("choose"),
                "price_note": p.get("price_note"),
                "conditions": p.get("conditions"),
                "anchor": _bundle_anchor_id(name, p.get("name")),
                "services": svcs,
                "standalone_usd": standalone,
                "standalone_parts": parts,
                "unpriced": unpriced,
                "savings_pct": savings_pct,
            })
            # 덤벨 차트용: 행=번들 구성, 점=할인가(번들가) ↔ 정가 합계
            if eff is not None:
                g["dumbbell"].append({
                    "company": name, "plan": p.get("name"), "icon": co_icon,
                    "bundle": eff, "list": standalone, "save": savings_pct,
                    "anchor": _bundle_anchor_id(name, p.get("name")),
                })
        g["companies"].append({
            "name": name,
            "icon": co_icon,
            "plans": co_plans,
            "price_min": min(co_prices) if co_prices else None,
            "price_max": max(co_prices) if co_prices else None,
            "plan_count": len(co_plans),
            "analyzed": analyzed,
            "stale": bool(stale),
            "anchor": anchor,
            "pickone": pickone,
            "krsearch": krsearch,
        })

    groups = []
    for g in groups_map.values():
        prices = sorted(g.pop("prices"), key=lambda x: x["usd"])
        cat_count = g.pop("cat_count")
        cat_value = g.pop("cat_value")
        g["price_min"] = prices[0]["usd"] if prices else None
        g["price_max"] = prices[-1]["usd"] if prices else None
        g["price_points"] = prices
        db = g.pop("dumbbell")
        vals = [d["bundle"] for d in db] + [d["list"] for d in db if d["list"] is not None]
        # 할인율순(기본 보기의 대체): 절감률 큰 순. 절감률 미상은 뒤로.
        g["dumbbell"] = sorted(db, key=lambda d: (d["save"] is None, -(d["save"] or 0), d["bundle"]))
        # 제공업체별(기본 보기): 같은 제공업체끼리 묶고, 그 안에선 번들가 오름차순.
        #   제공업체 정렬은 첫 등장 순서를 유지(수집/등록 순).
        prov_map: dict = {}
        for d in db:
            pr = prov_map.get(d["company"])
            if pr is None:
                pr = prov_map[d["company"]] = {
                    "company": d["company"], "icon": d["icon"], "rows": [],
                }
            pr["rows"].append(d)
        for pr in prov_map.values():
            pr["rows"].sort(key=lambda d: d["bundle"])
        g["dumbbell_providers"] = list(prov_map.values())
        g["dmax"] = max(vals) if vals else 1
        g["plan_total"] = sum(co["plan_count"] for co in g["companies"])
        g["service_categories"] = sorted(
            ({"category": k, "count": v} for k, v in cat_count.items()),
            key=lambda x: -x["count"],
        )
        g["category_values"] = sorted(
            ({"category": k, "usd": round(v["sum"] / v["n"], 2), "n": v["n"]}
             for k, v in cat_value.items() if v["n"]),
            key=lambda x: -x["usd"],
        )
        groups.append(g)
    groups.sort(key=lambda x: (x["id"] is None, (x["name"] or "").lower()))
    return {
        "groups": groups,
        "count": len(all_bundle),
        "needs_analysis": needs_analysis,
        "access_required": bool(config.ACCESS_CODE),
        "all_companies": all_names,
        "category_chips": cat_chips,
        "company_cat": company_cat,
        "selected": sorted(active),
    }


def save_bundle_card(title: str = "", names: list[str] | None = None) -> int | None:
    """현재(선택) 번들 분석 결과를 저장 시점 그대로 카드로 저장. 그룹이 없으면 None."""
    data = bundle_view(names)
    if not data.get("groups"):
        return None
    if not title:
        names = [g["name"] or "미분류" for g in data["groups"]]
        title = " · ".join(names[:3]) or "번들 분석"
    return store.save_bundle_card(title, json.dumps(data, ensure_ascii=False))


def saved_bundle_cards() -> list[dict]:
    out = []
    for r in store.list_bundle_cards():
        # 저장된 페이로드에서 제공업체명(그룹별 업체)을 뽑아 카드 부제로.
        provs, seen = [], set()
        try:
            data = json.loads(r["payload_json"]) or {}
            for g in data.get("groups", []):
                for co in g.get("companies", []):
                    nm = co.get("name")
                    if nm and nm not in seen:
                        seen.add(nm)
                        provs.append(nm)
        except (ValueError, TypeError):
            provs = []
        out.append({
            "id": r["id"], "title": r["title"], "created_at": r["created_at"],
            "providers": provs,
        })
    return out


def load_bundle_card(card_id: int) -> dict | None:
    row = store.get_bundle_card(card_id)
    if row is None:
        return None
    try:
        data = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"], "data": data}


def run_bundle_extraction(
    names: list[str] | None = None, only_stale: bool = False, progress_cb=None
) -> int:
    """번들 업체의 대표 출처 원문에서 번들 요금제를 AI로 구조화 추출·저장.

    names 가 주어지면 그 업체만(없으면 전체 번들 업체).
    only_stale=True 면 원문이 직전 추출 이후 바뀐 업체만(수집 파이프라인용).
    progress_cb(done, total, current) 가 주어지면 업체별 진행률을 보고한다.
    반환: 추출을 시도한 업체 수.
    """
    from . import extract

    _cl, _n2i, id_to_name = _category_context()
    want = set(names) if names else None
    targets = [
        c for c in store.list_companies(active_only=True)
        if c["is_bundle"] and (want is None or c["name"] in want)
    ]
    total = len(targets)
    n = 0
    for i, c in enumerate(targets):
        name = c["name"]
        if progress_cb:
            progress_cb(i, total, name)
        rt, sig = _primary_raw_text(name)
        if not rt:
            continue
        if only_stale:
            row = store.get_bundle_analysis(name)
            if row is not None and row["signature"] == sig:
                continue  # 원문 변경 없음 → 재추출 생략(비용 절약)
        anchor = _bundle_anchor(id_to_name.get(c["category_id"]))
        result = extract.extract_bundles_ai(name, rt, anchor)
        store.set_bundle_analysis(name, json.dumps(result, ensure_ascii=False), sig)
        n += 1
        # 정가를 번들 페이지에서 못 구한(=list_price 없는) '진짜 서비스'만 구성요소로
        # 등록해 개별 정가를 따로 수집한다(예: 멤버십 페이지엔 없고 전용 검색에만 있는
        # Naver MyBox). 혜택성 이름(Perks/Credit/Content 등)은 등록 제외.
        needs = [
            (s.get("name") or "").strip()
            for p in result.get("plans", [])
            for s in p.get("services", [])
            if s.get("list_price") in (None, "", 0) and not _is_junk_component(s.get("name") or "")
        ]
        _auto_register_components(needs, exclude_name=name, category_id=c["category_id"])
    if progress_cb:
        progress_cb(total, total, "")
    return n


# 혜택 설명(실제 구독 서비스가 아님)을 구성요소 자동 등록에서 제외하기 위한 패턴.
_JUNK_COMPONENT_RE = re.compile(
    r"(?i)(perk|benefit|credit|content|discount|reward|\bpoint|bonus|cashback|"
    r"voucher|coupon|membership|included|free\s*trial|subscription credit|혜택|적립|할인|쿠폰)"
)


def _is_junk_component(name: str) -> bool:
    """혜택성/설명형 이름이면 True(구성요소로 만들지 않음)."""
    nm = (name or "").strip()
    if len(nm) < 2:
        return True
    return bool(_JUNK_COMPONENT_RE.search(nm))


def _auto_register_components(names, exclude_name: str, category_id) -> None:
    """번들 포함 서비스 중 정가 미상인 '진짜 서비스'를 구성요소(원가 수집용)로 등록.

    새로 만든 서비스만 구성요소로 표시 + 같은 번들 분류 + 구글 검색 소스 부여.
    이미 존재하는 업체(표기 변형 포함)는 건드리지 않는다.
    """
    from .fetch import build_google_search_url

    existing = {_normalize_feature(c["name"]) for c in store.list_companies(active_only=False)}
    excl = _normalize_feature(exclude_name or "")
    for nm in names:
        nm = (nm or "").strip()
        key = _normalize_feature(nm)
        if not nm or key == excl or key in existing:
            continue
        store.add_company(nm)
        store.set_company_component(nm, True)
        if category_id:
            store.set_company_category(nm, category_id)
        store.add_source(
            company=nm, source_type="google_search", url=build_google_search_url(nm)
        )
        existing.add(key)


def diag_bundle_price(q: str = "mybox", grep: str | None = None) -> str:
    """특정 서비스의 정가가 합산에 안 들어가는 원인을 텍스트로 진단.
    CLI 스크립트와 웹 라우트가 공용으로 사용."""
    from pathlib import Path
    from .. import config

    q = (q or "mybox").lower()
    out = []
    p = Path(config.DB_PATH).expanduser()
    out.append("DB_PATH        : %s" % config.DB_PATH)
    out.append("DB_PATH(abs)   : %s" % p.resolve())
    out.append("search term    : %s" % q)
    allc = store.list_companies(active_only=False)
    out.append("total companies: %d" % len(allc))
    hits = [c for c in allc if q in c["name"].lower()]
    if not hits:
        out.append("!! '%s' 를 이름에 포함하는 업체가 없음. 등록된 구성요소:" % q)
        out += ["   - " + c["name"] for c in allc if c["is_component"]]
        return "\n".join(out)

    out.append("SERPAPI_KEY set: %s" % bool(config.SERPAPI_KEY))
    smap = _standalone_usd_map()
    names = {c["name"] for c in hits}
    for c in hits:
        out.append("-" * 50)
        out.append("company   : %s" % c["name"])
        out.append("  flags   : component=%s bundle=%s active=%s"
                   % (c["is_component"], c["is_bundle"], c["active"]))
        out.append("  search.kr: %r  (원본 표시=%s)" % (
            store.get_setting("search.kr:" + c["name"]),
            "ko/kr" if store.get_setting("search.kr:" + c["name"]) == "1" else "en/us"))
        srcs = store.list_sources(company=c["name"], active_only=False)
        out.append("  sources : %d" % len(srcs))
        for s in srcs:
            out.append("    src: id=%s type=%s active=%s url=%s"
                       % (s["id"], s["source_type"], s["active"], (s["url"] or "")[:90]))
        rows = store.latest_snapshots_for_company(c["name"])
        out.append("  snapshots: %d" % len(rows))
        for r in rows:
            rt = r["raw_text"] or ""
            out.append("  snap    : src=%s conf=%s cur=%s raw_len=%d"
                       % (r["source_type"], r["confidence"], r["currency"], len(rt)))
            out.append("    raw[:400]= %r" % rt[:400])
            if grep:
                low = rt.lower()
                gl = grep.lower()
                idx = low.find(gl)
                out.append("    grep(%r): %d회%s" % (
                    grep, low.count(gl),
                    ("  …%r…" % rt[max(0, idx - 60):idx + 60]) if idx >= 0 else " (없음)"))
        tiers = _company_plan_tiers(c["name"])
        if not tiers:
            out.append("  tiers   : (none)  <-- 수집/추출에서 가격 티어가 안 나옴")
        for t in tiers:
            out.append("  tier    : name=%r free=%s monthly=%s annual=%s"
                       % (t["name"], t["is_free"], t["monthly"], t["annual"]))
        key = _normalize_feature(c["name"])
        out.append("  norm key : %s" % key)
        out.append("  smap[key]= %s (USD)" % smap.get(key))
        out.append("  match    = %s" % _match_standalone(c["name"], smap))

        # 번들 업체: 추출 경로(원문 선택→앵커→추출 결과) 진단
        if c["is_bundle"]:
            _cl, _n2i, id_to_name = _category_context()
            anchor = _bundle_anchor(id_to_name.get(c["category_id"]))
            primary = _pick_primary(rows, _priority_map(c["name"])) if rows else None
            prt = (primary["raw_text"] or "") if primary else ""
            out.append("  -- bundle 추출 진단 --")
            out.append("  category : %s" % id_to_name.get(c["category_id"]))
            out.append("  anchor   : %r" % anchor)
            out.append("  primary  : src=%s raw_len=%d"
                       % (primary["source_type"] if primary else "(none)", len(prt)))
            if anchor:
                out.append("  anchor_in_raw : %s  (%d회)"
                           % (anchor.lower() in prt.lower(), prt.lower().count(anchor.lower())))
            brow = store.get_bundle_analysis(c["name"])
            if not brow:
                out.append("  bundle_analysis: (없음) <-- 추출이 아직 안 됨/실패")
            else:
                try:
                    bp = json.loads(brow["payload_json"]) or {}
                except (ValueError, TypeError):
                    bp = {}
                pls = bp.get("plans", []) or []
                out.append("  bundle_analysis: plans=%d (sig=%s)"
                           % (len(pls), (brow["signature"] or "")[:8]))
                for pl in pls:
                    svcs = pl.get("services", []) or []
                    out.append("    plan: name=%r cur=%s monthly=%s annual=%s choose=%s svc=%d"
                               % (pl.get("name"), pl.get("currency"), pl.get("monthly"),
                                  pl.get("annual"), pl.get("choose"), len(svcs)))
                    for s in svcs:
                        out.append("      svc: %r cat=%s choice=%s list_price=%s"
                                   % (s.get("name"), s.get("category"),
                                      s.get("choice"), s.get("list_price")))

    out.append("=" * 50)
    out.append("최근 수집 로그(해당 업체):")
    for r in store.recent_runs(120):
        if r["company"] in names:
            out.append("  [%s] src=%s status=%s err=%s"
                       % (r["company"], r["source_type"], r["status"], r["error_message"]))

    out.append("=" * 50)
    out.append("번들 분석상의 서비스 / list_price / 매칭:")
    for c in allc:
        if not c["is_bundle"]:
            continue
        row = store.get_bundle_analysis(c["name"])
        if not row:
            continue
        try:
            payload = json.loads(row["payload_json"]) or {}
        except (ValueError, TypeError):
            continue
        for pl in payload.get("plans", []):
            for s in pl.get("services", []):
                nm = s.get("name") or ""
                if q not in nm.lower():
                    continue
                out.append("  [%s] svc=%r choice=%s list_price=%s -> match=%s"
                           % (c["name"], nm, s.get("choice"), s.get("list_price"),
                              _match_standalone(nm, smap)))
    return "\n".join(out)


def clear_components() -> int:
    """구성요소(🧩)로 등록된 업체를 모두 삭제. 반환: 삭제한 업체 수."""
    comps = [c["name"] for c in store.list_companies(active_only=False) if c["is_component"]]
    for nm in comps:
        store.delete_company(nm)
    return len(comps)


def _dedup_company_groups(groups) -> int:
    """동일 그룹(같은 서비스로 판정된 업체들)에서 중복 '구성요소'를 정리.

    그룹에 일반(비구성요소) 업체가 있으면 그걸 남기고 중복 구성요소를 모두 삭제.
    모두 구성요소면 수집 데이터(스냅샷) 있는 것 우선 1개만 남김. 반환: 삭제 수.
    """
    removed = 0
    for members in groups:
        if len(members) < 2:
            continue
        non_comp = [c for c in members if not c["is_component"]]
        comps = [c for c in members if c["is_component"]]
        if non_comp:
            to_delete = comps
        else:
            comps.sort(key=lambda c: (
                0 if store.latest_snapshots_for_company(c["name"]) else 1, c["name"]))
            to_delete = comps[1:]
        for c in to_delete:
            store.delete_company(c["name"])
            removed += 1
    return removed


def dedup_components() -> int:
    """이름 표기만 다른 중복 구성요소를 정규화 키 기준으로 정리. 반환: 삭제 수."""
    groups: dict[str, list] = {}
    for c in store.list_companies(active_only=False):
        groups.setdefault(_normalize_feature(c["name"]), []).append(c)
    return _dedup_company_groups(groups.values())


def dedup_components_ai() -> int:
    """AI 의미 군집으로 한↔영 등 표기가 전혀 다른 중복 구성요소까지 정리.

    예: '넷플릭스'·'Netflix'·'Netflix Standard' → 한 그룹. (AI 호출, 코드 필요)
    """
    from . import extract

    companies = list(store.list_companies(active_only=False))
    names = [c["name"] for c in companies]
    if len(names) < 2:
        return 0
    mapping = extract.dedupe_features_ai(names)  # {원본명: canonical}
    by_name = {c["name"]: c for c in companies}
    groups: dict[str, list] = {}
    for nm in names:
        canon = mapping.get(nm) or _normalize_feature(nm)
        groups.setdefault(canon.lower(), []).append(by_name[nm])
    return _dedup_company_groups(groups.values())


def _bundle_provider_services() -> dict:
    """번들 제공업체명 → 그 업체 번들에 실제 포함된 구성요소 업체명 목록.

    각 제공업체의 bundle_analysis(plans[].services)에 나온 서비스명을, 등록된
    구성요소 업체명과 매칭한다. (구성요소는 여러 제공업체에 중복될 수 있음)
    """
    comp_names = [
        c["name"] for c in store.list_companies(active_only=True) if c["is_component"]
    ]
    out: dict[str, list[str]] = {}
    for c in store.list_companies(active_only=True):
        if not c["is_bundle"]:
            continue
        row = store.get_bundle_analysis(c["name"])
        svc_names: set[str] = set()
        if row:
            try:
                payload = json.loads(row["payload_json"]) or {}
                for p in payload.get("plans", []):
                    for s in p.get("services", []):
                        nm = (s.get("name") or "").strip().lower()
                        if nm:
                            svc_names.add(nm)
            except (ValueError, TypeError):
                pass
        matched = []
        for cn in comp_names:
            cl = cn.lower()
            if any(len(sn) >= 3 and (cl in sn or sn in cl) for sn in svc_names):
                matched.append(cn)
        out[c["name"]] = matched
    return out


def collection_targets() -> dict:
    """수집 대상 선택용 구조: 분류 그룹 안에서 제공업체별로 그 업체의 결합
    서비스를 중첩한다. (여러 번들이 같은 분류를 공유해도 업체별로 분리)
    """
    cat_list, _n2i, _id2n = _category_context()
    admin = companies_admin()
    by_name = {c["name"]: c for c in admin["companies"]}
    prov_services = _bundle_provider_services()

    def build(members):
        providers = [c for c in members if c["is_bundle"]]
        prov_blocks = []
        claimed = set()
        for pv in providers:
            svc_cos = []
            for sn in prov_services.get(pv["name"], []):
                co = by_name.get(sn)
                if co:
                    svc_cos.append(co)
                    claimed.add(sn)
            prov_blocks.append({"provider": pv, "services": svc_cos})
        others = [c for c in members if not c["is_bundle"] and not c["is_component"]]
        orphans = [
            c for c in members if c["is_component"] and c["name"] not in claimed
        ]
        return prov_blocks, others, orphans

    groups = []
    for cat in cat_list:
        members = [c for c in admin["companies"] if c["category_id"] == cat["id"]]
        if not members:
            continue
        prov_blocks, others, orphans = build(members)
        groups.append({"id": cat["id"], "name": cat["name"], "count": len(members),
                       "providers": prov_blocks, "others": others, "orphans": orphans})
    uncat = [c for c in admin["companies"] if not c["category_id"]]
    if uncat:
        prov_blocks, others, orphans = build(uncat)
        groups.append({"id": None, "name": None, "count": len(uncat),
                       "providers": prov_blocks, "others": others, "orphans": orphans})
    return {"groups": groups, "categories": cat_list,
            "category_chips": admin["category_chips"], "companies": admin["companies"]}


def _effective_category(feature: str, cat_map: dict[str, str]) -> str:
    """기능의 카테고리: 저장된 매핑(AI/사용자) 우선, 없으면 키워드 휴리스틱."""
    from . import compare as cmp

    if feature in cat_map:
        return cat_map[feature]
    return cmp.categorize_feature(feature)[0]


def _is_free_tier(t) -> bool:
    """무료 티어 여부: 월가격 0이거나 이름에 free/무료 포함."""
    return (t.monthly_price == 0) or ("free" in t.name.lower()) or ("무료" in t.name)


def _company_paid_data(name: str):
    """업체 대표 출처의 (산점도 점들, 유료 기능 목록, 최저가 플랜의 월/연 가격).

    여러 출처가 있어도 현황과 동일한 우선순위로 고른 **대표 출처 한 곳**의 값만
    사용한다(가격 산점도가 출처별로 뒤섞이지 않도록).
    같은 플랜의 결제주기 티어(Monthly/Annual 등)는 (월, 연환산) 한 점으로 병합한다.
    무료 티어는 산점도에 표시하지 않는다(유료 가격 비교 목적).
    """
    from . import compare as cmp

    rows = store.latest_snapshots_for_company(name)
    if not rows:
        return [], [], {"monthly": None, "annual": None}
    rows = [_pick_primary(rows, _priority_map(name))]
    pts, feats, seen = [], [], set()
    price_info = {"monthly": None, "annual": None}
    best_eff = None
    for row in rows:
        snap = PricingSnapshot.from_payload_json(row["payload_json"])

        # 플랜 단위로 묶어 (월, 연환산) 한 점씩 (무료 티어는 점에서 제외).
        # 같은 플랜의 월/연 결제 변형은 이름 기준으로 합쳐 점 1개로 만든다.
        paid_tiers = [t for t in snap.tiers if not _is_free_tier(t)]
        plot = cmp.plan_points(paid_tiers)
        for p in plot:
            key = (p["x"], p["y"], p["tier"])
            if key not in seen:
                seen.add(key)
                pts.append(p)
            # 대표 가격 = 최저가 플랜의 월/연을 함께 보관
            cands = [v for v in (p.get("monthly"), p.get("annual")) if v is not None and v > 0]
            if cands:
                eff = min(cands)
                if best_eff is None or eff < best_eff:
                    best_eff = eff
                    price_info = {"monthly": p.get("monthly"), "annual": p.get("annual")}

        for t in snap.tiers:
            if not _is_free_tier(t):
                for f in t.features:
                    if f not in feats:
                        feats.append(f)
    return pts, feats, price_info


def _company_plan_tiers(name: str, cat_map: dict[str, str] | None = None) -> list[dict]:
    """업체 대표 출처의 티어를 플랜 단위로 묶어 반환(가로 열 표시용).

    같은 플랜의 월/연 결제 변형은 한 열로 합치고, 무료/유료를 구분한다.
    각 티어의 기능은 카테고리별로 묶는다(클릭 시 세부 기능 표시용).
    각 항목: {name, is_free, monthly, annual, price_note,
              categories:[{category, features}]}
    """
    from . import compare as cmp

    if cat_map is None:
        cat_map = store.get_feature_categories()

    rows = store.latest_snapshots_for_company(name)
    if not rows:
        return []
    primary = _pick_primary(rows, _priority_map(name))
    snap = PricingSnapshot.from_payload_json(primary["payload_json"])

    groups: dict[str, dict] = {}
    order = 0
    for t in snap.tiers:
        key = cmp._plan_key(t.name)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "name": cmp._plan_display(t.name), "is_free": False,
                "monthly": None, "annual": None, "price_note": t.price_note,
                "cat_feats": {}, "order": order,
            }
            order += 1
        if _is_free_tier(t):
            g["is_free"] = True
        m, a = t.monthly_price, t.annual_price_per_month
        if m is not None and a is not None:
            g["monthly"] = m if g["monthly"] is None else min(g["monthly"], m)
            g["annual"] = a if g["annual"] is None else min(g["annual"], a)
        else:
            price = m if m is not None else a
            if price is not None:
                kind = cmp._billing_kind(t.name)
                if kind == "annual" or (kind == "other" and a is not None and m is None):
                    g["annual"] = price if g["annual"] is None else min(g["annual"], price)
                else:
                    g["monthly"] = price if g["monthly"] is None else min(g["monthly"], price)
        for f in t.features:
            c = _effective_category(f, cat_map)
            fs = g["cat_feats"].setdefault(c, [])
            if f not in fs:
                fs.append(f)

    # 무료 먼저, 그다음 월가격(연환산) 오름차순
    def sort_key(g):
        eff = g["monthly"] if g["monthly"] is not None else g["annual"]
        return (0 if g["is_free"] else 1, eff if eff is not None else 1e9, g["order"])

    out = []
    for g in sorted(groups.values(), key=sort_key):
        cats = sorted(
            g.pop("cat_feats").items(),
            key=lambda kv: (-cmp.WEIGHTS.get(kv[0], cmp.DEFAULT_WEIGHT), kv[0]),
        )
        g["categories"] = [{"category": c, "features": fs} for c, fs in cats]
        out.append(g)
    return out


def _company_unlock_breakdown(tiers: list[dict]) -> list[dict]:
    """플랜 티어(가격 오름차순)에서 각 가격대에 '처음 풀리는' 기능만 추린다.

    각 기능을 그 기능이 처음 제공되는 가장 싼 티어(가격)에 한 번만 귀속시켜,
    '무료 / 월 $x / 월 $y' 형태의 증분 분리를 만든다.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for tr in tiers:
        feats: list[str] = []
        for cat in tr.get("categories", []):
            feats.extend(cat["features"])
        # canonical 키로 중복 제거 — 표현이 달라도 같은 기능이면 한 번만(가장 싼 티어)
        new = []
        for f in feats:
            k = _normalize_feature(f)
            if k not in seen:
                seen.add(k)
                new.append(f)
        if new:
            if tr["is_free"]:
                label = "무료"
            elif tr["monthly"] is not None:
                label = f"${tr['monthly']:g}/월"
            elif tr["annual"] is not None:
                label = f"${tr['annual']:g}/월(연)"
            else:
                label = tr.get("price_note") or "문의"
            out.append({
                "price_label": label,
                "is_free": tr["is_free"],
                "monthly": tr["monthly"],
                "annual": tr["annual"],
                "price_note": tr.get("price_note"),
                "features": new,
            })
    return out


def _unlock_signature(groups: list[dict]) -> str:
    """증분 데이터의 해시 — AI 분석 이후 데이터가 바뀌었는지(스테일) 감지용."""
    basis = [[g["price_label"], sorted(g["features"])] for g in groups]
    blob = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def unlock_groups_for(name: str) -> tuple[list[dict], str]:
    """업체의 가격대별 증분 그룹과 서명(AI 분석 입력/스테일 판정용)."""
    cat_map = store.get_feature_categories()
    groups = _company_unlock_breakdown(_company_plan_tiers(name, cat_map))
    return groups, _unlock_signature(groups)


# ── 업체간 비교 (/compare) ───────────────────────────────────
def compare(names: list[str]) -> dict:
    """선택 업체 비교: 가격 산점도 + 업체별(카테고리·기능·월가격) + 카테고리 매트릭스.

    카테고리는 저장된 동적 매핑(AI/사용자)을 우선 사용하고, 없으면 키워드 추정.
    """
    from . import compare as cmp

    cat_map = store.get_feature_categories()
    icon_map = {c["name"]: c["icon_url"] for c in store.list_companies(active_only=False)}
    # 업체별 소스 URL — 앱 아이콘이 없을 때 브랜드 도메인 파비콘 폴백용
    src_map: dict[str, list[str]] = {}
    for s in store.list_sources(active_only=True):
        src_map.setdefault(s["company_name"], []).append(s["url"])

    chosen = []
    scatter = []
    per_company = []          # 업체별 카테고리·기능·월가격
    matrix: dict[str, dict[str, int]] = {}
    scores: dict[str, int] = {}
    all_features: set[str] = set()

    for name in names:
        if not store.latest_snapshots_for_company(name):
            continue
        pts, feats, price_info = _company_paid_data(name)
        scatter.append({
            "label": name,
            "data": pts,
            "icon": _company_icon(icon_map.get(name), src_map.get(name, [])),
        })

        cat_feats: dict[str, list[str]] = {}
        for f in feats:
            c = _effective_category(f, cat_map)
            cat_feats.setdefault(c, []).append(f)
            matrix.setdefault(c, {})[name] = matrix.get(c, {}).get(name, 0) + 1
            all_features.add(f)

        plan_tiers = _company_plan_tiers(name, cat_map)
        unlock = _company_unlock_breakdown(plan_tiers)
        ai_unlock, ai_stale = None, False
        ai_row = store.get_pricing_analysis(name)
        if ai_row:
            try:
                ai_unlock = json.loads(ai_row["payload_json"]) or None
            except (ValueError, TypeError):
                ai_unlock = None
            if ai_unlock is not None:
                ai_stale = ai_row["signature"] != _unlock_signature(unlock)
        per_company.append(
            {
                "company": name,
                "icon": _company_icon(icon_map.get(name), src_map.get(name, [])),
                "monthly_price": price_info["monthly"],
                "annual_price": price_info["annual"],
                "tiers": plan_tiers,
                "unlock": unlock,
                "ai_unlock": ai_unlock,
                "ai_stale": ai_stale,
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
        ({"company": n, "score": scores[n], "icon": _company_icon(icon_map.get(n), src_map.get(n, []))}
         for n in chosen),
        key=lambda x: x["score"],
        reverse=True,
    )
    _alias_for_edit = store.get_feature_aliases()
    editable = [
        {"feature": f, "category": _effective_category(f, cat_map),
         "assigned": f in cat_map, "alias": _alias_for_edit.get(f, "")}
        for f in sorted(all_features)
    ]

    # ── 가격대별 분석: $5 단위 밴드(무료 / ~$5 / ~$10 …) → 카테고리 → 업체(기능) ──
    import math
    import statistics

    # AI 유사 기능 통합 매핑(있으면 의미상 같은 기능을 한 그룹으로)
    alias_map = store.get_feature_aliases()
    cheap_usd = get_cheap_threshold()  # '저렴(무료에 준함)' 판정 가격 임계값
    band_usd = get_band_width()        # 가격대별/기능별 분석의 가격 묶음 단위
    commodity_pen = get_commodity_pen() / 100.0  # 커머디티 최소 보급률(설정값)

    def _good_alias(f: str):
        # 별칭(통합명)이 노이즈(제약/부정/포함안내 등)면 신뢰하지 않고 무시 →
        # AI가 무관한 기능을 잘못 묶어둔 옛 별칭(예: Stealth Mode→'Watermarked
        # outputs')이 그대로 남아 오분류되는 것을 방지(원본 이름으로 되돌림).
        a = alias_map.get(f)
        return a if (a and not _skip_feature(a)) else None

    def _canon_key(f: str) -> str:
        a = _good_alias(f)
        return ("ALIAS::" + a) if a else _normalize_feature(f)

    def _canon_disp(f: str) -> str:
        return _good_alias(f) or f

    # 0) 기능(canonical) 보급률·해금가 집계 → 커머디티/차별화 분류 (무료 기능 포함)
    agg: dict[str, dict] = {}
    contributing: set[str] = set()  # 실제로 기능 데이터를 1개 이상 제공한 업체
    for pc in per_company:
        for g in pc.get("unlock", []):
            eff = g["annual"] if g["annual"] is not None else g["monthly"]
            for f in g["features"]:
                if _skip_feature(f):
                    continue  # 포함 안내 문구·광고 관련은 실제 기능이 아님
                contributing.add(pc["company"])
                key = _canon_key(f)
                a = agg.get(key)
                if a is None:
                    a = agg[key] = {"display": _canon_disp(f), "companies": set(),
                                    "prices": [], "detail": {}}
                elif _good_alias(f):
                    a["display"] = _good_alias(f)  # 유효 별칭이 있으면 대표명으로 승격
                a["companies"].add(pc["company"])
                if eff is not None:
                    a["prices"].append(eff)
                d = a["detail"].setdefault(
                    pc["company"], {"features": set(), "is_free": False, "price": None}
                )
                d["features"].add(f)
                if g["is_free"]:
                    d["is_free"] = True
                if eff is not None:
                    d["price"] = eff if d["price"] is None else min(d["price"], eff)

    # 보급률 분모 = '기능 데이터가 있는 업체 수'(데이터 0건 업체는 제외해 희석 방지)
    n_co = len(contributing)

    def _classify(key: str) -> dict:
        # 분류 기준(보급률 = 제공 업체 수 / 전체 업체 수, 저렴 기준가 = cheap_usd):
        #   commodity     : 다수(≥60%)가 제공 + 그중 절반 이상이 무료/기준가 이하 → 기본기
        #   differentiated: 소수(≤⅓)만 제공 + 그중 절반 이상이 유료(무료/기준가 초과)
        #                   — 단, 업체가 3곳 이상일 때만(소표본 과대분류 방지)
        #   standard      : 그 외
        a = agg.get(key)
        if not a or n_co == 0:
            return {"label": "standard", "providers": 0, "penetration": 0.0}
        providers = len(a["companies"])
        pen = providers / n_co
        # 제공 업체 중 '무료/기준가 이하(저렴)' 비율 — 커머디티 판정용.
        cheap = sum(
            1 for dd in a["detail"].values()
            if dd["is_free"] or (dd["price"] is not None and dd["price"] <= cheap_usd)
        )
        entry = (cheap / providers) if providers else 0.0
        # 유료 비율 — 차별화 판정용. '가격이 실제로 확인된 + 기준가 초과'인 업체만
        #   유료로 센다. 가격 미상(None, 화면 $0)·무료·기준가 이하는 유료가 아님.
        paid = sum(
            1 for dd in a["detail"].values()
            if not dd["is_free"]
            and dd["price"] is not None
            and dd["price"] > cheap_usd
        )
        paid_ratio = (paid / providers) if providers else 0.0
        if pen >= commodity_pen and entry >= 0.5:
            label = "commodity"
        elif n_co >= 3 and pen <= 0.34 and paid_ratio >= 0.5:
            label = "differentiated"
        else:
            label = "standard"
        return {"label": label, "providers": providers,
                "penetration": round(pen, 3)}

    feature_positioning = []
    pos_by_key: dict[str, dict] = {}
    for key, a in agg.items():
        cls = _classify(key)
        price = statistics.median(a["prices"]) if a["prices"] else 0.0
        providers_list = sorted(
            (
                {
                    "company": co,
                    "features": sorted(dd["features"]),
                    "is_free": dd["is_free"],
                    "price": dd["price"],
                }
                for co, dd in a["detail"].items()
            ),
            key=lambda e: (e["price"] is None, e["price"] or 0, e["company"]),
        )
        entry = {
            "feature": a["display"],
            "category": _effective_category(a["display"], cat_map),
            "providers": cls["providers"],
            "total": n_co,
            "penetration": cls["penetration"],
            "unlock_price": round(price, 2),
            "label": cls["label"],
            "providers_list": providers_list,
        }
        feature_positioning.append(entry)
        pos_by_key[key] = entry
    # 분류(커머디티 → 표준 → 차별화)별로 묶어서 표시(그 안에서는 보급률·해금가 순)
    _label_rank = {"commodity": 0, "standard": 1, "differentiated": 2}
    feature_positioning.sort(
        key=lambda x: (_label_rank.get(x["label"], 9), -x["penetration"], x["unlock_price"])
    )

    # 카테고리 × 커머디티/차별화 교차표 — '어떤 종류의 기능에서 차별화가 나오나'
    from .extract import FEATURE_CATEGORIES
    _cat_order = {c: i for i, c in enumerate(FEATURE_CATEGORIES)}
    cross: dict[str, dict] = {}
    for e in feature_positioning:
        cat = e["category"] or "미분류"
        row = cross.get(cat)
        if row is None:
            row = cross[cat] = {"category": cat, "commodity": 0,
                                "standard": 0, "differentiated": 0, "total": 0}
        row[e["label"]] += 1
        row["total"] += 1
    for row in cross.values():
        t = row["total"] or 1
        row["diff_pct"] = round(row["differentiated"] / t * 100)
        row["comm_pct"] = round(row["commodity"] / t * 100)
    # 고정 대분류 순서 → 그 외(미분류 등)는 뒤로, 차별화 비율 높은 순 보조정렬
    positioning_cross = sorted(
        cross.values(),
        key=lambda r: (_cat_order.get(r["category"], 99), -r["diff_pct"], -r["total"]),
    )

    # 1) 같은 기능(canonical)이 여러 업체·가격대에 나타나면 '가장 싼' 한 곳만 남긴다
    best: dict[str, dict] = {}
    for pc in per_company:
        for g in pc.get("unlock", []):
            eff = g["annual"] if g["annual"] is not None else g["monthly"]
            # 정렬 가격: 무료/저가 우선, 미공개는 맨 뒤
            cand_price = eff if eff is not None else float("inf")
            for f in g["features"]:
                if _skip_feature(f):
                    continue  # 포함 안내 문구·광고 관련은 실제 기능이 아님
                key = _canon_key(f)
                rank = (cand_price, pc["company"])
                cur = best.get(key)
                if cur is None or rank < cur["_rank"]:
                    best[key] = {
                        "feature": f, "company": pc["company"], "icon": pc["icon"],
                        "eff": eff, "is_free": g["is_free"],
                        "price_note": g["price_note"], "_rank": rank,
                    }

    # 2) 중복 제거된 기능들만으로 밴드 → 카테고리 → 업체 구성
    bands: dict = {}
    for e in best.values():
        eff = e["eff"]
        lower = None
        if e["is_free"]:
            bkey, order, label, ub = "free", (0, 0.0), None, None
        elif eff is not None:
            ub = max(band_usd, math.ceil(eff / band_usd) * band_usd)
            lower = max(0.0, ub - band_usd)
            ub_disp = "%g" % ub
            bkey, order, label = f"b{ub_disp}", (1, float(ub)), f"~${ub_disp}"
        else:
            bkey, order, label, ub = "note", (2, 0.0), e["price_note"] or "문의", None
        b = bands.get(bkey)
        if b is None:
            b = bands[bkey] = {
                "is_free": e["is_free"] and bkey == "free",
                "label": label,
                "upper": ub,
                "lower": lower,
                "_order": order,
                "_cats": {},  # category -> {company -> entry}
            }
        c = _effective_category(e["feature"], cat_map)
        comp_map = b["_cats"].setdefault(c, {})
        entry = comp_map.get(e["company"])
        if entry is None:
            entry = comp_map[e["company"]] = {
                "company": e["company"],
                "icon": e["icon"],
                "price": eff,
                "features": [],
            }
        entry["features"].append(e["feature"])

    price_bands = []
    for b in sorted(bands.values(), key=lambda x: x.pop("_order")):
        cats = sorted(
            b.pop("_cats").items(),
            key=lambda kv: (-cmp.WEIGHTS.get(kv[0], cmp.DEFAULT_WEIGHT), kv[0]),
        )
        b["categories"] = [
            {
                "category": c,
                "companies": sorted(
                    comp_map.values(), key=lambda e: (e["price"] is None, e["price"] or 0, e["company"])
                ),
            }
            for c, comp_map in cats
        ]
        price_bands.append(b)

    # ── 기능별 분석: 카테고리 → 가격대(열) → 세부 기능·업체 (price_bands 전치) ──
    cat_bands: dict[str, dict] = {}     # category -> {band_index -> [items]}
    band_descs: list[dict] = []         # band_index -> {is_free, label, upper}
    for bi, b in enumerate(price_bands):
        band_descs.append({"is_free": b["is_free"], "label": b["label"], "upper": b["upper"]})
        for cat in b["categories"]:
            slot = cat_bands.setdefault(cat["category"], {}).setdefault(bi, [])
            for co in cat["companies"]:
                for f in co["features"]:
                    key = _canon_key(f)
                    pe = pos_by_key.get(key, {})
                    slot.append({
                        "feature": pe.get("feature", f),  # 대표명(별칭 통합 반영)
                        "label": pe.get("label", "standard"),
                        "providers": pe.get("providers", 0),
                        "total": n_co,
                        "providers_list": pe.get("providers_list", []),
                    })
    feature_analysis = []
    for c in sorted(cat_bands, key=lambda c: (-cmp.WEIGHTS.get(c, cmp.DEFAULT_WEIGHT), c)):
        bands_out = [
            {**band_descs[bi], "entries": cat_bands[c][bi]}
            for bi in sorted(cat_bands[c])
        ]
        feature_analysis.append({"category": c, "bands": bands_out})

    # 번들 업체·번들 구성요소(원가 수집용)는 일반 비교 선택에서 제외
    all_company_names = sorted(
        c["name"] for c in store.list_companies(active_only=True)
        if not c["is_bundle"] and not c["is_component"]
    )
    cat_chips, company_cat = _company_category_picker(all_company_names)
    return {
        "companies": chosen,
        "scatter": {"datasets": scatter},
        "per_company": per_company,
        "price_bands": price_bands,
        "feature_analysis": feature_analysis,
        "feature_positioning": feature_positioning,
        "positioning_cross": positioning_cross,
        "matrix": matrix_rows,
        "ranking": ranking,
        "editable": editable,
        "cheap_usd": cheap_usd,
        "band_usd": band_usd,
        "commodity_pen": get_commodity_pen(),
        "all_companies": all_company_names,
        "category_chips": cat_chips,
        "company_cat": company_cat,
    }


def _company_category_picker(names: list[str]) -> tuple[list[dict], dict[str, int | None]]:
    """업체 선택 UI용: (분류별 개수 칩, {업체명: category_id}).

    분류는 빈 것도 노출(탭으로 보이게), 미분류는 해당 업체가 있을 때만.
    """
    cat_list, name_to_id, _id_to_name = _category_context()
    company_cat = {n: name_to_id.get(n) for n in names}
    chips = []
    for cat in cat_list:
        cnt = sum(1 for n in names if company_cat.get(n) == cat["id"])
        if cnt:  # 해당 목록에 속한 업체가 있는 분류만 노출(번들 전용 분류 등 제외)
            chips.append({"id": cat["id"], "name": cat["name"], "count": cnt})
    n_uncat = sum(1 for n in names if not company_cat.get(n))
    if n_uncat:
        chips.append({"id": None, "name": None, "count": n_uncat})
    return chips, company_cat


def distinct_paid_features(names: list[str]) -> list[str]:
    """선택 업체들의 유료 기능 합집합(AI 카테고리 분류용)."""
    out: list[str] = []
    for name in names:
        _, feats, _ = _company_paid_data(name)
        for f in feats:
            if f not in out:
                out.append(f)
    return out


def distinct_features(names: list[str]) -> list[str]:
    """선택 업체들의 전체 기능 합집합(무료+유료) — AI 카테고리 분류 대상.

    무료 티어 기능까지 포함해 'AI'·'기타' 휴리스틱 버킷에 남는 기능을 줄인다.
    """
    cat_map = store.get_feature_categories()
    out: list[str] = []
    for name in names:
        for tr in _company_plan_tiers(name, cat_map):
            for cat in tr["categories"]:
                for f in cat["features"]:
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
    if not isinstance(data, dict):
        return None
    # 예전 구조로 저장된 카드 호환: 누락된 키를 안전 기본값으로 채운다
    # (새 템플릿이 data.feature_positioning 등을 tojson/순회하다 깨지는 것 방지).
    defaults = {
        "companies": [],
        "scatter": {"datasets": []},
        "per_company": [],
        "price_bands": [],
        "feature_analysis": [],
        "feature_positioning": [],
        "positioning_cross": [],
        "matrix": [],
        "ranking": [],
        "editable": [],
    }
    for k, v in defaults.items():
        if data.get(k) is None:
            data[k] = v
    # 범례 표시용 — 저장 카드엔 없을 수 있으니 현재 전역 임계값으로 보강(표시용)
    if data.get("cheap_usd") is None:
        data["cheap_usd"] = get_cheap_threshold()
    if data.get("band_usd") is None:
        data["band_usd"] = get_band_width()
    if data.get("commodity_pen") is None:
        data["commodity_pen"] = get_commodity_pen()
    # 선택 목록(체크박스)은 현재 업체 기준으로 갱신해 새 비교 시작이 가능하도록.
    data["all_companies"] = sorted(
        c["name"] for c in store.list_companies(active_only=True)
        if not c["is_bundle"] and not c["is_component"]
    )
    data["category_chips"], data["company_cat"] = _company_category_picker(
        data["all_companies"]
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
