# 구독 서비스 Pricing 모니터

여러 업체의 **구독 Pricing**(USD 금액·Tier별 기능)을 **공식 US 페이지 기준**으로
수집·구조화 저장하고, **주 1회** 자동 실행하며 **이전 주 대비 변동을 감지**하고
**웹 대시보드**로 조회한다.

> 설계 핵심: Render(상시) → 로컬 PC 이전을 *프레임워크 재작성이 아닌 설정 변경*으로.
> `core/`(프레임워크 무관) / `config.py`(환경 흡수) / `web/`(얇은 어댑터) 3층 분리.

## 빠른 시작 (로컬)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

copy .env.example .env        # ANTHROPIC_API_KEY 채우기

# 수집 1회 (웹 없이도 완결: 수집·저장·diff)
python -m app.core.pipeline

# 대시보드
waitress-serve --port=8000 app.web.flask_app:app   # http://localhost:8000
```

## 구조
```
app/
  core/        ❶ 프레임워크 비의존 — fetch/extract/store/diff/presenters/pipeline
  config.py    ❷ 환경 흡수층 (env 만 읽음)
  scheduler.py ❸ APScheduler (internal 모드 전용)
  web/         ❹ 얇은 Flask 어댑터
templates/     Jinja2 (Flask/FastAPI 공용)
static/        style.css
companies.yaml 모니터링 대상
scripts/       OS 스케줄러용 래퍼
docs/          deploy_render.md / deploy_local.md
```

## 파이프라인 (`core/pipeline.py: run_once()`)
1. `companies.yaml` 로드
2. 업체별: Playwright(US 로케일) 렌더링 → 본문 텍스트
3. `raw_text_hash` 가 직전과 같으면 추출 스킵("변동 없음")
4. Claude API 추출 → Pydantic 검증(실패 1회 재시도)
5. `currency != USD` → confidence=low(임의 환산 금지)
6. 직전 스냅샷과 diff → `changes` 기록 → 스냅샷·run_log 저장

`run_once()` 는 웹·APScheduler·OS cron 의 **단일 진입점**.

## 화면
- `/` 현황 · `/company/<name>` 가격 추이 차트 · `/changes` 변동 로그 · `/runs` 수집 상태([지금 수집])
- API: `/api/snapshots/latest`, `/api/company/<name>/history`, `POST /run-now`

## 배포
- 1단계 Render: [docs/deploy_render.md](docs/deploy_render.md)
- 2단계 로컬 PC: [docs/deploy_local.md](docs/deploy_local.md)

데이터는 SQLite 파일 1개(`config.DB_PATH`, WAL 모드). **이전은 이 파일 복사로 끝**.
