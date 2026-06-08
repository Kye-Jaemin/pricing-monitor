# 1단계 배포 — Render (상시 Web Service)

US 리전 + Persistent Disk + 내부 APScheduler 구성.

## 1. 사전 준비
- Render 계정, 이 저장소를 GitHub 에 push.
- `ANTHROPIC_API_KEY` 발급.

## 2. Blueprint 로 생성
1. Render 대시보드 → **New → Blueprint** → 이 저장소 선택.
2. `render.yaml` 이 자동 인식된다(서비스 1개: `pricing-monitor`).
3. **환경변수**에서 `ANTHROPIC_API_KEY` 를 입력(`sync: false` 라 직접 넣어야 함).
4. **Create** → 빌드 시작.

## 3. 빌드가 하는 일 (`render.yaml`)
```
pip install -r requirements.txt
playwright install --with-deps chromium     # Chromium + OS 의존성
```
- 메모리 1GB+ 권장(Playwright Chromium). `starter` 이상 플랜 사용.

## 4. 핵심 설정 확인
| 항목 | 값 | 이유 |
|---|---|---|
| `region` | `oregon` (US) | IP 기반으로 USD 페이지 수신 |
| `DB_PATH` | `/var/data/pricing.db` | **Persistent Disk 경로 필수** |
| `disk.mountPath` | `/var/data` | SQLite 영속화 |
| `SCHEDULER_MODE` | `internal` | 웹 프로세스 내 APScheduler 주간 발화 |

> ⚠️ Render 기본 파일시스템은 휘발성. SQLite 는 반드시 Persistent Disk(`/var/data`) 에 둔다.
> 무료 인스턴스는 디스크 불가 + 15분 후 잠들어 스케줄러가 멈춘다 → **유료 상시 인스턴스 전제**.

## 5. 동작 확인
- `https://<your-app>.onrender.com/` 현황 화면.
- `https://<your-app>.onrender.com/healthz` → `{"status":"ok","scheduler_mode":"internal"}`.
- `/runs` 에서 **[지금 수집]** 으로 즉시 1회 수집 테스트.

## 6. 스케줄
- `SCHEDULE_DAY_OF_WEEK=mon`, `SCHEDULE_HOUR=9`, `SCHEDULE_TIMEZONE=America/New_York`
  → 매주 월요일 09:00 ET 자동 수집.
- 재배포로 놓친 주간 실행은 `coalesce=True` + `misfire_grace_time` 으로 다음 기동 시 보충.

## 7. 로컬로 이전할 때
→ [deploy_local.md](deploy_local.md) 참고. `pricing.db` 복사 + `.env` 만 바꾸면 됨(코드 변경 없음).
