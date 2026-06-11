"""❹ Flask 어댑터 — 현재+향후 공통 (권장). 로직 없음, core 호출 + 렌더링만.

기동:
  개발  flask --app app.web.flask_app run --port 8000
  운영  waitress-serve --port=8000 app.web.flask_app:app
        gunicorn app.web.flask_app:app --bind 0.0.0.0:$PORT   (Render)
"""
from __future__ import annotations

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
_progress = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "ok": 0,
    "error": 0,
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
    return render_template("changes.html", data=presenters.changes_view(company_filter))


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


@app.route("/compare/categorize", methods=["POST"])
def compare_categorize():
    """선택 업체 유료 기능을 AI로 동적 카테고리 분류(사용자 지정은 보존). AI 작업→코드 필요."""
    names = [n for n in request.form.getlist("company") if n]
    if config.ACCESS_CODE and (request.form.get("access_code") or "").strip() != config.ACCESS_CODE:
        return redirect(_compare_url(names, error="bad_code"))

    feats = presenters.distinct_paid_features(names)
    if not feats:
        return redirect(_compare_url(names, error="no_features"))
    try:
        from ..core import extract

        mapping = extract.categorize_features_ai(feats)
    except Exception:  # noqa: BLE001
        log.exception("[compare] AI 카테고리 분류 실패")
        return redirect(_compare_url(names, error="ai_failed"))

    existing = store.get_feature_category_rows()
    for feature, category in mapping.items():
        # 사용자가 직접 지정한 항목은 덮어쓰지 않음
        if existing.get(feature, (None, "ai"))[1] == "user":
            continue
        store.set_feature_category(feature, category, source="ai")
    return redirect(_compare_url(names))


@app.route("/compare/set-category", methods=["POST"])
def compare_set_category():
    """기능의 카테고리를 사용자가 직접 지정/추가."""
    names = [n for n in request.form.getlist("company") if n]
    feature = (request.form.get("feature") or "").strip()
    category = (request.form.get("category") or "").strip()
    if feature and category:
        store.set_feature_category(feature, category, source="user")
    return redirect(_compare_url(names))


@app.route("/value")
def value_page():
    """공급 측 피쳐 가치 분석 — 2×2 산점도 + 정렬 가능한 표."""
    from ..core import value_analysis

    return render_template("value.html", data=value_analysis.analyze())


@app.route("/companies")
def companies_page():
    return render_template(
        "companies.html",
        data=presenters.companies_admin(),
        source_types=SOURCE_TYPE_LABELS,
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


def _resolve_source_url(company: str, source_type: str, url: str):
    """소스 URL 을 결정한다. 비어 있으면 종류별로 자동 생성/탐색.

    반환: (url, icon, error). url 이 None 이면 추가 불가(error 사유 포함).
      - google_search: 업체명으로 검색 URL 자동 생성
      - apple / google_play: 스토어에서 앱 자동 탐색(자동 찾기와 동일)
      - web / other: URL 필수
    """
    url = (url or "").strip()
    if url:
        return url, None, None
    if source_type == "google_search":
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
    """업체 + 첫 소스를 함께 등록. 스토어 종류는 URL 없이 자동 탐색."""
    name = (request.form.get("name") or "").strip()
    source_type = _norm_type(request.form.get("source_type"))
    if not name:
        return redirect(url_for("companies_page", error="업체명은 필수입니다."))

    url, icon, error = _resolve_source_url(name, source_type, request.form.get("url"))
    if error:
        return redirect(url_for("companies_page", error=error))

    store.add_source(company=name, source_type=source_type, url=url)
    if icon:
        store.set_company_icon(name, icon)
    return redirect(url_for("companies_page"))


@app.route("/companies/delete", methods=["POST"])
def companies_delete():
    name = (request.form.get("name") or "").strip()
    if name:
        store.delete_company(name)
    return redirect(url_for("companies_page"))


@app.route("/sources/add", methods=["POST"])
def sources_add():
    """기존 업체에 소스 추가. 스토어 종류는 URL 없이 자동 탐색."""
    company = (request.form.get("company") or "").strip()
    source_type = _norm_type(request.form.get("source_type"))
    if not company:
        return redirect(url_for("companies_page", error="업체가 필요합니다."))

    url, icon, error = _resolve_source_url(company, source_type, request.form.get("url"))
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
    return render_template(
        "runs.html",
        data=presenters.runs_view(),
        companies=presenters.companies_admin()["companies"],
        running=_run_in_progress["value"],
        access_required=bool(config.ACCESS_CODE),
        contact=config.ACCESS_CONTACT,
        error=request.args.get("error"),
    )


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
            text = fetch.fetch_page_text(url)
            body = f"[즉석 수집]\nURL: {url}\n\n{_clean_page_text(text)}"
        except Exception as exc:  # noqa: BLE001
            body = f"[즉석 수집 실패]\nURL: {url}\n\n{exc}"
    return Response(body[:40000], mimetype="text/plain; charset=utf-8")


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
