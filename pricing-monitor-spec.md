# 구독 서비스 Pricing 모니터링 — 작업 명세서 (for Claude Code)

## 1. 목적 (Goal)

여러 업체를 입력하면, 각 업체의 **구독 서비스 Pricing 정보**(USD 금액, Tier별 제공 기능)를
**공식 홈페이지(US 페이지) 기준**으로 수집하여 구조화 저장한다.
**주 1회 스케줄러**로 자동 실행하며, **이전 주 대비 가격·티어 변동을 감지**하고,
**웹 대시보드**로 현황과 변동 이력을 조회한다.

배포 환경은 **(1단계) Render 상시 Web Service → (2단계) 로컬 PC 서버**로 이전할 예정이다.
따라서 **환경·프레임워크 변경 비용을 최소화하는 이식성 있는 구조**로 설계하는 것이 중요한 요구사항이다.

---

## 2. 핵심 설계 판단 (Key Design Decisions)

### 2.1 수집 방식
사이트별 전용 스크레이퍼는 만들지 않는다. 업체마다 구조가 다르고 JS 렌더링이 많아 파서가 쉽게 깨지기 때문이다.
주 1회 실행이라 LLM 추출 비용/속도는 문제가 안 된다. 다음 2단계 파이프라인을 쓴다:
1. **Fetch**: Playwright(headless)로 US 로케일 렌더링 → 본문 텍스트 확보
2. **Extract**: Claude API로 정해진 JSON 스키마 구조화 추출

### 2.2 이식성 우선 — 3층 분리 (가장 중요)
Render(FastAPI/Flask 무관) → 로컬 PC(Flask) 이전을 *프레임워크 재작성이 아니라 설정 변경*으로 만든다.

- **권장: 처음부터 Flask로 통일.** Flask는 Render에서도 동작하므로, 향후 이전 시 **프레임워크 교체가 불필요**해진다. (변경 대상이 배포/스케줄/설정으로 축소됨)
- 코드를 3층으로 분리한다:
  - **❶ `core/` — 프레임워크 비의존 계층.** fetch/extract/store/diff/pipeline. **절대 web(flask/fastapi)을 import하지 않는다.** 환경이 바뀌어도 이 계층은 수정 없음.
  - **❷ `config.py` — 환경 흡수 계층.** DB 경로, 스케줄 방식, 로케일, 포트를 모두 환경변수로 읽는다. Render↔로컬 차이는 오직 여기(=env 값)에서만 흡수.
  - **❸ web 어댑터 — 얇은 계층.** 요청을 받아 core 함수를 호출하고 Jinja2 템플릿을 렌더링만 한다. 로직을 담지 않는다.
- **Jinja2 템플릿은 Flask/FastAPI 공용**이므로 `templates/`는 프레임워크가 바뀌어도 그대로 재사용된다.
- **수집 1회를 독립 CLI(`python -m app.core.pipeline`)로** 제공한다. 웹·스케줄러·OS cron 누가 호출해도 동일하게 동작 → 환경별 스케줄링 방식만 갈아끼우면 된다.

### 2.3 데이터 이식성
SQLite 파일 1개(`pricing.db`)가 전체 데이터다. **환경 이전은 이 파일 복사로 끝난다.**
DB 경로는 `config.DB_PATH`로만 참조하고, **WAL 모드**를 켜서 (웹 읽기 + 스케줄러 쓰기) 동시 접근에 안전하게 한다.

---

## 3. 기술 스택 (Tech Stack)

