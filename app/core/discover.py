"""스토어 자동 탐색 — App Store / Play Store 링크와 앱 아이콘을 찾는다.

- Apple: 무료 공식 iTunes Search API (인증 불필요). 앱 URL + 아이콘 제공.
- Google Play: 공식 검색 API 가 없어 Playwright 로 검색 페이지의 첫 앱 링크를 추출.

모두 best-effort — 실패하면 None 을 돌려준다(예외를 올리지 않는다).
core 계층이므로 web 프레임워크에 의존하지 않는다.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

from .fetch import USER_AGENT, normalize_us_url


def find_apple_app(company: str) -> Optional[dict]:
    """iTunes Search API 로 US App Store 앱을 찾는다.

    반환: {"url": 앱스토어 URL, "icon": 아이콘 URL, "name": 앱 이름} 또는 None.
    """
    q = urllib.parse.urlencode(
        {"term": company, "country": "us", "entity": "software", "limit": 1}
    )
    url = f"https://itunes.apple.com/search?{q}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    results = data.get("results") or []
    if not results:
        return None
    item = results[0]
    track_url = item.get("trackViewUrl")
    if not track_url:
        return None
    return {
        "url": track_url,
        "icon": item.get("artworkUrl100") or item.get("artworkUrl60"),
        "name": item.get("trackName"),
    }


def find_google_play_app(company: str) -> Optional[dict]:
    """Play Store 검색 페이지에서 첫 번째 앱 상세 링크를 추출한다.

    반환: {"url": Play Store URL} 또는 None. (봇 차단 시 None)
    """
    from playwright.sync_api import sync_playwright

    search = (
        "https://play.google.com/store/search?"
        + urllib.parse.urlencode({"q": company, "c": "apps", "gl": "us", "hl": "en"})
    )
    href = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                locale="en-US",
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = context.new_page()
            try:
                page.goto(search, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
                href = page.evaluate(
                    "() => { const a = document.querySelector("
                    "'a[href*=\"/store/apps/details?id=\"]'); "
                    "return a ? a.getAttribute('href') : null; }"
                )
            finally:
                context.close()
                browser.close()
    except Exception:  # noqa: BLE001
        return None

    if not href:
        return None
    full = ("https://play.google.com" + href) if href.startswith("/") else href
    # 패키지 식별자만 남기고 US/영어 파라미터를 붙인다
    return {"url": normalize_us_url("google_play", full)}
