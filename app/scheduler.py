"""❸ APScheduler — internal 모드(Render 상시)에서만 사용 (11장).

웹 프로세스 내에서 주 1회 run_once() 를 발화한다.
external 모드(로컬 PC)에서는 시작하지 않는다 — OS 스케줄러가 담당.
"""
from __future__ import annotations

import logging

from . import config
from .core.pipeline import run_once

log = logging.getLogger("pricing.scheduler")

_scheduler = None  # 싱글톤


def start_scheduler() -> bool:
    """internal 모드일 때만 APScheduler 를 기동. 기동했으면 True."""
    global _scheduler

    if config.SCHEDULER_MODE != "internal":
        log.info("SCHEDULER_MODE=%s → 내부 스케줄러 비활성 (OS 가 담당)",
                 config.SCHEDULER_MODE)
        return False

    if _scheduler is not None:
        return True

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    _scheduler = BackgroundScheduler(timezone=config.SCHEDULE_TIMEZONE)
    trigger = CronTrigger(
        day_of_week=config.SCHEDULE_DAY_OF_WEEK,
        hour=config.SCHEDULE_HOUR,
        minute=config.SCHEDULE_MINUTE,
        timezone=config.SCHEDULE_TIMEZONE,
    )
    _scheduler.add_job(
        _job,
        trigger=trigger,
        id="weekly_collect",
        # 재시작으로 놓친 주간 실행을 다음 기동 시 보충
        coalesce=True,
        misfire_grace_time=6 * 60 * 60,  # 6시간
        max_instances=1,
        replace_existing=True,
    )
    _scheduler.start()
    log.info(
        "내부 스케줄러 기동: %s %02d:%02d %s",
        config.SCHEDULE_DAY_OF_WEEK,
        config.SCHEDULE_HOUR,
        config.SCHEDULE_MINUTE,
        config.SCHEDULE_TIMEZONE,
    )
    return True


def _job() -> None:
    log.info("주간 수집 시작")
    try:
        result = run_once()
        log.info("주간 수집 완료: 성공 %d / 에러 %d",
                 result.ok_count, result.error_count)
    except Exception:  # noqa: BLE001
        log.exception("주간 수집 실패")
