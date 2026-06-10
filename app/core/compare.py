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


def categorize_feature(feature: str) -> list[str]:
    """기능 문자열을 1개 이상의 카테고리로 분류(매칭 없으면 기타)."""
    f = feature.lower()
    cats = [c for c, kws, _ in CATEGORY_RULES if any(k in f for k in kws)]
    return cats or [DEFAULT_CATEGORY]
