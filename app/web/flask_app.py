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
    redirect,
    render_template,
    request,
    url_for,
)

from .. import config
from ..core import presenters, store
from ..core.pipeline import run_once
from ..scheduler import start_scheduler

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


@app.route("/runs")
def runs():
    return render_template(
        "runs.html",
        data=presenters.runs_view(),
        running=_run_in_progress["value"],
    )


# ── 액션 ─────────────────────────────────────────────────────
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
