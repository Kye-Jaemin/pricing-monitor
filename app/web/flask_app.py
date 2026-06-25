"""❹ Flask 어댑터 — 현재+향후 공통 (권장). 로직 없음, core 호출 + 렌더링만.

기동:
  개발  flask --app app.web.flask_app run --port 8000
  운영  waitress-serve --port=8000 app.web.flask_app:app
        gunicorn app.web.flask_app:app --bind 0.0.0.0:$PORT   (Render)
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import config
from ..core import discover, presenters, store
from ..core.fetch import build_google_search_url
from ..core.models import SOURCE_TYPE_LABELS, SOURCE_TYPES
from ..core.pipeline import run_once
from .. import scheduler as sched
from ..scheduler import start_scheduler
from .i18n import DEFAULT_LANG, LANGUAGES, translate

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pricing.web")

# templates/ 와 static/ 은 프로젝트 루트에 위치 (Flask/FastAPI 공용)
_ROOT = Path(__file__).resolve().parents[2]

app = Flask(
    __name__,
    template_folder=str(_ROOT / "templates"),
    static_folder=str(_ROOT / "static"),
)

# DB 준비 + (internal 모드면) 스케줄러 기동
store.init_db()
start_scheduler()

# [지금 수집] 중복 실행 방지용 락 + 진행 상황
_run_lock = threading.Lock()
_run_in_progress = {"value": False}
# 진행 중 선택된 source_ids (선택 수집일 때만 set, 전체 수집/비실행 시 None)
# → 진행 중에는 화면이 사용자의 실제 선택을 유지하고, 완료 후 전체 선택으로 복귀.
_running_ids = {"value": None}
_progress = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "ok": 0,
    "error": 0,
}

# 번들 AI 분석(추출) 진행 상태 — 수집과 별개 트랙.
_bundle_lock = threading.Lock()
_bundle_progress = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "n": 0,          # 추출 시도 업체 수(완료 시)
    "names": [],     # 분석 대상(완료 후 결과 필터·리다이렉트용)
    "error": "",
}


# ── 다국어(한/영) ────────────────────────────────────────────
def _current_lang() -> str:
    lang = request.cookies.get("lang", DEFAULT_LANG)
    return lang if lang in LANGUAGES else DEFAULT_LANG


@app.context_processor
def inject_i18n():
    lang = _current_lang()

    def t(key: str, **kwargs) -> str:
        return translate(key, lang, **kwargs)

    return {"t": t, "lang": lang, "languages": LANGUAGES}


@app.route("/lang/<code>")
def set_lang(code: str):
    target = request.referrer or url_for("index")
    resp = make_response(redirect(target))
    if code in LANGUAGES:
        resp.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


# ── 화면 ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", data=presenters.overview())


@app.route("/company/<name>")
def company(name: str):
    detail = presenters.company_detail(name)
    if detail is None:
        return render_template("not_found.html", name=name), 404
    return render_template("company.html", data=detail)


@app.route("/changes")
def changes():
    company_filter = request.args.get("company") or None
    category_filter = request.args.get("category") or None
    return render_template(
        "changes.html",
        data=presenters.changes_view(company_filter, category_filter),
    )


@app.route("/bundle")
def bundle_page():
    """가격 분석(번들): 번들 상품 업체를 분류별로 묶어 분석(선택 업체만)."""
    names = [n for n in request.args.getlist("company") if n]
    return render_template(
        "bundle.html",
        data=presenters.bundle_view(names or None),
        saved_cards=presenters.saved_bundle_cards(),
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
        bundle_running=_bundle_progress["running"] or request.args.get("analyzing") == "1",
    )


@app.route("/bundle/save", methods=["POST"])
def bundle_save():
    """현재(선택) 번들 분석을 저장 시점 그대로 카드로 저장."""
    title = (request.form.get("title") or "").strip()
    names = [n for n in request.form.getlist("company") if n]
    card_id = presenters.save_bundle_card(title, names or None)
    if card_id is None:
        return redirect(url_for("bundle_page", error="no_data"))
    return redirect(url_for("bundle_page", notice="saved"))


@app.route("/bundle/card/<int:card_id>")
def bundle_card(card_id: int):
    """저장된 번들 분석 카드를 고정 스냅샷 그대로 표시."""
    card = presenters.load_bundle_card(card_id)
    if card is None:
        return redirect(url_for("bundle_page"))
    return render_template(
        "bundle.html",
        data=card["data"],
        saved_cards=presenters.saved_bundle_cards(),
        saved_view=True,
        card=card,
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@app.route("/bundle/card/<int:card_id>/delete", methods=["POST"])
def bundle_card_delete(card_id: int):
    store.delete_bundle_card(card_id)
    return redirect(url_for("bundle_page"))


@app.route("/bundle/cards/delete", methods=["POST"])
def bundle_cards_delete():
    """저장된 번들 분석 카드 여러 개를 한 번에 삭제."""
    ids = [int(x) for x in request.form.getlist("card_ids") if x.isdigit()]
    for cid in ids:
        store.delete_bundle_card(cid)
    return redirect(url_for("bundle_page"))


@app.route("/bundle/card/<int:card_id>/rename", methods=["POST"])
def bundle_card_rename(card_id: int):
    title = (request.form.get("title") or "").strip()
    if title:
        store.rename_bundle_card(card_id, title)
    return redirect(request.referrer or url_for("bundle_page"))


def _bundle_url(names, **extra):
    from urllib.parse import urlencode

    q = [("company", n) for n in names] + [(k, v) for k, v in extra.items() if v]
    return url_for("bundle_page") + (("?" + urlencode(q)) if q else "")


@app.route("/bundle/run", methods=["POST"])
def bundle_run():
    """선택 업체 분석 — 'AI 구조 분석' 체크 시 추출을 함께 실행하고, 결과를
    선택 업체로 필터해 보여준다(선택+분석 한 버튼)."""
    names = [n for n in request.form.getlist("company") if n]
    if request.form.get("ai_analyze"):
        if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
            return redirect(_bundle_url(names, error="bad_code"))
        # 백그라운드로 추출 실행 → 즉시 번들 페이지로 복귀(진행바가 폴링하며 표시).
        if not _bundle_progress["running"]:
            with _bundle_lock:
                if not _bundle_progress["running"]:
                    _bundle_progress.update(
                        {"running": True, "total": 0, "done": 0,
                         "current": "시작 중…", "n": 0, "names": names, "error": ""}
                    )
                    threading.Thread(
                        target=_background_bundle_run,
                        kwargs={"names": names or None},
                        daemon=True,
                    ).start()
        return redirect(_bundle_url(names, analyzing="1"))
    return redirect(_bundle_url(names))


def _bundle_progress_cb(done: int, total: int, current: str) -> None:
    _bundle_progress.update({"done": done, "total": total, "current": current})


def _background_bundle_run(names=None) -> None:
    try:
        log.info("[bundle-run] AI 번들 추출 시작 (names=%s)", names)
        n = presenters.run_bundle_extraction(names, progress_cb=_bundle_progress_cb)
        _bundle_progress.update({"n": n})
        log.info("[bundle-run] 완료: %d곳 분석", n)
    except Exception:  # noqa: BLE001
        log.exception("[bundle-run] 실패")
        _bundle_progress.update({"error": "ai_failed"})
    finally:
        _bundle_progress.update({"running": False, "current": ""})


@app.route("/bundle-progress")
def bundle_progress():
    return jsonify(_bundle_progress)


@app.route("/diag/bundle-price")
def diag_bundle_price():
    """번들 구성요소 정가 매칭 진단(브라우저에서 바로 확인). 텍스트로 출력.
    예: /diag/bundle-price?q=mybox  (ACCESS_CODE 설정 시 &code=... 필요)"""
    if config.ACCESS_CODE and (request.args.get("code") or "") != config.ACCESS_CODE:
        return ("코드가 필요합니다: /diag/bundle-price?q=mybox&code=액세스코드", 403,
                {"Content-Type": "text/plain; charset=utf-8"})
    q = request.args.get("q") or "mybox"
    report = presenters.diag_bundle_price(q)
    return (report, 200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/bundle/svcprice", methods=["POST"])
def bundle_svcprice():
    """번들 포함 서비스의 정가를 사용자가 직접 입력/수정(번들 통화 기준).
    멤버십 페이지엔 없고 별도 검색에만 있는 정가를 직접 채울 때 사용. 빈 값=해제.
    재분석 없이 즉시 반영."""
    company = (request.form.get("company") or "").strip()
    key = (request.form.get("key") or "").strip()
    value = (request.form.get("value") or "").strip()
    if company and key:
        store.set_setting("bundle.svc:" + company + ":" + key, value)
    names = [n for n in request.form.getlist("sel") if n]
    return redirect(_bundle_url(names))


@app.route("/bundle/pickone", methods=["POST"])
def bundle_pickone():
    """번들을 '택1 멤버십'으로 지정/해제. 기본은 포함 서비스를 전부 합산하지만,
    Naver처럼 같은 종류 중 하나만 고르는 멤버십은 켜서 같은 종류는 하나만 센다.
    재분석 없이 즉시 반영."""
    company = (request.form.get("company") or "").strip()
    if company:
        val = "1" if request.form.get("pickone") else "0"
        store.set_setting("bundle.pickone:" + company, val)
    names = [n for n in request.form.getlist("sel") if n]
    return redirect(_bundle_url(names))


@app.route("/howto")
def howto():
    return render_template("howto.html")


def _compare_url(names, **extra):
    from urllib.parse import urlencode

    q = [("company", n) for n in names]
    q += [(k, v) for k, v in extra.items()]
    return url_for("compare_page") + (("?" + urlencode(q)) if q else "")


@app.route("/compare")
def compare_page():
    names = [n for n in request.args.getlist("company") if n]
    return render_template(
        "compare.html",
        data=presenters.compare(names),
        saved_cards=presenters.saved_comparison_cards(),
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@app.route("/compare/save", methods=["POST"])
def compare_save():
    """현재 비교 결과를 저장 시점 그대로 카드로 저장(수동 저장)."""
    names = [n for n in request.form.getlist("company") if n]
    title = (request.form.get("title") or "").strip()
    card_id = presenters.save_comparison(names, title)
    if card_id is None:
        return redirect(_compare_url(names, error="no_features"))
    return redirect(_compare_url(names, notice="saved"))


@app.route("/compare/card/<int:card_id>")
def compare_card(card_id: int):
    """저장된 비교 카드를 고정 스냅샷 그대로 표시(재계산 없음)."""
    card = presenters.load_comparison_card(card_id)
    if card is None:
        return redirect(url_for("compare_page"))
    return render_template(
        "compare.html",
        data=card["data"],
        saved_cards=presenters.saved_comparison_cards(),
        saved_view=True,
        card=card,
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@app.route("/compare/card/<int:card_id>/delete", methods=["POST"])
def compare_card_delete(card_id: int):
    store.delete_comparison_card(card_id)
    return redirect(url_for("compare_page"))


@app.route("/compare/card/<int:card_id>/rename", methods=["POST"])
def compare_card_rename(card_id: int):
    """저장된 비교 카드의 이름(제목) 변경."""
    title = (request.form.get("title") or "").strip()
    if title:
        store.rename_comparison_card(card_id, title)
    return redirect(request.referrer or url_for("compare_page"))


def _apply_categorize(names: list[str]) -> None:
    """선택 업체 전체 기능을 AI로 카테고리 분류해 저장(사용자 지정은 보존)."""
    feats = presenters.distinct_features(names)
    if not feats:
        return
    from ..core import extract

    mapping = extract.categorize_features_ai(feats)
    existing = store.get_feature_category_rows()
    for feature, category in mapping.items():
        if existing.get(feature, (None, "ai"))[1] == "user":
            continue
        store.set_feature_category(feature, category, source="ai")


def _apply_dedupe(names: list[str]) -> None:
    """선택 업체 전체 기능(무료 포함)을 AI로 유사 통합해 별칭으로 저장."""
    feats = presenters.distinct_features(names)
    if not feats:
        return
    from ..core import extract

    mapping = extract.dedupe_features_ai(feats)
    store.set_feature_aliases(mapping)


def _apply_pricing(names: list[str]) -> None:
    """선택 업체의 가격대별 증분을 AI가 분석(테마·요약)해 저장."""
    from ..core import extract

    for name in names:
        groups, sig = presenters.unlock_groups_for(name)
        if not groups:
            continue
        result = extract.analyze_pricing_ai(
            name, [{"price_label": g["price_label"], "features": g["features"]}
                   for g in groups]
        )
        store.set_pricing_analysis(name, json.dumps(result, ensure_ascii=False), sig)


@app.route("/compare/run", methods=["POST"])
def compare_run():
    """비교하기 — 선택 업체 + 체크한 AI 분석(카테고리/가격)을 한 번에 실행."""
    names = [n for n in request.form.getlist("company") if n]
    do_cat = bool(request.form.get("ai_categorize"))
    do_price = bool(request.form.get("ai_pricing"))
    do_dedupe = bool(request.form.get("ai_dedupe"))
    if do_cat or do_price or do_dedupe:
        if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
            return redirect(_compare_url(names, error="bad_code"))
        try:
            if do_dedupe:
                _apply_dedupe(names)
            if do_cat:
                _apply_categorize(names)
            if do_price:
                _apply_pricing(names)
        except Exception:  # noqa: BLE001
            log.exception("[compare] AI 분석 실패")
            return redirect(_compare_url(names, error="ai_failed"))
    return redirect(_compare_url(names))


@app.route("/compare/categorize", methods=["POST"])
def compare_categorize():
    """(개별) 선택 업체 기능을 AI로 카테고리 분류. AI 작업 → 코드 필요."""
    names = [n for n in request.form.getlist("company") if n]
    if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
        return redirect(_compare_url(names, error="bad_code"))
    try:
        _apply_categorize(names)
    except Exception:  # noqa: BLE001
        log.exception("[compare] AI 카테고리 분류 실패")
        return redirect(_compare_url(names, error="ai_failed"))
    return redirect(_compare_url(names))


@app.route("/compare/analyze-pricing", methods=["POST"])
def compare_analyze_pricing():
    """(개별) 선택 업체의 가격대별 증분을 AI가 분석. AI 작업 → 코드 필요."""
    names = [n for n in request.form.getlist("company") if n]
    if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
        return redirect(_compare_url(names, error="bad_code"))
    try:
        _apply_pricing(names)
    except Exception:  # noqa: BLE001
        log.exception("[compare] AI 가격 분석 실패")
        return redirect(_compare_url(names, error="ai_failed"))
    return redirect(_compare_url(names))


@app.route("/compare/set-category", methods=["POST"])
def compare_set_category():
    """기능의 카테고리·통합(별칭)을 사용자가 직접 지정/수정.

    같은 통합 대표명을 입력한 기능들은 분류에서 하나로 묶인다(빈 값이면 통합 해제).
    """
    names = [n for n in request.form.getlist("company") if n]
    feature = (request.form.get("feature") or "").strip()
    category = (request.form.get("category") or "").strip()
    if feature and category:
        store.set_feature_category(feature, category, source="user")
    if feature:
        alias = (request.form.get("alias") or "").strip()
        if alias:
            store.set_feature_aliases({feature: alias})
        else:
            store.delete_feature_alias(feature)
    return redirect(_compare_url(names))


@app.route("/compare/set-threshold", methods=["POST"])
def compare_set_threshold():
    """커머디티/차별화 '저렴' 기준 가격($)을 조정(전역 설정, AI/외부호출 아님)."""
    names = [n for n in request.form.getlist("company") if n]
    try:
        value = float((request.form.get("cheap_usd") or "").strip())
        if value >= 0:
            presenters.set_cheap_threshold(value)
    except (TypeError, ValueError):
        pass
    return redirect(_compare_url(names))


@app.route("/compare/set-band", methods=["POST"])
def compare_set_band():
    """가격대별/기능별 분석의 가격 묶음 단위($)를 조정(전역 설정, AI/외부호출 아님)."""
    names = [n for n in request.form.getlist("company") if n]
    try:
        value = float((request.form.get("band_usd") or "").strip())
        if value >= 1:
            presenters.set_band_width(value)
    except (TypeError, ValueError):
        pass
    return redirect(_compare_url(names))


@app.route("/compare/find-feature", methods=["POST"])
def compare_find_feature():
    """입력한 기능과 유사한 기능들을 AI가 골라 카테고리별로 묶어 반환(JSON).

    정확히 일치하는 기능이 없을 때 클라이언트가 호출. 분석된 기능 집합 안에서만
    고르므로 각 기능의 보급률·해금가·분류 지표를 그대로 붙여 돌려준다.
    """
    names = [n for n in request.form.getlist("company") if n]
    query = (request.form.get("q") or "").strip()
    if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
        return jsonify({"error": "bad_code"})
    if not query or not names:
        return jsonify({"groups": []})

    from ..core import extract

    data = presenters.compare(names)
    pos = data.get("feature_positioning", [])
    by_name = {p["feature"]: p for p in pos}
    try:
        result = extract.find_similar_features_ai(query, list(by_name.keys()))
    except Exception:  # noqa: BLE001
        log.exception("[compare] AI 유사 기능 검색 실패")
        return jsonify({"error": "ai_failed"})

    groups = []
    for g in result.get("groups", []):
        feats = [by_name[f] for f in g.get("features", []) if f in by_name]
        if feats:
            groups.append({"category": g.get("category") or "기타", "features": feats})
    return jsonify({"groups": groups})


@app.route("/companies")
def companies_page():
    return render_template(
        "companies.html",
        data=presenters.companies_admin(),
        source_types=SOURCE_TYPE_LABELS,
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@app.route("/companies/discover", methods=["POST"])
def companies_discover():
    """업체명으로 App Store / Play Store 링크를 자동 탐색해 소스로 추가."""
    name = (request.form.get("company") or "").strip()
    if not name:
        return redirect(url_for("companies_page"))

    found = []
    apple = discover.find_apple_app(name)
    if apple and apple.get("url"):
        store.add_source(company=name, source_type="apple", url=apple["url"])
        found.append("App Store")
        if apple.get("icon"):
            store.set_company_icon(name, apple["icon"])

    play = discover.find_google_play_app(name)
    if play and play.get("url"):
        store.add_source(company=name, source_type="google_play", url=play["url"])
        found.append("Play Store")

    if found:
        notice = f"{name}: " + " / ".join(found) + " 자동 추가됨"
    else:
        notice = f"{name}: 스토어에서 앱을 찾지 못했습니다."
    return redirect(url_for("companies_page", notice=notice))


def _norm_type(value: str) -> str:
    value = (value or "web").strip().lower()
    return value if value in SOURCE_TYPES else "other"


def _company_bundle_anchor(name: str) -> tuple[bool, str]:
    """업체가 번들이면 (True, 분류명에서 뽑은 앵커), 아니면 (False, '')."""
    from ..core.presenters import _bundle_anchor

    row = next((c for c in store.list_companies(active_only=False) if c["name"] == name), None)
    if not row or not row["is_bundle"]:
        return False, ""
    if row["category_id"]:
        cats = {c["id"]: c["name"] for c in store.list_company_categories()}
        return True, (_bundle_anchor(cats.get(row["category_id"])) or "")
    return True, ""


def _resolve_source_url(company: str, source_type: str, url: str,
                        is_bundle: bool = False, anchor: str = ""):
    """소스 URL 을 결정한다. 비어 있으면 종류별로 자동 생성/탐색.

    반환: (url, icon, error). url 이 None 이면 추가 불가(error 사유 포함).
      - google_search: 업체명으로 검색 URL 자동 생성(번들이면 번들 전용 검색어)
      - apple / google_play: 스토어에서 앱 자동 탐색(자동 찾기와 동일)
      - web / other: URL 필수
    """
    url = (url or "").strip()
    if url:
        return url, None, None
    if source_type == "google_search":
        if is_bundle:
            from ..core.fetch import build_bundle_search_url
            return build_bundle_search_url(company, anchor or None), None, None
        return build_google_search_url(company), None, None
    if source_type == "apple":
        found = discover.find_apple_app(company)
        if found and found.get("url"):
            return found["url"], found.get("icon"), None
        return None, None, f"{company}: App Store에서 앱을 찾지 못했습니다."
    if source_type == "google_play":
        found = discover.find_google_play_app(company)
        if found and found.get("url"):
            return found["url"], None, None
        return None, None, f"{company}: Play Store에서 앱을 찾지 못했습니다."
    return None, None, "소스 URL은 필수입니다(공식 홈페이지/기타)."


@app.route("/companies/add", methods=["POST"])
def companies_add():
    """업체 + 첫 소스를 함께 등록.

    번들 상품이면: 주체=업체명, 대상(앵커)=anchor. 소스는 웹/구글 검색만(스토어
    제외), 구글 검색어는 번들 전용. 앵커가 있으면 분류 '번들-{앵커}'에 자동 배정.
    """
    name = (request.form.get("name") or "").strip()
    source_type = _norm_type(request.form.get("source_type"))
    is_bundle = bool(request.form.get("is_bundle"))
    anchor = (request.form.get("anchor") or "").strip()
    if not name:
        return redirect(url_for("companies_page", error="업체명은 필수입니다."))

    # 번들은 구글 검색만 지원(제공업체+결합업체로 자동 검색) — 소스/URL 입력 불필요
    if is_bundle:
        source_type = "google_search"

    # 앵커 칸이 비었지만 '번들-X' 분류를 골랐다면 거기서 앵커를 보강(검색어에 포함)
    if is_bundle and not anchor:
        cid_sel = (request.form.get("category_id") or "").strip()
        if cid_sel.isdigit():
            from ..core.presenters import _bundle_anchor
            cname = next((c["name"] for c in store.list_company_categories()
                          if c["id"] == int(cid_sel)), None)
            anchor = _bundle_anchor(cname) or ""

    # 번들은 URL 입력 없이 항상 '제공업체 결합업체' 자동 검색 소스를 생성
    src_url = "" if is_bundle else request.form.get("url")
    url, icon, error = _resolve_source_url(
        name, source_type, src_url, is_bundle=is_bundle, anchor=anchor
    )
    if error:
        return redirect(url_for("companies_page", error=error))

    store.add_source(company=name, source_type=source_type, url=url)
    if icon:
        store.set_company_icon(name, icon)
    if is_bundle:
        store.set_company_bundle(name, True)
        if anchor:  # 분류 '번들-{앵커}' 자동 생성·배정
            cat_name = f"번들-{anchor}"
            store.add_company_category(cat_name)
            cat_id = next((c["id"] for c in store.list_company_categories()
                           if c["name"] == cat_name), None)
            if cat_id:
                store.set_company_category(name, cat_id)
            # 앵커(대상 서비스)는 원가 수집용 구성요소로 자동 등록(같은 번들 분류).
            # 나머지 포함 서비스는 번들 분석(추출) 시 자동 등록된다.
            a = anchor.strip()
            if a and a.lower() != name.lower() and not any(
                c["name"].lower() == a.lower() for c in store.list_companies(active_only=False)
            ):
                store.add_company(a)
                store.set_company_component(a, True)
                if cat_id:
                    store.set_company_category(a, cat_id)
                store.add_source(company=a, source_type="google_search",
                                 url=build_google_search_url(a))
            return redirect(url_for("companies_page"))
    cid = (request.form.get("category_id") or "").strip()
    if cid.isdigit():
        store.set_company_category(name, int(cid))
    return redirect(url_for("companies_page"))


@app.route("/companies/set-bundle", methods=["POST"])
def companies_set_bundle():
    """업체의 번들 상품 여부 토글."""
    name = (request.form.get("name") or "").strip()
    if name:
        store.set_company_bundle(name, bool(request.form.get("is_bundle")))
    return redirect(request.referrer or url_for("companies_page"))


@app.route("/companies/set-component", methods=["POST"])
def companies_set_component():
    """업체의 번들 구성요소(원가 수집용) 여부 토글. 일반 비교 목록에서 숨겨진다."""
    name = (request.form.get("name") or "").strip()
    if name:
        store.set_company_component(name, bool(request.form.get("is_component")))
    return redirect(request.referrer or url_for("companies_page"))


@app.route("/companies/delete", methods=["POST"])
def companies_delete():
    name = (request.form.get("name") or "").strip()
    if name:
        store.delete_company(name)
    return redirect(url_for("companies_page"))


# ── 업체 분류(업종/도메인) ───────────────────────────────────
@app.route("/categories/add", methods=["POST"])
def categories_add():
    """업체 분류 생성(예: Health, AI, 이미지/영상)."""
    name = (request.form.get("name") or "").strip()
    if name:
        store.add_company_category(name)
    return redirect(url_for("companies_page"))


@app.route("/categories/delete", methods=["POST"])
def categories_delete():
    """분류 삭제 — 소속 업체는 미분류로 되돌린다."""
    cid = request.form.get("category_id")
    if cid and cid.isdigit():
        store.delete_company_category(int(cid))
    return redirect(url_for("companies_page"))


@app.route("/companies/dedup-components", methods=["POST"])
def companies_dedup_components():
    """이름 표기만 다른 중복 구성요소를 정리(자동 등록 변형 중복 제거)."""
    n = presenters.dedup_components()
    dest = "runs" if "runs" in (request.referrer or "") else "companies_page"
    return redirect(url_for(dest, notice=f"중복 구성요소 {n}개를 정리했습니다."))


@app.route("/companies/dedup-components-ai", methods=["POST"])
def companies_dedup_components_ai():
    """AI 군집으로 한↔영 등 표기가 다른 중복 구성요소까지 정리. 코드 필요."""
    dest = "runs" if "runs" in (request.referrer or "") else "companies_page"
    if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
        return redirect(url_for(dest, error="액세스 코드가 올바르지 않습니다."))
    try:
        n = presenters.dedup_components_ai()
    except Exception:  # noqa: BLE001
        log.exception("[companies] AI 중복 정리 실패")
        return redirect(url_for(dest, error="AI 정리에 실패했습니다. 잠시 후 다시 시도하세요."))
    return redirect(url_for(dest, notice=f"AI 중복 구성요소 {n}개를 정리했습니다."))


@app.route("/companies/clear-components", methods=["POST"])
def companies_clear_components():
    """구성요소(🧩)로 등록된 업체를 모두 삭제(자동 생성된 쓰레기 정리)."""
    n = presenters.clear_components()
    dest = "runs" if "runs" in (request.referrer or "") else "companies_page"
    return redirect(url_for(dest, notice=f"구성요소 {n}개를 삭제했습니다."))


@app.route("/companies/delete-by-category", methods=["POST"])
def companies_delete_by_category():
    """해당 분류에 속한 업체를 전체 삭제."""
    cid = request.form.get("category_id")
    if cid and cid.isdigit():
        store.delete_companies_in_category(int(cid))
    return redirect(url_for("companies_page"))


@app.route("/companies/set-category", methods=["POST"])
def companies_set_category():
    """업체를 분류에 배정(빈 값이면 미분류)."""
    name = (request.form.get("name") or "").strip()
    cid = (request.form.get("category_id") or "").strip()
    if name:
        store.set_company_category(name, int(cid) if cid.isdigit() else None)
    return redirect(request.referrer or url_for("companies_page"))


@app.route("/sources/add", methods=["POST"])
def sources_add():
    """기존 업체에 소스 추가. 스토어 종류는 URL 없이 자동 탐색."""
    company = (request.form.get("company") or "").strip()
    source_type = _norm_type(request.form.get("source_type"))
    if not company:
        return redirect(url_for("companies_page", error="업체가 필요합니다."))

    # 번들 업체면 스토어 소스 제외(web 보정) + 구글 검색어에 앵커 포함
    is_bundle, anchor = _company_bundle_anchor(company)
    if is_bundle and source_type in ("apple", "google_play"):
        source_type = "web"

    url, icon, error = _resolve_source_url(
        company, source_type, request.form.get("url"), is_bundle=is_bundle, anchor=anchor
    )
    if error:
        return redirect(url_for("companies_page", error=error))

    store.add_source(company=company, source_type=source_type, url=url)
    if icon:
        store.set_company_icon(company, icon)
    return redirect(url_for("companies_page"))


@app.route("/sources/delete", methods=["POST"])
def sources_delete():
    sid = request.form.get("source_id")
    if sid and sid.isdigit():
        store.delete_source(int(sid))
    return redirect(url_for("companies_page"))


@app.route("/source/delete", methods=["POST"])
def source_delete():
    """현황 등에서 업체의 특정 출처(소스 타입)와 데이터를 삭제."""
    company = (request.form.get("company") or "").strip()
    stype = (request.form.get("source_type") or "").strip()
    if company and stype:
        store.delete_source_data(company, stype)
    return redirect(request.referrer or url_for("index"))


@app.route("/runs")
def runs():
    running_ids = _running_ids["value"]
    targets = presenters.collection_targets()
    return render_template(
        "runs.html",
        data=presenters.runs_view(),
        companies=targets["companies"],
        target_groups=targets["groups"],
        categories=targets["categories"],
        category_chips=targets["category_chips"],
        running=_run_in_progress["value"],
        running_ids=list(running_ids) if running_ids is not None else None,
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        scheduler=sched.get_status(),
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@app.route("/scheduler/save", methods=["POST"])
def scheduler_save():
    """스케줄러 온/오프·주기·stale 일수 설정 저장 + 즉시 재구성."""
    f = request.form
    category_ids = [int(x) for x in f.getlist("category_ids") if x.isdigit()]
    sched.save_settings(
        enabled=bool(f.get("enabled")),
        day_of_week=(f.get("day_of_week") or config.SCHEDULE_DAY_OF_WEEK).strip(),
        hour=max(0, min(23, _safe_int(f.get("hour"), config.SCHEDULE_HOUR))),
        minute=max(0, min(59, _safe_int(f.get("minute"), config.SCHEDULE_MINUTE))),
        timezone=(f.get("timezone") or config.SCHEDULE_TIMEZONE).strip(),
        stale_days=max(0, _safe_int(f.get("stale_days"), config.SCHEDULE_STALE_DAYS)),
        category_ids=category_ids,
    )
    return redirect(url_for("runs"))


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── 액션 ─────────────────────────────────────────────────────
@app.route("/runs/delete", methods=["POST"])
def runs_delete():
    """수집 실행 기록 1건 삭제."""
    rid = request.form.get("run_id")
    if rid and rid.isdigit():
        store.delete_run_log(int(rid))
    return redirect(url_for("runs"))


@app.route("/runs/clear", methods=["POST"])
def runs_clear():
    """수집 실행 기록만 삭제(스냅샷·변동 이력 유지)."""
    store.clear_run_logs()
    return redirect(url_for("runs"))


@app.route("/data/clear", methods=["POST"])
def data_clear():
    """수집 결과 전체 초기화(스냅샷·변동·실행기록 삭제, 업체/소스 유지)."""
    store.clear_collected_data()
    return redirect(url_for("runs"))


@app.route("/run-now", methods=["POST"])
def run_now():
    """백그라운드로 run_once() 실행. AI API 비용 발생 → 액세스 코드 필요."""
    if config.ACCESS_CODE:
        code = (request.form.get("access_code") or "").strip()
        if code != config.ACCESS_CODE:
            return redirect(url_for("runs", error="bad_code"))

    # 부분 수집: scope=selected 면 체크된 소스만, 아니면 전체
    source_ids = None
    if (request.form.get("scope") or "all") == "selected":
        source_ids = [int(x) for x in request.form.getlist("source_ids") if x.isdigit()]
        if not source_ids:
            return redirect(url_for("runs", error="no_selection"))

    if not _run_in_progress["value"]:
        with _run_lock:
            if not _run_in_progress["value"]:
                _run_in_progress["value"] = True
                # 선택 수집이면 그 선택을 진행 중 화면에 유지(전체 수집은 None=전체)
                _running_ids["value"] = set(source_ids) if source_ids else None
                _progress.update(
                    {"running": True, "total": 0, "done": 0,
                     "current": "시작 중…", "ok": 0, "error": 0}
                )
                threading.Thread(
                    target=_background_run,
                    kwargs={"source_ids": source_ids},
                    daemon=True,
                ).start()
    return redirect(url_for("runs"))


def _progress_cb(done: int, total: int, current: str) -> None:
    _progress.update({"done": done, "total": total, "current": current})


def _background_run(source_ids=None) -> None:
    try:
        log.info("[run-now] 수집 시작 (source_ids=%s)", source_ids)
        result = run_once(progress_cb=_progress_cb, source_ids=source_ids)
        _progress.update({"ok": result.ok_count, "error": result.error_count})
        log.info("[run-now] 완료: 성공 %d / 에러 %d",
                 result.ok_count, result.error_count)
    except Exception:  # noqa: BLE001
        log.exception("[run-now] 실패")
    finally:
        _progress.update({"running": False, "current": ""})
        _run_in_progress["value"] = False
        _running_ids["value"] = None


@app.route("/run-progress")
def run_progress():
    return jsonify(_progress)


# ── 내부 API ─────────────────────────────────────────────────
@app.route("/api/snapshots/latest")
def api_latest():
    return jsonify(presenters.latest_snapshots_api())


@app.route("/api/company/<name>/history")
def api_history(name: str):
    data = presenters.company_history_api(name)
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/debug/source")
def debug_source():
    """소스가 실제로 수집한 원문 텍스트를 보여준다(진단용).

    /debug/source?company=<업체>&type=<web|google_search|apple|google_play>
    저장된 마지막 수집 원문이 있으면 그걸, 없으면 즉석으로 가져와 보여준다.
    """
    from ..core import fetch
    from ..core.extract import _clean_page_text

    company = (request.args.get("company") or "").strip()
    stype = (request.args.get("type") or "google_search").strip()
    srcs = store.list_sources(company=company, active_only=False)
    src = next((s for s in srcs if s["source_type"] == stype), None)
    if not src:
        return Response(
            f"source not found: company={company!r} type={stype!r}",
            mimetype="text/plain; charset=utf-8",
            status=404,
        )

    url = fetch.normalize_us_url(stype, src["url"])
    row = store.latest_snapshot_row(company, url)
    stored = (
        row["raw_text"] if row and "raw_text" in row.keys() and row["raw_text"] else None
    )
    if stored:
        body = (
            f"[저장된 마지막 수집 원문]\nURL: {url}\n"
            f"신뢰도: {row['confidence']}\n\n{_clean_page_text(stored)}"
        )
    else:
        try:
            # google_search 는 실제 수집 경로(SerpAPI 키 있으면 SerpAPI)를 그대로 따른다
            if stype == "google_search" and config.SERPAPI_KEY:
                text = fetch.fetch_google_via_serpapi(url)
            else:
                text = fetch.fetch_page_text(url)
            body = f"[즉석 수집]\nURL: {url}\n\n{_clean_page_text(text)}"
        except Exception as exc:  # noqa: BLE001
            body = f"[즉석 수집 실패]\nURL: {url}\n\n{exc}"
    return Response(body[:40000], mimetype="text/plain; charset=utf-8")


@app.route("/debug/serpapi")
def debug_serpapi():
    """SerpAPI 키 인식 여부 + 실제 호출 결과를 진단(Render 설정 검증용).

    /debug/serpapi?company=<업체명>   또는   ?q=<검색어>
    """
    from urllib.parse import quote_plus

    from ..core import fetch

    key = config.SERPAPI_KEY
    masked = f"{key[:4]}…{key[-2:]}" if len(key) >= 6 else ("(미설정)" if not key else "(너무 짧음)")
    lines = [f"SERPAPI_KEY 감지: {bool(key)}   값(마스킹): {masked}"]
    if not key:
        lines.append("→ Render → 서비스 → Environment 에 SERPAPI_KEY 추가 후 재배포가 필요합니다.")
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")

    company = (request.args.get("company") or "").strip()
    q = (request.args.get("q") or "").strip()
    if q:
        url = f"https://www.google.com/search?q={quote_plus(q)}&hl=en&gl=us"
    else:
        url = fetch.build_google_search_url(company or "Notion")
    lines.append(f"테스트 URL: {url}")
    try:
        text = fetch.fetch_google_via_serpapi(url)
        has_ov = "AI OVERVIEW:" in text
        lines.append(f"✅ 성공 — {len(text)}자 수신 · AI Overview 포함: {has_ov}")
        lines.append("-" * 48)
        lines.append(text[:8000])
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ 실패: {exc}")
    return Response("\n".join(lines)[:40000], mimetype="text/plain; charset=utf-8")


@app.route("/priority/move", methods=["POST"])
def priority_move():
    """대표 출처 우선순위 순서 변경(위/아래). company 지정 시 업체별 설정."""
    stype = (request.form.get("type") or "").strip()
    direction = (request.form.get("dir") or "").strip()
    company = (request.form.get("company") or "").strip() or None
    order = presenters.get_priority_order(company)
    if stype in order and direction in ("up", "down"):
        i = order.index(stype)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            presenters.set_priority_order(order, company)
    if company:
        return redirect(url_for("company", name=company))
    return redirect(url_for("index"))


@app.route("/priority/set-primary", methods=["POST"])
def priority_set_primary():
    """업체별 대표 출처를 직접 지정(선택한 출처를 그 업체 우선순위 최상위로)."""
    company = (request.form.get("company") or "").strip()
    stype = (request.form.get("type") or "").strip()
    if company and stype:
        order = presenters.get_priority_order(company)
        if stype in order:
            order.remove(stype)
            order.insert(0, stype)
            presenters.set_priority_order(order, company)
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "scheduler_mode": config.SCHEDULER_MODE})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
