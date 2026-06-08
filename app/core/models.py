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
