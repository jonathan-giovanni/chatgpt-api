ARG INSTALL_SIP=false

FROM python:3.12-slim AS build
ARG INSTALL_SIP

ENV PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

COPY pyproject.toml README.md ./
COPY chatgpt_api ./chatgpt_api

RUN python -m pip install --upgrade pip \
    && if [ "$INSTALL_SIP" = "true" ]; then \
        python -m pip wheel --wheel-dir /wheels ".[sip]"; \
    else \
        python -m pip wheel --wheel-dir /wheels .; \
    fi

FROM python:3.12-slim AS runtime
ARG INSTALL_SIP

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CHAT_PROVIDER=chatgpt \
    CHATGPT_API_HOST=0.0.0.0 \
    CHATGPT_API_PORT=8000 \
    CHATGPT_ACCOUNT=free \
    CHATGPT_ACCOUNTS=free \
    CHATGPT_ACCOUNT_STRATEGY=failover \
    CHATGPT_ACCOUNTS_DIR=/data/secrets/accounts \
    CHATGPT_IMAGE_OUTPUT_DIR=/data/outputs/chatgpt-images \
    CHATGPT_RESEARCH_OUTPUT_DIR=/data/outputs/chatgpt-research \
    CHATGPT_ADMIN_DB_PATH=/data/outputs/chatgpt-admin.sqlite \
    CHATGPT_PUBLIC_BASE_URL=http://127.0.0.1:8000/v1 \
    CHATGPT_AGENT_MODE=optimized \
    CHATGPT_MODEL_FALLBACK=auto \
    CHATGPT_TEMPORARY_CHAT=true \
    CHATGPT_WEB_TIMEOUT=5400 \
    CHATGPT_CHAT_CONCURRENCY=free=1,go=2,plus=3,pro=4 \
    CHATGPT_UPLOAD_CONCURRENCY=free=1,go=1,plus=1,pro=1 \
    CHATGPT_IMAGE_CONCURRENCY=free=1,go=1,plus=2,pro=3 \
    CHATGPT_RESEARCH_CONCURRENCY=free=1,go=1,plus=2,pro=2

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY --from=build /wheels /wheels

RUN python -m pip install --upgrade pip \
    && if [ "$INSTALL_SIP" = "true" ]; then \
        python -m pip install --no-index --find-links=/wheels "chatgpt-api[sip]"; \
    else \
        python -m pip install --no-index --find-links=/wheels chatgpt-api; \
    fi \
    && rm -rf /wheels

RUN mkdir -p /data/secrets/accounts /data/outputs/chatgpt-images /data/outputs/chatgpt-research \
    && chown -R appuser:appuser /data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('CHATGPT_API_PORT','8000'); req=urllib.request.Request(f'http://127.0.0.1:{port}/health'); key=os.environ.get('CHATGPT_API_KEY'); req.add_header('Authorization', f'Bearer {key}') if key else None; urllib.request.urlopen(req, timeout=4).read()"

CMD ["chatgpt-api", "server", "start"]