| 영역 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.11+ | Render·로컬 공통 |
| 웹/대시보드 | **Flask** + Jinja2 (+ HTMX) | Render·로컬 양쪽에서 동일 사용(권장) |
| 차트 | Chart.js (CDN) | 가격 추이 시계열 |
| 페이지 수집 | Playwright (Chromium, headless) | JS 렌더링 + US 로케일 강제 |
| 구조화 추출 | Anthropic Claude API (`claude-sonnet`) | 텍스트 → JSON |
| 스키마 검증 | Pydantic v2 | 추출 결과 강제 검증 |
| 저장소 | SQLite (WAL 모드, `config.DB_PATH`) | 파일 복사로 이식 |
| 스케줄(상시 환경) | APScheduler (웹 프로세스 내 cron) | Render용 |
| 스케줄(로컬 PC) | OS 스케줄러 → CLI 호출 | Windows 작업 스케줄러 / cron |
| 프로덕션 서버 | Render: gunicorn / 로컬: waitress 또는 gunicorn | WSGI |
| 설정 | `.env` + `config.py` | 환경 흡수층 |

> 대안: 굳이 지금 FastAPI로 시작하고 싶다면, web 어댑터만 `fastapi_app.py`로 두면 된다.
> `core/`와 `templates/`는 그대로이므로 교체는 어댑터 1개 수준. 단 이 경우 로컬 이전 때 Flask로 재작성이 필요하므로 **Flask 통일을 권장**한다.

---

## 4. 입력 (Input)

업체 목록은 `companies.yaml`로 관리한다. 가능하면 US Pricing 페이지 URL을 직접 지정한다(가장 안정적).

```yaml
companies:
  - name: Notion
    homepage: https://www.notion.so
    pricing_url: https://www.notion.so/pricing
  - name: Figma
    homepage: https://www.figma.com
    pricing_url: https://www.figma.com/pricing/
  - name: Linear
    homepage: https://linear.app
    pricing_url: https://linear.app/pricing
```

- `name`(필수) / `pricing_url`(권장) / `homepage`(선택, 미지정 시 `{homepage}/pricing` 추정)

---

## 5. 데이터 모델 (Data Model)

### 추출 JSON 스키마 (Pydantic로 검증)

```json
{
  "company": "Notion",
  "source_url": "https://www.notion.so/pricing",
  "collected_at": "2026-06-08T09:00:00Z",
  "currency": "USD",
  "tiers": [
    {
      "name": "Free",
      "monthly_price": 0,
      "annual_price_per_month": 0,
      "billing_unit": "per_user",
      "price_note": null,
      "features": ["무제한 페이지", "기본 협업"],
      "limits": {"file_upload": "5MB"}
    },
    {
      "name": "Enterprise",
      "monthly_price": null,
      "annual_price_per_month": null,
      "billing_unit": "per_user",
      "price_note": "Contact Sales (비공개)",
      "features": ["SAML SSO", "감사 로그"],
      "limits": {}
    }
  ],
  "extraction_confidence": "high"
}
```

필드 규칙:
- `monthly_price`/`annual_price_per_month`: 숫자(USD). 비공개면 `null` + `price_note`.
- `billing_unit`: `per_user` | `flat` | `usage_based` | `unknown`.
- `currency`: **USD 기대**. 아니면 confidence=low로 플래그(8장).
- `extraction_confidence`: `high` | `medium` | `low`.

### SQLite 테이블

```
snapshots(id, company, source_url, collected_at, currency, raw_text_hash, payload_json, confidence)
changes(id, company, detected_at, change_type, tier_name, field, old_value, new_value, summary)
run_logs(id, run_started_at, run_finished_at, company, status, error_message)
```

- `raw_text_hash`: 직전과 동일하면 추출 스킵(비용 절감).
- `change_type`: `price_changed` | `tier_added` | `tier_removed` | `feature_changed`.
- **DB 초기화 시 `PRAGMA journal_mode=WAL` 적용**, 커넥션은 짧게 열고 닫는다.

---

## 6. 처리 흐름 (Workflow) — `core/pipeline.py: run_once()`

