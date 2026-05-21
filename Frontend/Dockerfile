# =============================================================================
# Dockerfile — Imagen del backend BookDork
# =============================================================================
# Imagen multi-stage para minimizar el tamaño final:
#   Stage 1 (builder): instala dependencias Python
#   Stage 2 (runtime): solo el código y las librerías necesarias
# =============================================================================

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Evitar preguntas interactivas
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build

# Instalar dependencias de compilación (para aioquic y cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Usuario no-root para mayor seguridad
RUN groupadd -r bookdork && useradd -r -g bookdork bookdork

WORKDIR /app

# Copiar librerías instaladas desde el builder
COPY --from=builder /install /usr/local

# Copiar código de la aplicación
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Directorio para certificados TLS (montado como volumen)
RUN mkdir -p /app/certs && chown -R bookdork:bookdork /app

# Variables de entorno de producción por defecto
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HTTP3_PORT=8443
ENV HOST=0.0.0.0

# Cambiar a usuario no-root
USER bookdork

# Exponer puertos TCP y UDP (HTTP/3 requiere UDP)
EXPOSE 8443/tcp
EXPOSE 8443/udp

# Comando de arranque con Hypercorn (HTTP/3)
CMD ["python", "-m", "backend.main"]
