FROM python:3.12-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src src

RUN pip install --no-cache-dir --no-compile ".[hosted-service]" \
    && groupadd --system --gid 65532 attune \
    && useradd --system --uid 65532 --gid attune --home-dir /nonexistent attune

USER 65532:65532

ENV PORT=8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "20", "attune.hosted.google_chat_ingress_app:app"]