```
1. companies.yaml 로드
2. 각 업체 반복:
   a. pricing_url 결정 (없으면 {homepage}/pricing)
   b. Playwright로 US 로케일 렌더링 → 본문 텍스트
   c. raw_text_hash 계산. 직전과 동일하면 추출 생략 → "변동 없음"
   d. Claude API 추출 → JSON
   e. Pydantic 검증. 실패 시 1회 재시도, 그래도 실패면 run_logs 에러 기록
   f. currency != USD 이면 confidence=low + note
3. 직전 스냅샷과 diff → changes 기록
4. 스냅샷 저장 + run_logs 기록
```

> `run_once()`는 웹·APScheduler·OS cron이 **공통으로 호출**하는 단일 진입점이다.
> CLI 실행: `python -m app.core.pipeline`

### Claude 추출 프롬프트 (핵심 — `core/extract.py`)
```
You are a pricing data extractor. From the given web page text, extract the
subscription pricing tiers. Return ONLY valid JSON matching this schema:
{여기에 5장 스키마 삽입}

Rules:
- Prices are expected in USD. If the page shows a non-USD currency, set
  currency accordingly and lower extraction_confidence.
- If a tier has no public price (e.g. "Contact Sales"), set prices to null and
  put the reason in price_note.
- Do not invent features or prices. If unsure, lower extraction_confidence.
- Output JSON only. No prose, no code fences.
```

---

## 7. 변동 감지 (Change Detection) — `core/diff.py`

직전 스냅샷의 `payload_json`과 이번 회차 비교:
- **가격**: 동일 티어의 월/연 가격 차이 → `price_changed`
- **티어**: 이름 집합 비교 → `tier_added` / `tier_removed`
- **기능**: `features` 차집합 → `feature_changed`

각 변동은 `old → new`와 한 줄 요약을 기록. 예: `"Notion Plus: $8 → $10 (per user/mo) 인상"`.

---

## 8. US / USD 강제 (Currency Enforcement)

1. **US 리전/IP**: Render는 US 리전(Oregon 등) 배포. 로컬 PC가 한국이면 일부 사이트가 비-USD를 줄 수 있으므로 2·4번으로 보강.
2. **Playwright 컨텍스트**: `locale="en-US"`, `timezone_id="America/New_York"`, header `Accept-Language: en-US,en;q=0.9`.
3. **검증**: 결과 `currency`가 `USD`가 아니면 confidence=low + note(임의 환산 금지).
4. **URL 보강**: 그래도 통화가 안 맞으면 해당 업체 `pricing_url`을 US 전용 경로(`?currency=usd`, `/us/pricing` 등)로 지정.

> 로컬 PC(한국 IP)에서는 IP 기반 지역 추정으로 비-USD가 더 자주 나올 수 있다.
> 이때 2·4번이 1차 방어선이며, 정 안 되면 환율 환산 컬럼 추가를 옵션으로 검토(기본 미적용).

---

## 9. 엣지 케이스 & 정책 (Edge Cases)

- **비공개 가격(Enterprise)**: 에러 아님. price=null + note.
- **추출 실패/low**: 직전 스냅샷 유지 + 검수 플래그. 임의 추정 금지.
- **봇 감지**: 현실적 User-Agent, 지수 백오프(최대 3회), 요청 간 지연.
- **접근 예의**: 공개 Pricing 페이지 읽기 수준, robots.txt 존중, 주 1회면 충분.
- **동시 접근**: WAL 모드 + 짧은 커넥션으로 웹 읽기/스케줄러 쓰기 충돌 방지.
- **Playwright 설치**: 빌드/설치 단계에서 `playwright install chromium`(Render는 `--with-deps`). 메모리 1GB+ 권장.

---

## 10. 웹 대시보드 (Dashboard)

Flask + Jinja2 서버 렌더링. SQLite를 직접 읽는다. **로직은 `core/presenters.py`(프레임워크 무관)** 에 두고 라우트는 얇게.

