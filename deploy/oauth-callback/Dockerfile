FROM python:3.12-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28

WORKDIR /app

COPY deploy/oauth-callback/requirements.lock ./
RUN pip install --no-cache-dir --no-compile --require-hashes \
    -r requirements.lock \
    && groupadd --system --gid 65532 attune \
    && useradd --system --uid 65532 --gid attune --home-dir /nonexistent attune

COPY src/attune/hosted/oauth_callback_service.py ./
COPY src/attune/hosted/oauth_exchange_client.py ./
COPY deploy/oauth-callback/app.py ./

USER 65532:65532

ENV PORT=8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "10", "--access-logfile", "/dev/null", "app:app"]
