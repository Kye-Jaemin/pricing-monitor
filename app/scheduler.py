"""❸ APScheduler — internal 모드(Render 상시)에서만 사용 (11장).

웹 프로세스 내에서 주기적으로 run_once() 를 발화한다.
external 모드(로컬 PC)에서는 시작하지 않는다 — OS 스케줄러가 담당.

스케줄 설정(온/오프·주기·stale 일수)은 DB(settings)에 저장되어 웹 UI에서
바꿀 수 있고, 재시작 없이 reconfigure() 로 즉시 반영된다(env 값은 기본값).
"""
from __future__ import annotations

import logging

from . import config
from .core import store
from .core.pipeline import run_once

log = logging.getLogger("pricing.scheduler")

_scheduler = None  # 싱글톤
_JOB_ID = "weekly_collect"


def _to_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def get_settings() -> dict:
    """현재 스케줄 설정(DB 우선, 없으면 env 기본값)."""
    g = store.get_setting
    en = g("sched.enabled")
    return {
        "enabled": (en == "1") if en is not None else True,
        "day_of_week": g("sched.day_of_week") or config.SCHEDULE_DAY_OF_WEEK,
        "hour": _to_int(g("sched.hour"), config.SCHEDULE_HOUR),
        "minute": _to_int(g("sched.minute"), config.SCHEDULE_MINUTE),
        "timezone": g("sched.timezone") or config.SCHEDULE_TIMEZONE,
        "stale_days": _to_int(g("sched.stale_days"), config.SCHEDULE_STALE_DAYS),
    }


def save_settings(
    *, enabled: bool, day_of_week: str, hour: int, minute: int,
    timezone: str, stale_days: int,
) -> None:
    store.set_setting("sched.enabled", "1" if enabled else "0")
    store.set_setting("sched.day_of_week", day_of_week)
    store.set_setting("sched.hour", str(hour))
    store.set_setting("sched.minute", str(minute))
    store.set_setting("sched.timezone", timezone)
    store.set_setting("sched.stale_days", str(stale_days))
    reconfigure()


def reconfigure() -> None:
    """DB 설정대로 잡을 다시 건다(켜짐이면 등록, 꺼짐이면 제거). internal 모드 전용."""
    if _scheduler is None:
        return
    from apscheduler.triggers.cron import CronTrigger

    try:
        _scheduler.remove_job(_JOB_ID)
    except Exception:  # noqa: BLE001  (잡이 없을 때)
        pass

    s = get_settings()
    if not s["enabled"]:
        log.info("스케줄러 OFF — 자동 수집 잡 제거")
        return

    _scheduler.add_job(
        _job,
        trigger=CronTrigger(
            day_of_week=s["day_of_week"], hour=s["hour"], minute=s["minute"],
            timezone=s["timezone"],
        ),
        id=_JOB_ID,
        coalesce=True,
        misfire_grace_time=6 * 60 * 60,
        max_instances=1,
        replace_existing=True,
    )
    log.info("스케줄러 ON — %s %02d:%02d %s (stale_days=%s)",
             s["day_of_week"], s["hour"], s["minute"], s["timezone"], s["stale_days"])


def start_scheduler() -> bool:
    """internal 모드일 때만 APScheduler 를 기동하고 DB 설정대로 잡을 건다."""
    global _scheduler

    if config.SCHEDULER_MODE != "internal":
        log.info("SCHEDULER_MODE=%s → 내부 스케줄러 비활성 (OS 가 담당)",
                 config.SCHEDULER_MODE)
        return False

    if _scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler

        _scheduler = BackgroundScheduler(timezone=config.SCHEDULE_TIMEZONE)
        _scheduler.start()

    reconfigure()
    return True


def get_status() -> dict:
    """UI용 상태: 설정값 + internal 활성 여부 + 다음 실행 시각."""
    s = get_settings()
    s["internal"] = config.SCHEDULER_MODE == "internal"
    next_run = None
    if _scheduler is not None:
        job = _scheduler.get_job(_JOB_ID)
        if job and job.next_run_time:
            next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M %Z")
    s["next_run_time"] = next_run
    s["active"] = s["internal"] and s["enabled"] and next_run is not None
    return s


def _job() -> None:
    stale = get_settings()["stale_days"]
    log.info("자동 수집 시작 (stale_days=%s 만 대상)", stale)
    try:
        result = run_once(stale_days=stale)
        log.info("자동 수집 완료: 대상 %d개 · 성공 %d / 에러 %d",
                 len(result.results), result.ok_count, result.error_count)
    except Exception:  # noqa: BLE001
        log.exception("자동 수집 실패")