1. **현황** `/` — 전체 업체 × 티어 최신 가격표(월/연(월환산)/USD/과금단위) + 이번 주 변동 배지
2. **업체 상세** `/company/<name>` — 티어별 **가격 추이 차트**(Chart.js) + 현재 기능/limits + 원본 링크
3. **변동 로그** `/changes` — 최근 변동 목록(`old→new`, 요약), 업체/기간 필터
4. **수집 상태** `/runs` — 최근 실행 시각, 업체별 성공/실패/confidence, **[지금 수집] 버튼**(`POST /run-now`, 백그라운드)

내부 API: `GET /api/snapshots/latest`, `GET /api/company/<name>/history`, `POST /run-now`

---

## 11. 스케줄링 (환경별)

수집 1회(`run_once()`)는 동일하고, **트리거 방식만 환경에 따라 다르다.** `config.SCHEDULER_MODE`로 선택.

| 환경 | `SCHEDULER_MODE` | 트리거 | 이유 |
|---|---|---|---|
| Render(상시) | `internal` | 웹 프로세스 내 **APScheduler** (cron: 매주 월 09:00 ET) | 항상 켜져 있어 안정적 |
| 로컬 PC | `external` | **OS 스케줄러 → CLI**(`python -m app.core.pipeline`) | PC가 꺼져 있을 수 있어 in-process는 누락 위험 |

- **internal 모드 보강**: APScheduler에 `coalesce=True`, 넉넉한 `misfire_grace_time` 설정 → 재시작으로 놓친 주간 실행을 다음 기동 시 보충.
- **external 모드(로컬) 등록 예시**
  - Windows 작업 스케줄러: 매주 월요일, 동작 `python C:\path\app\core\pipeline.py`, "로그온 여부와 무관하게 실행" + "작업 실행을 위해 컴퓨터 절전 모드 해제".
  - macOS: `launchd`(StartCalendarInterval) 또는 cron.
  - Linux: cron `0 9 * * 1 ...` 또는 systemd timer(`Persistent=true`로 부팅 후 누락분 실행).

---

## 12. 프로젝트 구조 (Deliverables)

```
pricing-monitor/
├─ app/
│  ├─ core/                  # ❶ 프레임워크 비의존 (web import 금지)
│  │  ├─ models.py           #    Pydantic 스키마
│  │  ├─ fetch.py            #    Playwright (US 로케일)
│  │  ├─ extract.py          #    Claude API 추출
│  │  ├─ store.py            #    SQLite (config.DB_PATH, WAL)
│  │  ├─ diff.py             #    변동 감지
│  │  ├─ presenters.py       #    DB → 화면용 데이터 (프레임워크 무관)
│  │  └─ pipeline.py         #    run_once() + CLI 진입점(__main__)
│  ├─ config.py              # ❷ 환경 흡수층 (env: DB_PATH, SCHEDULER_MODE, LOCALE, PORT...)
│  ├─ scheduler.py           # ❸ APScheduler (internal 모드에서만)
│  └─ web/                   # ❹ 얇은 어댑터
│     ├─ flask_app.py        #    현재+향후 공통 (권장)
│     └─ fastapi_app.py      #    (선택) 동일 구조의 대안 — 같은 templates/core 사용
├─ templates/                # Jinja2 — Flask/FastAPI 공용
├─ static/
├─ companies.yaml
├─ .env.example              # ANTHROPIC_API_KEY / DB_PATH / SCHEDULER_MODE / ...
├─ requirements.txt
├─ render.yaml               # 1단계: Render 배포
├─ scripts/
│  └─ run_collect.py         # OS 스케줄러용 얇은 래퍼 (= run_once 호출)
└─ docs/
   ├─ deploy_render.md       # 1단계 가이드
   └─ deploy_local.md        # 2단계: 로컬 PC + Flask + OS 스케줄러 가이드
```

- 로컬 1회: `python -m app.core.pipeline`
- 로컬 서버: `waitress-serve --port=8000 app.web.flask_app:app` (또는 `flask --app app.web.flask_app run`)

---

## 13. 환경 이전 절차 (Render → 로컬 PC)

코드 변경 없이 **설정·실행 방식만** 바뀐다:

