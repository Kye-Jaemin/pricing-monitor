"""Playwright(Chromium, headless) 페이지 수집 — US 로케일 강제 (8장).

JS 렌더링 후 본문 텍스트를 확보한다. 사이트별 전용 파서는 만들지 않는다.
봇 감지 대비: 현실적 User-Agent, 지수 백오프(최대 N회), 요청 간 지연.
"""
from __future__ import annotations

import time

from .. import config

# 현실적인 데스크톱 Chrome User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """페이지 수집 실패."""


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
                    page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=config.FETCH_TIMEOUT_MS,
                    )
                    # 동적 가격 위젯이 채워질 여유
                    page.wait_for_timeout(1500)
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
