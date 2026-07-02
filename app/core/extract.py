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
- ALWAYS normalize every price to a per-MONTH USD amount. Watch the billing
  PERIOD carefully (per week / per month / per year are very different):
  - Weekly "$X/week" (common in diet/fitness apps) -> monthly_price = X * 4.345
    (weeks per month). Put the ORIGINAL "$X/week" in price_note. NEVER store a
    weekly price directly as monthly_price.
  - Daily "$X/day" -> monthly_price = X * 30; note the original in price_note.
  - Quarterly / 6-month (longer commitment billed upfront) -> set
    annual_price_per_month = total / number_of_months (e.g. 6-month $60 -> 10);
    note the original term in price_note.
- annual_price_per_month is ALWAYS the per-MONTH cost when billed annually
  (= annual total / 12), never the annual lump sum. E.g. "$71.99/year" -> 5.99.
- If the SAME plan is sold with multiple billing periods (e.g. "Weekly $6.99,
  Monthly $11.99, Annual $71.99/yr ($5.99/mo)"), represent it as ONE tier:
  monthly_price = the monthly-billing price (or weekly*4.345 if only weekly is
  offered), annual_price_per_month = annual total / 12, and note the other terms
  (weekly/6-month) in price_note. Do NOT create separate
  "Weekly"/"Monthly"/"Annual" tiers for one plan.
- Pricing pages often have a Monthly/Annual TOGGLE and show a struck-through
  original next to the discounted price (e.g. "PLUS $59 $47 per month, billed
  annually", "$129 $99 per month, billed annually", plus "Save $144 compared to
  monthly"). Here the SMALLER "$47 per month, billed annually" is the
  ANNUAL-billing per-month -> annual_price_per_month = 47, and the struck-through
  "$59" is the MONTHLY-billing per-month -> monthly_price = 59. Capture BOTH on
  ONE tier so the annual discount is preserved (do NOT keep only the displayed
  annual price). If only the annual per-month price P is shown together with
  "Save $X compared to monthly", infer monthly_price = P + X/12. If a plan says
  "no difference compared to monthly", set monthly_price = annual_price_per_month.
- Write every tier "name" and each "features" item in the SAME language as the
  page text: a Korean page -> Korean features, an English page -> English. Keep
  brand/product names (Netflix, Naver, etc.), numbers, and currencies as-is.
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
        "annual_price_per_month to (yearly / 12); for a WEEKLY amount set "
        "monthly_price to (weekly * 4.345) and keep the original '$X/week' in "
        "price_note. For each paid tier, also list the "
        "key features included (from the snippets/AI overview) in the features array, "
        "not just the price. Capture any free tier or free trial mentioned. "
        "Set extraction_confidence to medium when you found prices. "
        "Return an empty tiers list (confidence low) ONLY if no price appears "
        "anywhere in the results.\n"
    ),
    "apple": (
        "- This is an Apple App Store listing. Extract the in-app "
        "subscription/purchase tiers shown (use US storefront USD prices). "
        "Map each subscription option to a tier. WATCH the billing period — many "
        "apps sell WEEKLY subscriptions (e.g. '$6.99/week'); convert weekly to "
        "monthly_price = weekly * 4.345 and keep the original in price_note.\n"
    ),
    "google_play": (
        "- This is a Google Play Store listing. Extract the in-app "
        "subscription/purchase tiers shown (use US, USD prices). "
        "If only a price range is given, put it in price_note and lower confidence. "
        "WATCH the billing period — convert any WEEKLY price to "
        "monthly_price = weekly * 4.345 and keep the original '$X/week' in price_note.\n"
    ),
}

# 페이지 텍스트가 너무 길면 토큰 절약을 위해 잘라낸다(가격은 보통 상단에 있음).
MAX_PAGE_CHARS = 40000


class ExtractError(RuntimeError):
    """추출 실패."""


