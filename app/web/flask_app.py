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
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import config
from ..core import presenters, store
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

# [지금 수집] 중복 실행 방지용 락
_run_lock = threading.Lock()
_run_in_progress = {"value": False}


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


@app.route("/companies")
def companies_page():
    return render_template(
        "companies.html",
        data=presenters.companies_admin(),
        source_types=SOURCE_TYPE_LABELS,
        error=request.args.get("error"),
    )


def _norm_type(value: str) -> str:
    value = (value or "web").strip().lower()
    return value if value in SOURCE_TYPES else "other"


def _resolve_url(company: str, source_type: str, url: str) -> str:
    """구글 검색 소스는 URL 을 비우면 업체명으로 자동 생성한다."""
    if not url and source_type == "google_search":
        return build_google_search_url(company)
    return url


@app.route("/companies/add", methods=["POST"])
def companies_add():
    """업체 + 첫 소스를 함께 등록."""
    name = (request.form.get("name") or "").strip()
    source_type = _norm_type(request.form.get("source_type"))
    url = _resolve_url(name, source_type, (request.form.get("url") or "").strip())

    if not name:
        return redirect(url_for("companies_page", error="업체명은 필수입니다."))
    if not url:
        return redirect(url_for("companies_page", error="소스 URL은 필수입니다."))

    store.add_source(company=name, source_type=source_type, url=url)
    return redirect(url_for("companies_page"))


@app.route("/companies/delete", methods=["POST"])
def companies_delete():
    name = (request.form.get("name") or "").strip()
    if name:
        store.delete_company(name)
    return redirect(url_for("companies_page"))


@app.route("/sources/add", methods=["POST"])
def sources_add():
    """기존 업체에 소스 URL 추가."""
    company = (request.form.get("company") or "").strip()
    source_type = _norm_type(request.form.get("source_type"))
    url = _resolve_url(company, source_type, (request.form.get("url") or "").strip())

    if not (company and url):
        return redirect(url_for("companies_page", error="업체와 소스 URL이 필요합니다."))

    store.add_source(company=company, source_type=source_type, url=url)
    return redirect(url_for("companies_page"))


@app.route("/sources/delete", methods=["POST"])
def sources_delete():
    sid = request.form.get("source_id")
    if sid and sid.isdigit():
        store.delete_source(int(sid))
    return redirect(url_for("companies_page"))


@app.route("/runs")
def runs():
    return render_template(
        "runs.html",
        data=presenters.runs_view(),
        running=_run_in_progress["value"],
    )


# ── 액션 ─────────────────────────────────────────────────────
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
    """백그라운드로 run_once() 실행."""
    if not _run_in_progress["value"]:
        with _run_lock:
            if not _run_in_progress["value"]:
                _run_in_progress["value"] = True
                threading.Thread(target=_background_run, daemon=True).start()
    return redirect(url_for("runs"))


def _background_run() -> None:
    try:
        log.info("[run-now] 수집 시작")
        result = run_once()
        log.info("[run-now] 완료: 성공 %d / 에러 %d",
                 result.ok_count, result.error_count)
    except Exception:  # noqa: BLE001
        log.exception("[run-now] 실패")
    finally:
        _run_in_progress["value"] = False


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


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "scheduler_mode": config.SCHEDULER_MODE})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
