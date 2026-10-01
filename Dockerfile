# Dockerfile for the FastAPI backend.
#
# Hosts: Fly.io primarily. Should also work on Render/Railway/any Docker host.
# Build: docker build -t auction-sniper .
# Run:   docker run -p 8080:8080 \
#          -e DATABASE_URL="..." \
#          -e GODADDY_API_KEY="..." \
#          ...
#          auction-sniper

FROM python:3.11-slim

# System deps:
#   gcc + libpq-dev so psycopg2-binary builds (alembic migrations need sync driver).
#   curl for container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
        wamerican \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so they cache when only app code changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        fastapi>=0.110 \
        "uvicorn[standard]>=0.27" \
        pydantic>=2.6 \
        pydantic-settings>=2.2 \
        "sqlalchemy[asyncio]>=2.0" \
        asyncpg>=0.29 \
        psycopg2-binary>=2.9 \
        alembic>=1.13 \
        httpx>=0.27 \
        redis>=5.0 \
        apscheduler>=3.10 \
        lxml>=5.1 \
        metaphone>=0.6 \
        ijson>=3.2

# Copy the rest of the app.
COPY app ./app
COPY worker ./worker
COPY alembic ./alembic
COPY alembic.ini ./

# Fly defaults to 8080 internally.
EXPOSE 8080

# On startup: run any pending alembic migrations, then launch uvicorn.
# `exec` so uvicorn becomes PID 1 and receives Fly's SIGTERM on deploy.
# 2026-08-20: `&&` -> `;` — a failed migration (e.g. the Neon quota outage
# that took the whole app down, including the snipe worker) must NOT
# prevent the app from booting. Degraded-with-index beats dead; the app
# logs DB errors loudly and /health stays up so Fly doesn't kill-loop it.
CMD ["sh", "-c", "alembic upgrade head || echo 'WARNING: migration failed - starting anyway'; exec uvicorn app.main:app --host 0.0.0.0 --port 8080"]