def _clean_page_text(text: str) -> str:
    """가격 인식을 방해하는 형식을 정규화.

    구글 AI 오버뷰 등은 가격을 LaTeX 수식으로 감싸 내보낸다:
        \\(\\$11.99\\) USD / month  →  $11.99 USD / month
    이런 래핑을 제거해 모델이 평문 가격으로 읽도록 한다.
    """
    if not text:
        return text
    # LaTeX 인라인/디스플레이 수식 구분자 제거
    text = re.sub(r"\\[()\[\]]", " ", text)
    # 이스케이프된 달러/공백
    text = text.replace("\\$", "$").replace("\\,", "").replace("\\;", " ")
    return text


def _strip_code_fences(text: str) -> str:
    """모델이 실수로 ```json ... ``` 으로 감쌌을 때 제거."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _loads_loose(raw: str):
    """JSON 느슨 파싱: 코드펜스 제거 → 직접 파싱 → 실패 시 첫 { ~ 마지막 } 구간 재시도."""
    s = _strip_code_fences(raw)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 앞뒤에 잡설/잘림이 있을 때 가장 바깥 객체만 추출 시도
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(s[start:end + 1])
    raise json.JSONDecodeError("no JSON object found", s, 0)


# 고정 대분류(12개 + 기타). AI가 자유 생성하지 않고 반드시 이 안에서 고른다 → 일관성.
FEATURE_CATEGORIES = [
    "생성", "편집·후처리", "품질·해상도", "내보내기", "크레딧·사용량",
    "속도·성능", "협업·팀", "저장·자산", "통합·API", "보안·관리",
    "라이선스·상업이용", "지원", "기타",
]
_FEATURE_CATEGORY_SET = set(FEATURE_CATEGORIES)
_CATEGORY_GUIDE = (
    "생성=creating/generating new content; 편집·후처리=editing or enhancing existing "
    "content; 품질·해상도=resolution, quality, fidelity of output; 내보내기=export, "
    "download, output formats, watermark-free output; 크레딧·사용량=credits, tokens, "
    "usage quotas & limits; 속도·성능=speed, priority, concurrency, GPU; 협업·팀=team, "
    "sharing, seats, permissions; 저장·자산=storage, library, history, assets; "
    "통합·API=API, integrations, plugins, webhooks; 보안·관리=SSO/SAML, audit, admin, "
    "security, compliance; 라이선스·상업이용=commercial license, usage rights, "
    "ownership; 지원=support, onboarding, SLA. If truly none fit, use 기타."
)


def categorize_features_ai(features: list[str]) -> dict:
    """기능 목록을 '고정 대분류 12개' 중 하나로 분류한다(자유 생성 금지 → 일관성).

    반환: {기능 문자열: 카테고리명}. 목록 밖 값은 '기타'로 흡수. 파싱 실패 시 ExtractError.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    if not features:
        return {}

    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # 기능이 많으면 응답 JSON 이 max_tokens 에서 잘려 파싱 실패하므로 묶음 처리.
    BATCH = 40
    cat_list_str = ", ".join(FEATURE_CATEGORIES)
    result: dict = {}
    for i in range(0, len(features), BATCH):
        chunk = features[i:i + BATCH]
        feat_list = "\n".join(f"- {f}" for f in chunk)
        prompt = (
            "Assign each subscription feature to EXACTLY ONE of these FIXED categories "
            "(copy the Korean category name verbatim — do NOT invent new categories):\n"
            f"{cat_list_str}\n"
            f"Guide — {_CATEGORY_GUIDE}\n"
            "Return ONLY a JSON object mapping each feature (verbatim) to its category "
            "(the value MUST be exactly one of the list above). No prose, no code fences.\n\n"
            f"FEATURES:\n{feat_list}\n"
        )
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            data = _loads_loose(raw)
        except json.JSONDecodeError as exc:
            raise ExtractError(f"카테고리 JSON 파싱 실패: {exc}") from exc
        if not isinstance(data, dict):
            raise ExtractError("카테고리 응답이 객체(JSON object)가 아닙니다.")
        for k, v in data.items():
            if v:
                cat = str(v).strip()
                if cat not in _FEATURE_CATEGORY_SET:
                    cat = "기타"     # 목록 밖 값은 흡수(일관성 유지)
                result[str(k)] = cat
    return result


