"""OS 스케줄러용 얇은 래퍼 (external 모드).

run_once() 를 호출하는 것 외에 아무 로직도 없다. Windows 작업 스케줄러 /
cron / launchd 가 이 파일을 호출한다.

사용:
    python scripts/run_collect.py
또는 동등하게:
    python -m app.core.pipeline
"""
import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가 (어디서 호출하든 동작)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.pipeline import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
