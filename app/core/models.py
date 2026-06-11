"""Pydantic v2 스키마 — 추출 결과 강제 검증 (5장).

Claude 추출 결과 JSON 을 이 스키마로 검증한다. 검증 실패 시 pipeline 이
1회 재시도하고, 그래도 실패하면 run_logs 에 에러로 기록한다.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

BillingUnit = Literal["per_user", "flat", "usage_based", "unknown"]
Confidence = Literal["high", "medium", "low"]

# LLM 추출이 enum 밖 값을 낼 때 가까운 표준 값으로 흡수(없으면 'unknown').
#  - 좌석/인당 과금 → per_user
#  - 그룹(가족·가구·기기·계정 등) 고정가 → flat
#  - 사용량/크레딧 기반 → usage_based
_BILLING_UNIT_SYNONYMS: dict[str, str] = {
    "per_seat": "per_user", "seat": "per_user", "per_member": "per_user",
    "member": "per_user", "per_agent": "per_user", "per_editor": "per_user",
    "per_person": "per_user", "person": "per_user", "per_user_per_month": "per_user",
    "per_family": "flat", "per_household": "flat", "household": "flat",
    "family": "flat", "per_device": "flat", "device": "flat",
    "per_account": "flat", "account": "flat", "per_workspace": "flat",
    "per_organization": "flat", "per_org": "flat", "per_site": "flat",
    "per_team": "flat", "fixed": "flat", "flat_rate": "flat",
    "metered": "usage_based", "usage": "usage_based", "consumption": "usage_based",
    "pay_as_you_go": "usage_based", "credit": "usage_based", "credits": "usage_based",
    "token": "usage_based", "tokens": "usage_based",
}

# 소스 타입 — 한 업체가 여러 소스를 가질 수 있다.
# 우선순위(숫자 작을수록 우선): 공식 홈페이지 > 구글 검색 > App Store > Play Store
SOURCE_TYPES = ["web", "google_search", "apple", "google_play", "other"]
SOURCE_TYPE_LABELS = {
    "web": "공식 홈페이지",
    "google_search": "구글 검색",
    "apple": "App Store",
    "google_play": "Play Store",
    "other": "기타",
}
SOURCE_PRIORITY = {
    "web": 1,
    "google_search": 2,
    "apple": 3,
    "google_play": 4,
    "other": 5,
}


class Tier(BaseModel):
    name: str
    monthly_price: Optional[float] = None
    annual_price_per_month: Optional[float] = None
    billing_unit: BillingUnit = "unknown"
    price_note: Optional[str] = None
    features: list[str] = Field(default_factory=list)
    limits: dict[str, str] = Field(default_factory=dict)

    @field_validator("billing_unit", mode="before")
    @classmethod
    def _coerce_billing_unit(cls, v):
        """허용 외 값(예: per_family)을 표준 값으로 흡수. 모르면 unknown."""
        if v is None:
            return "unknown"
        s = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        if s in ("per_user", "flat", "usage_based", "unknown"):
            return s
        return _BILLING_UNIT_SYNONYMS.get(s, "unknown")


class PricingSnapshot(BaseModel):
    """한 업체의 한 회차 수집 결과."""

    company: str
    source_url: str
    collected_at: str  # ISO8601 UTC, e.g. "2026-06-08T09:00:00Z"
    currency: str = "USD"
    tiers: list[Tier] = Field(default_factory=list)
    # 무료 체험(Free Trial) 제공 여부·조건. 없으면 null.
    # 예: "14-day free trial on Pro/Business, no credit card required"
    free_trial: Optional[str] = None
    extraction_confidence: Confidence = "medium"

    def to_payload_json(self) -> str:
        """DB snapshots.payload_json 에 저장할 정규화 JSON 문자열."""
        return json.dumps(
            self.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_payload_json(cls, payload: str) -> "PricingSnapshot":
        return cls.model_validate_json(payload)


# 추출 프롬프트에 삽입할 스키마 설명 (extract.py 가 사용)
SCHEMA_JSON_EXAMPLE = json.dumps(
    {
        "company": "Notion",
        "source_url": "https://www.notion.so/pricing",
        "collected_at": "2026-06-08T09:00:00Z",
        "currency": "USD",
        "tiers": [
            {
                "name": "Free",
                "monthly_price": 0,
                "annual_price_per_month": 0,
                "billing_unit": "per_user",
                "price_note": None,
                "features": ["Unlimited pages", "Basic collaboration"],
                "limits": {"file_upload": "5MB"},
            },
            {
                "name": "Enterprise",
                "monthly_price": None,
                "annual_price_per_month": None,
                "billing_unit": "per_user",
                "price_note": "Contact Sales",
                "features": ["SAML SSO", "Audit log"],
                "limits": {},
            },
        ],
        "free_trial": "14-day free trial on Plus/Business, no credit card required",
        "extraction_confidence": "high",
    },
    ensure_ascii=False,
    indent=2,
)