1. `pricing.db`를 로컬로 복사(이력 유지) — 또는 새로 시작.
2. `.env` 조정: `DB_PATH=./data/pricing.db`, `SCHEDULER_MODE=external`.
3. 의존성 설치: `pip install -r requirements.txt` + `playwright install chromium`.
4. 웹 서버 기동: `waitress-serve ... app.web.flask_app:app`.
5. 주간 수집을 **OS 스케줄러**에 등록(11장) — `scripts/run_collect.py` 호출.
6. (US/USD) 로컬은 한국 IP이므로 8장 2·4번 설정 확인.

> `core/`·`templates/`·`companies.yaml`·DB 스키마는 **그대로**. 프레임워크가 Flask로 통일돼 있으면 web 코드도 변경 없음.

---

## 14. Render 배포 (1단계) — `render.yaml`

```yaml
services:
  - type: web
    name: pricing-monitor
    runtime: python
    region: oregon            # US 리전 → USD
    plan: starter             # 유료(상시) + 디스크 부착
    buildCommand: |
      pip install -r requirements.txt
      playwright install --with-deps chromium
    startCommand: gunicorn app.web.flask_app:app --bind 0.0.0.0:$PORT
    disk:
      name: data
      mountPath: /var/data
      sizeGB: 1
    envVars:
      - key: ANTHROPIC_API_KEY
        sync: false
      - key: DB_PATH
        value: /var/data/pricing.db   # ← Persistent Disk 경로 필수
      - key: SCHEDULER_MODE
        value: internal
```

> ⚠️ Render 기본 파일시스템은 휘발성. SQLite는 반드시 Persistent Disk 경로에 둔다.
> 무료 인스턴스는 디스크 불가 + 15분 후 잠들어 스케줄러가 멈추므로 유료 상시 인스턴스 전제.

---

## 15. 완료 기준 (Acceptance Criteria)

- [ ] `core/`의 어떤 모듈도 flask/fastapi를 import하지 않는다(이식성 검증).
- [ ] `python -m app.core.pipeline` 단독 실행으로 수집·저장·diff가 완결된다(웹 없이).
- [ ] 3개 업체 수집 시 티어/가격/기능이 USD로 SQLite에 저장된다. 비-USD는 low로 플래그.
- [ ] 재실행 시 변동 없으면 "변동 없음", 가격 변경 시 `changes`에 `old→new` 기록.
- [ ] 비공개(Enterprise) 티어가 `price=null` + note로 저장.
- [ ] 대시보드 4개 화면 동작 + 업체 상세 가격 추이 차트 + [지금 수집] 버튼.
- [ ] `SCHEDULER_MODE=internal`이면 APScheduler가 주간 발화, `external`이면 발화하지 않음(OS가 담당).
- [ ] DB는 WAL 모드. Render에선 Persistent Disk 경로 SQLite가 재배포 후 유지.
- [ ] `docs/deploy_local.md`에 로컬 PC + Flask + OS 스케줄러 등록 절차가 있다.

---

## 16. 확정된 결정 사항 (Resolved)

1. **프레임워크**: **Flask로 통일**(Render·로컬 공통) → 향후 이전 시 프레임워크 재작성 불필요.
2. **이식성**: 3층 분리(core / config / web 어댑터) + Jinja2 공용 + 독립 CLI 진입점.
3. **배포**: 1단계 Render(상시·Persistent Disk·SCHEDULER_MODE=internal) → 2단계 로컬 PC(SCHEDULER_MODE=external + OS 스케줄러).
4. **데이터 이전**: SQLite 파일 복사. WAL 모드로 동시 접근 안전.
5. **통화**: US 리전 + en-US 로케일로 USD 강제. 로컬(한국 IP)은 로케일/URL 보강이 1차 방어선.
6. **출력**: 웹 대시보드(현황/업체상세/변동로그/수집상태).
7. **추출 모델**: `claude-sonnet` 기본.
