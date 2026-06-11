"""Playwright(Chromium, headless) 페이지 수집 — US 로케일 강제 (8장).

JS 렌더링 후 본문 텍스트를 확보한다. 사이트별 전용 파서는 만들지 않는다.
봇 감지 대비: 현실적 User-Agent, 지수 백오프(최대 N회), 요청 간 지연.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from .. import config

# 현실적인 데스크톱 Chrome User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """페이지 수집 실패."""


def build_google_search_url(company: str) -> str:
    """업체명으로 US/영어 구글 검색 URL 을 만든다.

    가격뿐 아니라 유료 플랜·무료 체험 정보까지 스니펫에 잡히도록 검색어를 넓힌다.
    """
    from urllib.parse import quote_plus

    q = quote_plus(
        f"{company} pricing plans and what features are included "
        f"in each paid tier, plus free trial"
    )
    return f"https://www.google.com/search?q={q}&hl=en&gl=us"


def _serpapi_get(params: dict) -> dict:
    """SerpAPI search.json 호출 → JSON dict. (호출 1건 = 검색 1건 과금)"""
    import json
    import urllib.request

    if not config.SERPAPI_KEY:
        raise FetchError("SERPAPI_KEY 미설정")
    api = "https://serpapi.com/search.json?" + urlencode(
        {**params, "api_key": config.SERPAPI_KEY}
    )
    try:
        req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"SerpAPI 요청 실패: {exc}") from exc
    if data.get("error"):
        raise FetchError(f"SerpAPI 오류: {data['error']}")
    return data


def _flatten_ai_overview(ai: dict) -> str:
    """SerpAPI ai_overview(text_blocks/references)를 읽기 좋은 텍스트로 평탄화."""
    out: list[str] = []

    def walk(blocks, indent=""):
        for b in blocks or []:
            if b.get("snippet"):
                out.append(indent + b["snippet"])
            for item in b.get("list", []) or []:
                label = " ".join(
                    x for x in (item.get("title"), item.get("snippet")) if x
                )
                if label:
                    out.append(indent + "- " + label)
                walk(item.get("list"), indent + "  ")
            # 일부 블록은 text_blocks 를 중첩한다
            walk(b.get("text_blocks"), indent + "  ")

    walk(ai.get("text_blocks"))
    refs = [r.get("link") for r in (ai.get("references") or []) if r.get("link")]
    if refs:
        out.append("REFERENCES: " + "; ".join(refs[:10]))
    return "\n".join(o for o in out if o).strip()


def _ai_overview_text(ai: dict) -> str:
    """ai_overview 블록에서 본문 추출. page_token 만 있으면 2차 호출로 본문을 받는다.

    SerpAPI 는 AI Overview 를 인라인(text_blocks)으로 주거나, page_token 만 주고
    별도 engine=google_ai_overview 호출을 요구한다(이때 호출 1건이 추가 과금).
    """
    if not ai.get("text_blocks") and ai.get("page_token"):
        try:
            data = _serpapi_get(
                {"engine": "google_ai_overview", "page_token": ai["page_token"]}
            )
            ai = data.get("ai_overview", ai)
        except FetchError:
            return ""  # 2차 호출 실패는 치명적이지 않음(오가닉 스니펫으로 진행)
    return _flatten_ai_overview(ai)


def fetch_google_via_serpapi(url: str) -> str:
    """구글 검색을 SerpAPI로 가져온다(헤드리스 봇 차단 회피).

    URL 의 q 파라미터로 검색하고, answer_box / AI Overview 본문 / 상위 오가닉
    스니펫을 합쳐 텍스트로 돌려준다. config.SERPAPI_KEY 필요.
    AI Overview 가 page_token 형태면 2차 호출로 실제 본문을 받아온다.
    """
    import json

    q = parse_qs(urlparse(url).query).get("q", [""])[0]
    if not q:
        raise FetchError(f"검색어(q) 없음: {url}")

    data = _serpapi_get({"engine": "google", "q": q, "hl": "en", "gl": "us"})

    parts: list[str] = []
    if data.get("answer_box"):
        parts.append("ANSWER BOX:\n" + json.dumps(data["answer_box"], ensure_ascii=False))
    if data.get("ai_overview"):
        ov = _ai_overview_text(data["ai_overview"])
        if ov:
            parts.append("AI OVERVIEW:\n" + ov)
    for o in (data.get("organic_results") or [])[:10]:
        title = o.get("title", "")
        snippet = o.get("snippet", "")
        if title or snippet:
            parts.append(f"{title}\n{snippet}")

    text = "\n\n".join(parts).strip()
    if not text:
        raise FetchError("SerpAPI 결과가 비어 있음")
    return text


def normalize_us_url(source_type: str, url: str) -> str:
    """소스 URL 을 US/USD 로케일로 보정 (8장).

    - apple: apps.apple.com 경로에 국가코드가 없으면 '/us/' 삽입.
    - google_play / google_search: 쿼리에 gl=us, hl=en 강제.
    - web/other: 그대로 (Playwright 로케일이 1차 방어선).
    실패 시 원본 URL 을 그대로 돌려준다(안전).
    """
    try:
        p = urlparse(url)
        host = p.netloc.lower()

        if source_type == "apple" and "apple.com" in host:
            parts = p.path.split("/")
            # /app/...  또는  /us/app/...  형태. 'app' 앞에 국가코드가 없으면 us 삽입.
            if len(parts) > 1 and parts[1] == "app":
                parts.insert(1, "us")
                p = p._replace(path="/".join(parts))
            return urlunparse(p)

        if source_type in ("google_play", "google_search") and "google.com" in host:
            q = dict(parse_qsl(p.query))
            q.setdefault("gl", "us")
            q.setdefault("hl", "en")
            p = p._replace(query=urlencode(q))
            return urlunparse(p)
    except Exception:  # noqa: BLE001
        return url
    return url


def fetch_page_text(url: str) -> str:
    """주어진 URL 을 US 로케일로 렌더링하고 본문 텍스트를 돌려준다.

    지수 백오프로 최대 config.FETCH_MAX_RETRIES 회 재시도한다.
    실패 시 FetchError 를 던진다.
    """
    # 무거운 import 는 함수 안에서 — core import 만으로 Playwright 를 강제하지 않는다.
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    last_err: Exception | None = None

    for attempt in range(1, config.FETCH_MAX_RETRIES + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    locale=config.LOCALE,
                    timezone_id=config.TIMEZONE_ID,
                    user_agent=USER_AGENT,
                    extra_http_headers={"Accept-Language": config.ACCEPT_LANGUAGE},
                )
                page = context.new_page()
                try:
                    # domcontentloaded 로 기본 로드를 끝낸다.
                    # (networkidle 은 분석/폴링 스크립트가 계속 도는 사이트·구글 검색에서
                    #  영영 끝나지 않아 매번 풀타임아웃을 소모하므로 사용하지 않는다.)
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=config.FETCH_TIMEOUT_MS,
                    )
                    # 동적 가격 위젯이 채워질 시간을 best-effort 로만 기다린다
                    # (networkidle 에 도달 못 해도 예외 없이 넘어간다).
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except PWTimeout:
                        pass
                    page.wait_for_timeout(2000)
                    text = page.evaluate("() => document.body.innerText")
                finally:
                    context.close()
                    browser.close()

            text = (text or "").strip()
            if not text:
                raise FetchError(f"빈 본문 텍스트: {url}")
            return text

        except (PWTimeout, FetchError, Exception) as exc:  # noqa: BLE001
            last_err = exc
            if attempt < config.FETCH_MAX_RETRIES:
                backoff = 2 ** attempt  # 2s, 4s, 8s ...
                time.sleep(backoff)

    raise FetchError(f"{url} 수집 실패 ({config.FETCH_MAX_RETRIES}회 시도): {last_err}")
