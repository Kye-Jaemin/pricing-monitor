"""Claude API 구조화 추출 (6장).

페이지 본문 텍스트 → 정해진 JSON 스키마. 검증은 호출자(pipeline)가 Pydantic 으로.
"""
from __future__ import annotations

import json
import re

from .. import config
from .models import SCHEMA_JSON_EXAMPLE

PROMPT_TEMPLATE = """You are a pricing data extractor. From the given page text, extract the
subscription pricing tiers. Return ONLY valid JSON matching this schema:
{schema}

Rules:
- Prices are expected in USD. If the page shows a non-USD currency, set
  currency accordingly and lower extraction_confidence.
- Capture EVERY plan/tier you can find: the free tier (if any) AND all paid
  plans (Pro, Team, Business, Enterprise, etc.). The free tier is a tier with
  monthly_price 0; list its included features so free-vs-paid differences are clear.
- If a tier has no public price (e.g. "Contact Sales"), set prices to null and
  put the reason in price_note.
- "free_trial": describe any free trial offer (length, which plans it applies to,
  whether a credit card is required). If there is no free trial, set it to null.
  A free trial is different from a free tier — do not confuse them.
- Do not invent features or prices. If unsure, lower extraction_confidence.
{source_hint}- "company" must be exactly: {company}
- "source_url" must be exactly: {source_url}
- "collected_at" must be exactly: {collected_at}
- Output JSON only. No prose, no code fences.

PAGE TEXT:
{page_text}
"""

# 소스 타입별 추출 힌트(레이아웃이 다르므로 보강)
SOURCE_HINTS = {
    "google_search": (
        "- This is a Google search results page. Prices usually appear INSIDE the "
        "result snippets / AI overview (e.g. '$11.99/month', '$71.99/year', "
        "'Premium $9.99', 'Pro plan starts at $15'). You MUST extract these.\n"
        "  For every price you see, create a tier: use the plan name if the snippet "
        "shows one (e.g. 'Premium', 'Pro'); otherwise name it by billing period "
        "('Monthly', 'Annual') or 'Subscription'. Put the exact snippet wording in "
        "price_note. Put monthly amounts in monthly_price; for a yearly amount, set "
        "annual_price_per_month to (yearly / 12). Also capture any free tier or free "
        "trial mentioned. Set extraction_confidence to medium when you found prices. "
        "Return an empty tiers list (confidence low) ONLY if no price appears "
        "anywhere in the results.\n"
    ),
    "apple": (
        "- This is an Apple App Store listing. Extract the in-app "
        "subscription/purchase tiers shown (use US storefront USD prices). "
        "Map each subscription option to a tier.\n"
    ),
    "google_play": (
        "- This is a Google Play Store listing. Extract the in-app "
        "subscription/purchase tiers shown (use US, USD prices). "
        "If only a price range is given, put it in price_note and lower confidence.\n"
    ),
}

# 페이지 텍스트가 너무 길면 토큰 절약을 위해 잘라낸다(가격은 보통 상단에 있음).
MAX_PAGE_CHARS = 40000


class ExtractError(RuntimeError):
    """추출 실패."""


def _strip_code_fences(text: str) -> str:
    """모델이 실수로 ```json ... ``` 으로 감쌌을 때 제거."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def extract_pricing(
    *,
    company: str,
    source_url: str,
    collected_at: str,
    page_text: str,
    source_type: str = "web",
) -> dict:
    """Claude 로 구조화 추출하여 dict 를 돌려준다(스키마 검증은 호출자 책임).

    JSON 파싱 자체가 실패하면 ExtractError.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")

    # 함수 안에서 import — core import 만으로 anthropic 을 강제하지 않는다.
    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = PROMPT_TEMPLATE.format(
        schema=SCHEMA_JSON_EXAMPLE,
        source_hint=SOURCE_HINTS.get(source_type, ""),
        company=company,
        source_url=source_url,
        collected_at=collected_at,
        page_text=page_text[:MAX_PAGE_CHARS],
    )

    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    raw = _strip_code_fences(raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"JSON 파싱 실패: {exc}\n원문 앞부분: {raw[:300]}") from exc
