# One image, six roles: supervisor, finance, telegram-bot, and the three workers
# all run this image with a different command. Splitting them into six images
# would mean six builds of the same dependency tree and six places to patch.
FROM python:3.13-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is needed by setuptools for some deps; curl gives the healthcheck something
# to hit without installing a Python-side probe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI: the supervisor issues `docker compose` verbs to orchestrate the
# other services, so its container needs the client. The socket is mounted
# read-only at runtime. Version pinned; :cli is not an official tag.
COPY --from=docker:27.3.1-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

# Dependency layer first: a code-only change must not reinstall 60 packages.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.34" "psycopg[binary,pool]>=3.2" \
      "pydantic>=2.9" "httpx>=0.27" "apscheduler>=3.10" "PyYAML>=6.0" \
      "typer>=0.12" "rich>=13.7"

COPY engine ./engine
RUN pip install --no-cache-dir --no-deps -e .

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /logs \
 && chown -R appuser:appuser /app /logs

USER appuser

# ENGINE_RUNTIME=container is what permits this process to issue Docker verbs;
# a laptop checkout defaults to `local` and is refused (see db.assert_container_allowed).
ENV ENGINE_RUNTIME=container \
    ENGINE_LOG_LEVEL=INFO \
    PYTHONPATH=/app

EXPOSE 3100

ENTRYPOINT ["/usr/bin/tini", "--"]
# Default is the API, the one process that answers HTTP. docker-compose overrides
# command per service. Migrations are baked into each image and applied at startup
# under an advisory lock, so no container mounts the host source tree.
CMD ["engine", "api"]
