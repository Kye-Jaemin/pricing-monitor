"""Pydantic v2 스키마 — 추출 결과 강제 검증 (5장).

Claude 추출 결과 JSON 을 이 스키마로 검증한다. 검증 실패 시 pipeline 이
1회 재시도하고, 그래도 실패하면 run_logs 에 에러로 기록한다.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

BillingUnit = Literal["per_user", "flat", "usage_based", "unknown"]
Confidence = Literal["high", "medium", "low"]

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


class PricingSnapshot(BaseModel):
    """한 업체의 한 회차 수집 결과."""

    company: str
    source_url: str
    collected_at: str  # ISO8601 UTC, e.g. "2026-06-08T09:00:00Z"
    currency: str = "USD"
    tiers: list[Tier] = Field(default_factory=list)
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
        "extraction_confidence": "high",
    },
    ensure_ascii=False,
    indent=2,
)