def dedupe_features_ai(features: list[str]) -> dict:
    """의미상 같은 기능들을 묶어 대표(통합) 이름을 부여한다.

    반환: {원본 기능: 통합 기능명}. 같은 클러스터의 기능들은 동일한 통합명을 받는다.
    정말 같은 기능만 묶고, 구별되는 기능은 자기 자신을 통합명으로 둔다.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    if not features:
        return {}

    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    rules = (
        "You merge duplicate product features into clusters and give each cluster ONE "
        "canonical name. Features that mean the same thing MUST receive the EXACT same "
        "canonical name (character-for-character). Keep genuinely different capabilities "
        "separate.\n"
        "Two features are the SAME capability — MERGE them — when they differ only by:\n"
        "  • wording or language — 'API access' / 'API 액세스' / 'Developer API'\n"
        "  • a quantity, cap, or limit on the SAME capability — 'AI food scanner' / "
        "'Capped AI scans' / 'Limited AI scans' / '10 AI scans per day' / 'Unlimited AI "
        "scans' all mean the AI-scan capability → one canonical 'AI scans'\n"
        "  • a qualifier of degree/tier on the same thing — 'Basic analytics' / "
        "'Advanced analytics' / 'Analytics' → 'Analytics'; 'Priority support' / "
        "'Standard support' / 'Email support' → 'Support'\n"
        "  • a MODIFIER word in front of a generic consumable unit (credits, tokens, "
        "points, minutes) — the unit is the capability, the modifier is just what it is "
        "spent on: 'Credits' / 'Generative credits' / 'AI credits' / 'Monthly credits' / "
        "'Image credits' / 'Render credits' all → 'Credits'; 'Tokens' / 'AI tokens' → "
        "'Tokens'. (Merge these even if the modifier looks meaningful.)\n"
        "Do NOT merge features that deliver a genuinely different outcome, even if they "
        "share a word OR belong to the same theme/domain. Being about the same topic is "
        "NOT enough — merge ONLY when it is literally the SAME capability. These must stay "
        "SEPARATE (never merge): 'Barcode scanner' vs 'AI food scanner'; 'Watermarked "
        "outputs' (a free-tier limitation) vs 'Stealth/private mode — hide generations from "
        "a public gallery' vs 'Remove watermark' (these are THREE different things); 'Cloud "
        "storage' vs 'Cloud rendering'; 'Image generation' vs 'Image editing'; 'Commercial "
        "license' vs 'Priority generation'. Merge WHENEVER it is the same capability (per the "
        "merge rules above, including modifier-word cases like 'Generative credits' → "
        "'Credits'); keep separate ONLY when the underlying capability genuinely differs, "
        "like the specific examples just listed.\n"
        "Strip caps/limits/tier adjectives (capped, limited, unlimited, basic, advanced, "
        "premium, pro, '/day', 'per month', numbers) from the canonical name — name the "
        "capability itself, in the clearest shortest wording, matching the dominant "
        "language of the inputs.\n"
        "Return ONLY a JSON object mapping each feature (verbatim) to its canonical name. "
        "No prose, no code fences.\n"
    )

    def _run(chunk: list[str], known: list[str]) -> dict:
        feat_list = "\n".join(f"- {f}" for f in chunk)
        known_hint = (
            f"Reuse these existing canonical names whenever a feature means the same "
            f"thing: {', '.join(known)}.\n" if known else ""
        )
        prompt = rules + known_hint + f"\nFEATURES:\n{feat_list}\n"
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            data = _loads_loose(raw)
        except json.JSONDecodeError as exc:
            raise ExtractError(f"유사 기능 통합 JSON 파싱 실패: {exc}") from exc
        if not isinstance(data, dict):
            raise ExtractError("유사 기능 통합 응답이 객체(JSON object)가 아닙니다.")
        return data

    # 작은 집합(대부분의 경우)은 한 번에 처리해 전역적으로 일관되게 군집화한다.
    # (배치로 쪼개면 같은 기능이 서로 다른 배치에 흩어져 통합이 누락됨)
    BATCH = 120
    result: dict = {}
    known: list[str] = []
    for i in range(0, len(features), BATCH):
        data = _run(features[i:i + BATCH], known)
        for k, v in data.items():
            if v:
                canon = str(v)
                result[str(k)] = canon
                if canon not in known:
                    known.append(canon)
    return result


def find_similar_features_ai(query: str, candidates: list[str]) -> dict:
    """주어진 후보 기능 목록에서 query 와 의미상 가장 유사한 기능들을 골라
    카테고리별로 묶어 돌려준다.

    반환: {"groups": [{"category": str, "features": [<후보에 있던 이름 그대로>]}]}
    후보에 없는 이름은 만들지 않는다(환각 방지).
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    if not query or not candidates:
        return {"groups": []}

    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    payload = json.dumps({"query": query, "features": candidates}, ensure_ascii=False)
    prompt = (
        "From a fixed FEATURES list, find the ones semantically similar or related to "
        "QUERY (same capability, or adjacent/sibling capability). Group the picked "
        "features under short category names. Order groups from most to least relevant. "
        "Use ONLY feature strings that appear VERBATIM in FEATURES (do not invent or "
        "rephrase). Pick at most ~15 features total; if nothing is related, return empty "
        "groups. Match the dominant language of the features for category names.\n"
        "Return ONLY JSON: {\"groups\":[{\"category\":\"...\",\"features\":[\"...\"]}]}. "
        "No prose, no code fences.\n\n"
        f"INPUT:\n{payload}\n"
    )
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"유사 기능 검색 JSON 파싱 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractError("유사 기능 검색 응답이 객체(JSON object)가 아닙니다.")
    allowed = set(candidates)
    groups = []
    for g in data.get("groups", []) or []:
        feats = [str(f) for f in (g.get("features") or []) if str(f) in allowed]
        if feats:
            groups.append({"category": str(g.get("category") or "기타"), "features": feats})
    return {"groups": groups}


