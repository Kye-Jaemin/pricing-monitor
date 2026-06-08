# 2단계 배포 — 로컬 PC (Flask + OS 스케줄러)

Render 와 **동일 코드**. 바뀌는 것은 설정(`.env`)과 스케줄 방식뿐이다.
`core/` · `templates/` · `companies.yaml` · DB 스키마는 그대로.

## 1. 데이터 이전 (선택)
- Render 의 `pricing.db` 를 로컬 `./data/pricing.db` 로 복사하면 이력 유지.
- 새로 시작해도 무방(첫 수집부터 누적).

## 2. 의존성 설치
```powershell
cd C:\Users\<you>\AI cowork\pricing-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## 3. `.env` 설정
`.env.example` 를 `.env` 로 복사 후:
```
ANTHROPIC_API_KEY=sk-ant-...
DB_PATH=./data/pricing.db
SCHEDULER_MODE=external          # ← OS 스케줄러가 담당, 내부 APScheduler 끔
LOCALE=en-US                     # US/USD 1차 방어선 (8장)
TIMEZONE_ID=America/New_York
ACCEPT_LANGUAGE=en-US,en;q=0.9
PORT=8000
```

## 4. 웹 서버 기동 (읽기 전용 대시보드)
```powershell
waitress-serve --port=8000 app.web.flask_app:app
# 또는 개발 모드: flask --app app.web.flask_app run --port 8000
```
→ http://localhost:8000

## 5. 수집 1회 테스트 (웹 없이도 동작)
```powershell
python -m app.core.pipeline
# 또는
python scripts\run_collect.py
```

## 6. 주간 수집을 Windows 작업 스케줄러에 등록
1. **작업 스케줄러** → **작업 만들기**.
2. **일반**: "사용자의 로그온 여부에 관계없이 실행", "가장 높은 권한으로 실행".
3. **트리거**: 매주 · 월요일 · 09:00.
4. **동작**: 프로그램 시작
   - 프로그램: `C:\Users\<you>\AI cowork\pricing-monitor\.venv\Scripts\python.exe`
   - 인수: `-m app.core.pipeline`
   - 시작 위치: `C:\Users\<you>\AI cowork\pricing-monitor`
5. **조건**: "작업을 실행하기 위해 컴퓨터를 절전 모드에서 해제" 체크.

> PC 가 꺼져 있을 수 있으므로 in-process 스케줄러 대신 OS 스케줄러를 쓴다(`SCHEDULER_MODE=external`).

### macOS / Linux 참고
- macOS: `launchd`(StartCalendarInterval) 또는 cron.
- Linux: `0 9 * * 1 cd /path && /path/.venv/bin/python -m app.core.pipeline`
  또는 systemd timer(`Persistent=true` 로 부팅 후 누락분 실행).

## 7. US / USD 확인 (한국 IP 주의)
로컬은 한국 IP 라 일부 사이트가 비-USD 를 줄 수 있다(8장):
1. `LOCALE=en-US`, `TIMEZONE_ID`, `ACCEPT_LANGUAGE` 가 1차 방어선.
2. 그래도 비-USD 면 `companies.yaml` 의 `pricing_url` 을 US 전용 경로로
   (`?currency=usd`, `/us/pricing` 등) 지정.
3. 결과 `currency != USD` 면 자동으로 `confidence=low` + 검수 플래그(임의 환산 안 함).
