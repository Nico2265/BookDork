# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN groupadd -r bookdork && useradd -r -g bookdork bookdork

WORKDIR /app

COPY --from=builder /install /usr/local
COPY ["backend/", "./backend/"]
# IMPORTANTE: 'Frontend' con F mayúscula — el código busca parent.parent/"Frontend"
# (main.py). En Linux (case-sensitive) una carpeta 'frontend' minúscula NO se
# encontraría y el frontend devolvería 404.
COPY Frontend/ ./Frontend/

RUN chown -R bookdork:bookdork /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HTTP3_PORT=8000
ENV HOST=0.0.0.0

USER bookdork

EXPOSE 8000

CMD ["python", "-m", "backend.main"]
