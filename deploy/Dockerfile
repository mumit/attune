# The attune assistant image (roadmap prompt 10).
#
# Build context is the repository root:
#   docker build -f deploy/Dockerfile .
# or let deploy/compose.yml do it: docker compose --profile assistant up.
#
# Installs the app extras only ([orchestrator,memory,google,slack]); the
# republisher keeps its own image (deploy/republisher/Dockerfile) and its own
# dependency set, per the standalone-deployable convention in CLAUDE.md.
#
# No secrets in this image (rule 6): configuration arrives via env_file at
# run time. All mutable state lives under /data (a volume).

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src src

RUN pip install --no-cache-dir \
       ".[orchestrator,memory,google,slack,mcp]"

ENV ATTUNE_DATA_DIR=/data
VOLUME /data

CMD ["python", "-m", "attune"]