def extract_bundles_ai(company: str, raw_text: str, anchor: str | None = None) -> dict:
    """번들 제공자(통신사/애그리게이터) 페이지 원문에서 번들 요금제를 구조화 추출.

    반환: {"anchor": str, "plans": [{name, provider, monthly, annual, price_note,
            services: [{name, category}]}]}
    텍스트에 있는 내용만 사용(환각 방지). 가격은 USD 숫자 또는 null.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    if not raw_text:
        return {"anchor": anchor or "", "plans": []}

    from anthropic import Anthropic

    def _num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        sv = str(v).replace(",", "")
        if "%" in sv:
            return None  # 'Up to 5% points' 같은 비율 값은 정가가 아님
        # "1,650 KRW/month", "~3,000 to 5,000원" 같은 문자열 → 첫 숫자(범위면 하한)
        m = re.search(r"\d+(?:\.\d+)?", sv)
        return float(m.group()) if m else None

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    # 번들 검색 결과는 길 수 있어, 뒤쪽 요금제(예: SKT 5GX)가 잘리지 않도록 넉넉히.
    text = raw_text[:40000]
    anchor_line = (
        f"This page is about bundles built around '{anchor}'. Extract EVERY plan that "
        f"includes '{anchor}' as a benefit — BOTH dedicated bundle/pass products AND larger "
        f"PARENT plans (e.g. mobile/telecom rate plans like '5GX', unlimited plans) that "
        f"bundle '{anchor}' among their perks. Do NOT omit a plan just because '{anchor}' is "
        f"only one of several included benefits, and do NOT collapse distinct rate plans into "
        f"one. List each separately. " if anchor else ""
    )
    prompt = (
        "GROUND STRICTLY IN THE TEXT. Two rules, equally important:\n"
        "(1) RECALL — capture EVERYTHING that is in the PAGE TEXT: every plan, every "
        "service/benefit, and every price. If a service's standalone/regular price (e.g. "
        "'₩7,000', '월 9,900원', '$9.99/mo') appears ANYWHERE in the text, you MUST put "
        "that number in that service's list_price — NEVER leave list_price null when the "
        "text shows a price for it. Scan the whole text, not just the top.\n"
        "(2) NO INVENTION — use ONLY plans, services, and prices that literally appear in "
        "the text. Do NOT add a plan, service, or price that is not written there. If you "
        "are unsure whether something is in the text, leave it out.\n\n"
        "You extract BUNDLE or MEMBERSHIP plans from a provider's page. This includes "
        "carrier/aggregator bundles (mobile + streaming) AND membership programs that "
        "give multiple BENEFITS (e.g. Naver Membership, Coupang WOW, Amazon Prime: "
        "points/rewards, content credits, free shipping, a 'choose 1' streaming perk, "
        "cloud storage, webtoon cookies, etc.). Pages may be in Korean — read Korean. "
        + anchor_line +
        "If a membership/bundle has paid UPGRADE options (e.g. base ₩4,900, then "
        "'+₩6,500 for Netflix Standard', '+₩10,000 for Netflix Premium'), emit a SEPARATE "
        "plan for EACH resulting price point: the base plan AND one plan per upgrade tier "
        "(monthly = base price + upgrade cost; name it like '<base> + <upgrade>'). Do NOT "
        "hide upgrade tiers in price_note only — each distinct total price must be its own plan. "
        "For EACH plan return: name, provider (who sells it), currency (ISO code of the "
        "listed price, e.g. USD, KRW, JPY — infer ₩/원→KRW, $→USD), monthly (number in "
        "that currency or null), annual (number per month or null), choose (if you PICK "
        "some of the listed benefits, e.g. '택1'/'choose 1 of', set how many; else null), "
        "price_note (string or null), and services = EVERY listed benefit/service "
        "(혜택), each as {name, category, choice, list_price}. Capture ALL benefits, not "
        "only standalone subscriptions — include points/rewards, content, shopping perks, "
        "discounts, cloud, etc. choice=true if it is a selectable ALTERNATIVE (you pick "
        "among several), false if always included. IMPORTANT: many memberships let you "
        "pick ONE service among several (Korean memberships especially: e.g. 'Netflix / "
        "TVing / YouTube Premium 중 택1', '원하는 혜택 1개 선택', 'select/choose one of', "
        "'one of the following'). In that case set choose to how many you pick (usually 1) "
        "AND mark every option in that set choice=true — do NOT treat them as all-included "
        "(never sum their prices as if you get them all). list_price = that benefit's "
        "standalone/regular MONTHLY price IF stated ANYWHERE in the same currency as monthly. "
        "CAPTURE THIS AGGRESSIVELY for EVERY benefit, not just streaming subscriptions: any "
        "phrasing like 'Standalone Value: <amount>', 'standalone value if bought separately', "
        "'regular/normally <amount>', '<amount>/month' next to a perk all give its list_price. "
        "Strip thousands separators ('1,650' -> 1650). For a RANGE ('~3,000 to 5,000', "
        "'$5-$8') use the LOWER number. For non-numeric/percentage values ('Variable', 'Up to "
        "5% back') set list_price=null. Non-streaming perks that are always included (e.g. "
        "cloud storage, content/coupon packs, points/rewards, discounts) ARE services too — "
        "include each with its stated standalone value as list_price and choice=false. category is "
        "the SERVICE TYPE — short and concrete, e.g. 'Streaming Video', 'Music', "
        "'Mobile/Telecom', 'Cloud Storage', 'Gaming', 'News', 'Shopping', 'Points/Rewards', "
        "'Content', 'Delivery', 'Fitness'. Do NOT use vague labels like 'Additional "
        "subscription option', 'Optional', or 'Add-on' (whether a benefit is optional is "
        "expressed by choice=true, NOT by the category). "
        "Use ONLY info present in the text — do not invent. Keep prices in their ORIGINAL "
        "currency. Also return conditions = a SHORT note of any ELIGIBILITY REQUIREMENT or "
        "notable RESTRICTION needed to get/buy this bundle (string or null): e.g. 'requires "
        "an existing Xfinity Internet or Mobile plan', 'existing customers only', 'new "
        "customers only', 'autopay required', region/term limits, '기존 가입자 전용', "
        "'자사 인터넷/모바일 가입자 대상'. Set null if the text states no such restriction. "
        "If there are no plans, return [].\n"
        "Return ONLY JSON: {\"plans\":[{\"name\":...,\"provider\":...,\"currency\":...,"
        "\"monthly\":...,\"annual\":...,\"choose\":...,\"price_note\":...,\"conditions\":...,"
        "\"services\":"
        "[{\"name\":...,\"category\":...,\"choice\":false,\"list_price\":null}]}]}. No prose, no code fences.\n\n"
        f"PAGE TEXT:\n{text}\n"
    )
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8192,
        temperature=0,   # 같은 원문 → 같은 결과(재분석마다 plan 수가 달라지던 문제 완화)
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"번들 추출 JSON 파싱 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractError("번들 추출 응답이 객체(JSON object)가 아닙니다.")
    def _int(v):
        try:
            n = int(v)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    plans = []
    for p in data.get("plans", []) or []:
        services = []
        for s in (p.get("services") or []):
            nm = str(s.get("name") or "").strip()
            if nm:
                services.append({
                    "name": nm,
                    "category": str(s.get("category") or "기타").strip(),
                    "choice": bool(s.get("choice")),
                    "list_price": _num(s.get("list_price")),
                })
        if not services and not p.get("name"):
            continue
        cur = str(p.get("currency") or "USD").strip().upper() or "USD"
        plans.append({
            "name": str(p.get("name") or "Bundle"),
            "provider": str(p.get("provider") or company),
            "currency": cur,
            "monthly": _num(p.get("monthly")),
            "annual": _num(p.get("annual")),
            "choose": _int(p.get("choose")),
            "price_note": (str(p.get("price_note")).strip() if p.get("price_note") else None),
            "conditions": (str(p.get("conditions")).strip() if p.get("conditions") else None),
            "services": services,
        })

    # 안전장치: 원문에 '택1/중 하나/select one' 신호가 있으면, AI가 놓친 경우라도
    # 같은 카테고리의 복수 서비스를 택1로 보정(정가 합산 과대계상 방지).
    # (둘 다 포함하는 통신사 번들 등은 이런 문구가 없어 영향 없음.)
    low = (raw_text or "").lower()
    pick_signals = [
        "택1", "택 1", "중 택", "중 1개", "중 하나", "중에서 1", "1개 선택",
        "하나를 선택", "하나 선택", "원하는 1", "select one", "choose one",
        "choose 1", "pick one", "select any one", "choose any one",
    ]
    if any(sig in low for sig in pick_signals):
        for p in plans:
            by_cat: dict = {}
            for s in p["services"]:
                by_cat.setdefault(s["category"], []).append(s)
            changed = False
            for grp in by_cat.values():
                if len(grp) >= 2 and not any(s["choice"] for s in grp):
                    for s in grp:
                        s["choice"] = True
                    changed = True
            if changed and not p.get("choose"):
                p["choose"] = 1

    return {"anchor": anchor or "", "plans": plans}


def estimate_bundle_gaps_ai(
    company: str, anchor: str | None, plans: list[dict], market_kr: bool = False
) -> list[dict]:
    """번들 요금제의 모든 가격을 AI 지식으로 독립 추정(검색값과 비교·선택용).

    입력 plans: 저장된 분석의 요금제들
        [{name, currency, monthly, services:[{name, choice, list_price}]}]
    반환: plans 와 같은 순서의
        [{"name", "monthly": {v,conf,basis}|None,
          "services": [{"name", "list_price": {v,conf,basis}|None}]}]
    - 검색값이 이미 있어도 AI 값을 함께 뽑아, 사용자가 골라 적용할 수 있게 한다.
    - 모르면 지어내지 말고 v=null. 값마다 확신도(high/med/low)+한 줄 근거.
    - 개별 서비스 정가는 비교적 안정적, 번들 할인가는 변동 커서 보수적으로.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    # 모든 요금제·서비스를 대상으로 추정(검색값 유무와 무관 — 비교용 대안값 제공).
    gaps = []
    for p in plans:
        all_s = [s.get("name") for s in (p.get("services") or []) if (s.get("name") or "").strip()]
        gaps.append((p, True, all_s))
    if not gaps:
        return []

    from anthropic import Anthropic

    cur_hint = "KRW (Korean won, 원)" if market_kr else "the plan's own currency"
    spec = []
    for p, miss_m, miss_s in gaps:
        spec.append({
            "name": p.get("name"),
            "currency": (p.get("currency") or ("KRW" if market_kr else "USD")),
            "need_monthly": bool(miss_m),
            "need_services": miss_s,
        })
    anchor_line = f"These bundles are built around '{anchor}'. " if anchor else ""
    prompt = (
        "You INDEPENDENTLY estimate CURRENT retail prices from your own knowledge, so a user "
        "can COMPARE them with values found by live search and choose which to trust. "
        "Provider: " + company + ". "
        + anchor_line +
        "Estimate prices in " + cur_hint + ". Give the CURRENT typical retail price you know.\n"
        "STRICT RULES:\n"
        "1) Estimate EVERY field listed below (monthly and each service). Return the SAME plan names.\n"
        "2) If you do NOT confidently know a value, return v=null. NEVER invent a plausible "
        "number — a null is far better than a wrong price.\n"
        "3) For each value give conf = 'high' | 'med' | 'low' and basis = one short phrase "
        "(e.g. 'well-known Netflix Korea standard price', 'carrier plan price varies by promo').\n"
        "4) A service's standalone MONTHLY retail price is usually stable and safer to estimate. "
        "A carrier BUNDLE's discounted monthly price varies a lot — be conservative (lower conf).\n"
        "5) Numbers only, no currency symbols, no thousands separators.\n\n"
        "NEEDED (JSON):\n" + json.dumps(spec, ensure_ascii=False) + "\n\n"
        "Return ONLY JSON: {\"plans\":[{\"name\":...,\"monthly\":{\"v\":num|null,"
        "\"conf\":\"med\",\"basis\":\"...\"},\"services\":[{\"name\":...,\"list_price\":"
        "{\"v\":num|null,\"conf\":\"high\",\"basis\":\"...\"}}]}]}. Omit monthly if it was not "
        "needed. No prose, no code fences."
    )
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"AI 추정 JSON 파싱 실패: {exc}") from exc

    def _num2(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v) if v > 0 else None
        m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
        return float(m.group()) if m else None

    def _cell(d):
        if not isinstance(d, dict):
            return None
        v = _num2(d.get("v"))
        if v is None:
            return None
        conf = str(d.get("conf") or "").lower()
        conf = conf if conf in ("high", "med", "low") else "low"
        return {"v": v, "conf": conf, "basis": str(d.get("basis") or "").strip()[:120]}

    out = []
    for p in (data.get("plans") or []):
        svcs = []
        for s in (p.get("services") or []):
            nm = str(s.get("name") or "").strip()
            if nm:
                svcs.append({"name": nm, "list_price": _cell(s.get("list_price"))})
        out.append({
            "name": str(p.get("name") or "").strip(),
            "monthly": _cell(p.get("monthly")),
            "services": svcs,
        })
    return out


