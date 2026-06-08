# Microsoft 공식 Playwright 이미지 — Chromium + OS 의존성이 미리 설치되어 있어
# Render 네이티브 빌드의 "--with-deps(root 필요)" 문제를 통째로 회피한다.
# 태그(v1.58.0)는 requirements.txt 의 playwright 버전과 맞춘다.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# pip 가 설치한 playwright 버전에 맞는 Chromium 리비전 보장
# (베이스 이미지에 이미 있으면 사실상 no-op)
RUN playwright install chromium

# 앱 소스
COPY . .

# Render 가 $PORT 를 주입한다. 런타임에 확장되도록 sh -c 사용.
EXPOSE 8000
CMD ["sh", "-c", "gunicorn app.web.flask_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120"]
