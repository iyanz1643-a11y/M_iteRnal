# 앱과 워커가 함께 쓰는 이미지 하나. 어디서든 띄울 수 있게 준비만 한다. 운영 배포는 D-14 와 대표님 승인 뒤에 한다.
#   docker build -t ga-app .
#   docker run --env-file .env -p 8000:8000 -v ga-data:/app/data ga-app                 # 웹
#   docker run --env-file .env -v ga-data:/app/data ga-app python -m app.worker          # 워커
# 비밀 값은 이미지에 넣지 않는다. 실행할 때 환경 변수로 준다. 이름은 .env.example 에 있다.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_DB=/app/data/app.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY config ./config
COPY schema ./schema
COPY scripts ./scripts
COPY samples ./samples
COPY templates ./templates
COPY run_demo.py pyproject.toml ./

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data \
 && chown -R app:app /app/data
USER app

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# 프록시 뒤에서는 FORWARDED_ALLOW_IPS 에 프록시 주소를 준다. 요청 횟수 제한이 진짜 IP 를 보게 하려는 것이다
CMD ["sh", "-c", "exec uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