def describe_features_ai(items: list[dict], lang: str = "ko") -> dict:
    """기능마다 '이 기능은 무엇을 하는지' 한 줄 설명을 생성(업체 원문을 근거로).

    items: [{"key": 고유키, "name": 대표명, "samples": [업체 원문 기능 문구...]}]
    반환: {key: "한 줄 설명"}  — 근거가 부족하면 이름에서 일반적으로 기술.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    items = [it for it in (items or []) if (it.get("name") or "").strip()][:120]
    if not items:
        return {}

    from anthropic import Anthropic

    # 모델이 이상한 canon 키(예: 'translat', 'ALIAS::x')를 그대로 못 돌려줄 수 있어,
    # 정수 id 로 주고받고 아래에서 실제 키로 되돌린다(매핑 안전).
    spec = [
        {"id": i, "name": it["name"], "samples": (it.get("samples") or [])[:5]}
        for i, it in enumerate(items)
    ]
    lang_line = ("Write each description in Korean." if lang == "ko"
                 else "Write each description in English.")
    prompt = (
        "For each software/subscription FEATURE below, write ONE short plain-language "
        "sentence describing what it is / what it does, so a non-expert understands it. "
        + lang_line + " Ground it in the name and the sample descriptions from real "
        "provider pages; if samples are thin, describe generically from the name. Keep it "
        "concise (about 12 words), no marketing fluff, no price, no company names. "
        "If the name is too vague to describe, return an empty string for that id.\n\n"
        "FEATURES (JSON):\n" + json.dumps(spec, ensure_ascii=False) + "\n\n"
        "Return ONLY JSON keyed by the SAME numeric id: {\"desc\":{\"0\":\"<one sentence>\","
        "\"1\":\"...\"}}. No prose, no code fences."
    )
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8192,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"기능 설명 JSON 파싱 실패: {exc}") from exc
    # id → 실제 key 로 되돌린다.
    raw_desc = data.get("desc") or {}
    out = {}
    for i, it in enumerate(items):
        v = raw_desc.get(str(i))
        if v is None:
            v = raw_desc.get(i)   # 혹시 정수 키로 온 경우
        s = str(v or "").strip()
        if s:
            out[it["key"]] = s[:200]
    return out


def analyze_pricing_ai(company: str, groups: list[dict]) -> list[dict]:
    """가격대별 '처음 풀리는 기능'(결정적 증분)을 AI가 분석해 테마·요약을 붙인다.

    입력 groups: [{"price_label": "무료"|"$10/월"|..., "features": [...]}, ...]
    반환:        [{"price_label", "theme", "summary", "key_features": [...]}, ...]
    가격/증분 자체는 입력으로 고정(환각 방지)하고, AI 는 의미 해석만 더한다.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ExtractError("ANTHROPIC_API_KEY 가 설정되지 않았습니다 (.env 확인).")
    if not groups:
        return []

    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    payload = json.dumps({"company": company, "price_points": groups}, ensure_ascii=False)
    prompt = (
        "You are a SaaS pricing analyst. For ONE company, you are given the features "
        "that first unlock at each price point (already de-duplicated incrementally). "
        "For EACH price point, explain what that price buys.\n"
        "For each price point return:\n"
        "- price_label: copy the given label verbatim\n"
        "- theme: a short 2-4 word label for the value unlocked at this price\n"
        "- summary: ONE concise sentence — what you get / who it's for\n"
        "- key_features: the 2-6 most important features at this price (clean, "
        "deduplicated, human-readable; only from the given features)\n"
        "Use the SAME language as the features (Korean or English). Do not invent "
        "features or prices. Keep the SAME order and the SAME set of price points.\n"
        "Return ONLY JSON: {\"price_points\": [{price_label, theme, summary, "
        "key_features:[...]}, ...]}. No prose, no code fences.\n\n"
        f"INPUT:\n{payload}\n"
    )
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"가격 분석 JSON 파싱 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractError("가격 분석 응답이 객체(JSON object)가 아닙니다.")
    out = []
    for p in data.get("price_points", []):
        out.append({
            "price_label": str(p.get("price_label", "")),
            "theme": str(p.get("theme", "")),
            "summary": str(p.get("summary", "")),
            "key_features": [str(f) for f in (p.get("key_features") or [])],
        })
    return out


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
        page_text=_clean_page_text(page_text)[:MAX_PAGE_CHARS],
    )

    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )

    try:
        return _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"JSON 파싱 실패: {exc}\n원문 앞부분: {raw[:300]}") from exc
