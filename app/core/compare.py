"""업체간 유료 기능 비교 — 기능 카테고리화 + 값어치(가중치) 산정.

LLM 없이 키워드 규칙으로 분류한다(무료·투명·결정적). 필요 시 추후 AI 판정으로 교체 가능.
"""
from __future__ import annotations

# (카테고리, 키워드들, 값어치 가중치)
CATEGORY_RULES: list[tuple[str, list[str], int]] = [
    ("보안·관리", ["sso", "saml", "scim", "audit", "security", "compliance",
                 "admin", "permission", "encryption", "2fa", "mfa",
                 "provision", "soc 2", "hipaa", "gdpr", "관리", "보안", "감사"], 3),
    ("AI", ["ai", "gpt", "assistant", "copilot", "machine learning", "ml ", "인공지능"], 3),
    ("개발·통합", ["api", "integration", "webhook", "sdk", "developer",
                "automation", "zapier", "연동", "통합", "자동화"], 2),
    ("분석·리포트", ["analytic", "report", "dashboard", "insight", "metric",
                 "export", "분석", "리포트", "대시보드", "내보내기"], 2),
    ("지원", ["support", "sla", "priority", "onboarding", "success manager",
            "training", "지원", "우선", "온보딩"], 2),
    ("협업", ["collaborat", "team", "member", "guest", "share", "comment",
            "workspace", "role", "협업", "팀", "공유", "멤버"], 1),
    ("저장·용량", ["storage", "gb", "tb", "upload", "file size", "limit",
               "unlimited", "history", "저장", "용량", "업로드", "무제한"], 1),
    ("커스터마이즈", ["custom", "branding", "white label", "theme", "domain",
                "커스텀", "브랜드", "도메인"], 1),
]
DEFAULT_CATEGORY = "기타"
DEFAULT_WEIGHT = 1

CATEGORIES = [c for c, _, _ in CATEGORY_RULES] + [DEFAULT_CATEGORY]
WEIGHTS = {c: w for c, _, w in CATEGORY_RULES}
WEIGHTS[DEFAULT_CATEGORY] = DEFAULT_WEIGHT


import re

# 결제주기 티어 이름(한 플랜의 월/연 결제 옵션 → 하나로 병합)
_BILLING_RE = re.compile(
    r"^\s*(\d+[-\s]?month(s)?|monthly|month|annual(ly)?|yearly|year|"
    r"quarter(ly)?|week(ly)?|bi[-\s]?annual|semi[-\s]?annual|연간|월간|연|월)\s*$",
    re.IGNORECASE,
)


def is_billing_variant(name: str) -> bool:
    return bool(_BILLING_RE.match(name or ""))


def _billing_kind(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ("annual", "yearly", "year", "연")):
        return "annual"
    if n.strip() in ("monthly", "month", "월", "월간") or n.strip() == "monthly":
        return "monthly"
    return "other"  # 6-month 등 중간 주기


def merge_billing_points(tiers) -> tuple[list[dict], list]:
    """결제주기 변형 티어들을 (월, 연환산) 한 점으로 병합.

    반환: (병합 점 리스트, 병합되지 않은 나머지 티어들).
    결제주기 티어가 2개 미만이면 병합하지 않는다(실제 플랜 티어 보호).
    """
    variants = [t for t in tiers if is_billing_variant(t.name)]
    others = [t for t in tiers if not is_billing_variant(t.name)]
    if len(variants) < 2:
        return [], list(tiers)

    def eff(t):
        return t.annual_price_per_month if t.annual_price_per_month is not None else t.monthly_price

    monthly_eff = annual_eff = None
    for t in variants:
        e = eff(t)
        if e is None:
            continue
        kind = _billing_kind(t.name)
        if kind == "monthly":
            monthly_eff = e if monthly_eff is None else min(monthly_eff, e)
        else:  # annual / other(6-month 등)는 장기 결제 → 연환산 후보
            annual_eff = e if annual_eff is None else min(annual_eff, e)

    x = monthly_eff if monthly_eff is not None else annual_eff
    y = annual_eff if annual_eff is not None else monthly_eff
    points = [{"x": x, "y": y, "tier": "구독"}] if x is not None else []
    return points, others


def categorize_feature(feature: str) -> list[str]:
    """기능 문자열을 1개 이상의 카테고리로 분류(매칭 없으면 기타)."""
    f = feature.lower()
    cats = [c for c, kws, _ in CATEGORY_RULES if any(k in f for k in kws)]
    return cats or [DEFAULT_CATEGORY]
