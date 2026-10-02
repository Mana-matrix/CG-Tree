# ctx service: the CTX1 index of GitHub repos over HTTP (see service/app.py)
ARG BASE=python:3.12-slim
FROM ${BASE}

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt
COPY ctx.py SPEC.md ./
COPY service/__init__.py service/app.py service/repos.py service/

RUN useradd --system --uid 10001 --home /data ctx && mkdir -p /data && chown ctx /data \
 && git config --system --add safe.directory '*'
USER ctx
ENV CTX_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
# one worker: repo locks live in the process
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
