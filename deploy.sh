#!/bin/bash
# deploy.sh — Configura el servidor VPS y arranca BookDork en producción
# Ejecutar como root en Ubuntu 24.04:  bash deploy.sh
set -euo pipefail

echo "========================================"
echo "  BookDork — Despliegue en producción"
echo "========================================"

# ── 1. Actualizar sistema ─────────────────────────────────────
apt-get update && apt-get upgrade -y

# ── 2. Instalar Docker ────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker
    echo "Docker instalado."
else
    echo "Docker ya instalado."
fi

# ── 3. Verificar .env ─────────────────────────────────────────
if [ ! -f .env ]; then
    echo ""
    echo "ERROR: Falta el archivo .env"
    echo "Crea uno basado en .env.production.example:"
    echo "  cp .env.production.example .env"
    echo "  nano .env   # rellena los valores"
    exit 1
fi

source .env

if [ -z "${APP_DOMAIN:-}" ] || [ "$APP_DOMAIN" = "TU_DOMINIO.COM" ]; then
    echo "ERROR: APP_DOMAIN no configurado en .env"
    exit 1
fi

# ── 4. Construir y arrancar ───────────────────────────────────
echo "Construyendo imágenes y arrancando servicios..."
docker compose -f docker-compose.prod.yml up -d --build

echo ""
echo "========================================"
echo "  Despliegue completado."
echo "  Accede en: https://${APP_DOMAIN}"
echo ""
echo "  Logs en tiempo real:"
echo "    docker compose -f docker-compose.prod.yml logs -f"
echo "========================================"
