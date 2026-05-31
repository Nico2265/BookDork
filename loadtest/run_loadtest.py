"""
run_loadtest.py — Prueba de carga asíncrona contra el backend BookDork.

Filosofía:
  * Usa httpx + asyncio (sin dependencias extra).
  * Cada "virtual user" (VU) es un coroutine que envía requests en bucle
    durante una ventana de tiempo fija; al terminar la ventana, todos los VUs
    se cancelan limpiamente y se computan métricas.
  * Workload mixto ponderado: páginas HTML + assets estáticos. Refleja la carga
    real de un navegador abriendo el site.
  * Stages incrementales de concurrencia. Para detectar el punto de saturación.

Métricas reportadas por stage:
  * req/s sostenido
  * latencia p50/p95/p99/max (sólo de respuestas, no de errores de conexión)
  * conteo de 2xx / 3xx / 4xx / 5xx / errores de conexión / timeouts

Salida: JSON estructurado en stdout + tabla legible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


# ── Workload ─────────────────────────────────────────────────────────────────
# Mezcla representativa de un usuario abriendo páginas del site.
# Pesos relativos: un usuario que abre /search tira HTML + ~6 assets.
DEFAULT_WORKLOAD = [
    ("/",                     1.0),   # landing
    ("/search",               2.0),   # buscador (página HTML)
    ("/vault",                1.0),   # biblioteca
    ("/converter",            0.5),   # convertidor
    ("/api/health",           3.0),   # health endpoint (ligero, alta frecuencia)
    # Assets estáticos — el server los pasa por SafeStaticFiles
    # Sin hash: probamos el fallback al fuente. Con hash: probamos dist/.
    ("/static/styles.css",    2.0),   # podría 404 si está hasheado, eso también es señal
    ("/static/logo.png",      0.5),
]


@dataclass
class StageResult:
    name: str
    concurrency: int
    duration_s: float
    total_requests: int
    by_status: dict = field(default_factory=dict)
    connection_errors: int = 0
    timeouts: int = 0
    latencies_ms: list = field(default_factory=list)

    @property
    def rps(self) -> float:
        return self.total_requests / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        errs = self.connection_errors + self.timeouts + sum(
            v for k, v in self.by_status.items() if k >= 500
        )
        return errs / self.total_requests

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        # statistics.quantiles requiere n>=2; para p100 → max
        if p >= 100:
            return max(self.latencies_ms)
        sorted_lat = sorted(self.latencies_ms)
        k = (p / 100.0) * (len(sorted_lat) - 1)
        f = int(k)
        c = min(f + 1, len(sorted_lat) - 1)
        if f == c:
            return sorted_lat[f]
        return sorted_lat[f] + (sorted_lat[c] - sorted_lat[f]) * (k - f)

    def to_dict(self) -> dict:
        return {
            "stage": self.name,
            "concurrency": self.concurrency,
            "duration_s": round(self.duration_s, 2),
            "total_requests": self.total_requests,
            "rps": round(self.rps, 1),
            "error_rate_pct": round(self.error_rate * 100, 2),
            "by_status": {str(k): v for k, v in sorted(self.by_status.items())},
            "connection_errors": self.connection_errors,
            "timeouts": self.timeouts,
            "p50_ms": round(self.percentile(50), 1),
            "p95_ms": round(self.percentile(95), 1),
            "p99_ms": round(self.percentile(99), 1),
            "max_ms": round(self.percentile(100), 1),
        }


def pick_path(workload: list[tuple[str, float]], rng: random.Random) -> str:
    paths, weights = zip(*workload)
    return rng.choices(paths, weights=weights, k=1)[0]


async def virtual_user(
    client: httpx.AsyncClient,
    base_url: str,
    workload: list[tuple[str, float]],
    stop_at: float,
    result: StageResult,
    rng: random.Random,
) -> None:
    """Un VU envía requests en bucle hasta que se alcanza stop_at (epoch)."""
    while True:
        if time.monotonic() >= stop_at:
            return
        path = pick_path(workload, rng)
        url = base_url + path
        t0 = time.perf_counter()
        try:
            r = await client.get(url, timeout=10.0)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            result.latencies_ms.append(dt_ms)
            result.by_status[r.status_code] = result.by_status.get(r.status_code, 0) + 1
            result.total_requests += 1
        except httpx.TimeoutException:
            result.timeouts += 1
            result.total_requests += 1
        except (httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.ReadError, httpx.PoolTimeout, OSError):
            result.connection_errors += 1
            result.total_requests += 1
        except Exception:
            # Cualquier otro error de cliente cuenta como error de conexión.
            result.connection_errors += 1
            result.total_requests += 1


async def run_stage(
    base_url: str,
    workload: list[tuple[str, float]],
    concurrency: int,
    duration_s: float,
    seed: int,
) -> StageResult:
    """Lanza `concurrency` VUs durante `duration_s` segundos."""
    name = f"VU={concurrency}"
    result = StageResult(name=name, concurrency=concurrency, duration_s=0.0,
                         total_requests=0)
    rng = random.Random(seed)

    # Connection pool generoso: queremos saturar al server, no al cliente.
    limits = httpx.Limits(
        max_connections=concurrency * 2 + 32,
        max_keepalive_connections=concurrency + 16,
    )
    transport = httpx.AsyncHTTPTransport(retries=0)

    t_start = time.monotonic()
    stop_at = t_start + duration_s

    async with httpx.AsyncClient(
        limits=limits,
        transport=transport,
        http2=False,
        follow_redirects=False,
        headers={"User-Agent": "BookDork-LoadTest/1.0"},
    ) as client:
        tasks = [
            asyncio.create_task(
                virtual_user(client, base_url, workload, stop_at, result, rng)
            )
            for _ in range(concurrency)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_s = time.monotonic() - t_start
    return result


def print_header(scenario: str, base_url: str) -> None:
    print(f"\n{'=' * 78}")
    print(f" LOAD TEST  ·  {scenario}")
    print(f" target: {base_url}")
    print(f"{'=' * 78}")
    print(f"{'Stage':<10}{'RPS':>10}{'p50 ms':>10}{'p95 ms':>10}"
          f"{'p99 ms':>10}{'err%':>8}{'2xx':>8}{'4xx':>8}{'5xx':>8}{'conn':>8}")
    print("-" * 78)


def print_row(r: StageResult) -> None:
    by = r.by_status
    twoxx = sum(v for k, v in by.items() if 200 <= k < 300)
    fourxx = sum(v for k, v in by.items() if 400 <= k < 500)
    fivexx = sum(v for k, v in by.items() if 500 <= k < 600)
    print(f"{r.name:<10}{r.rps:>10.1f}{r.percentile(50):>10.1f}"
          f"{r.percentile(95):>10.1f}{r.percentile(99):>10.1f}"
          f"{r.error_rate * 100:>7.1f}%{twoxx:>8}{fourxx:>8}{fivexx:>8}"
          f"{r.connection_errors + r.timeouts:>8}")


async def warmup(base_url: str, n: int = 50) -> None:
    """Solicitudes preliminares para que JIT/cachés se asienten."""
    async with httpx.AsyncClient(timeout=5.0) as c:
        for _ in range(n):
            try:
                await c.get(base_url + "/api/health")
            except Exception:
                break


async def main_async(args) -> None:
    base_url = args.url.rstrip("/")
    scenario = args.scenario

    # Verificación de conectividad antes de empezar
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(base_url + "/api/health")
            print(f"[probe] /api/health -> HTTP {r.status_code} "
                  f"({r.elapsed.total_seconds() * 1000:.1f} ms)")
    except Exception as e:
        print(f"[probe] FAIL: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"[warmup] {50} requests…")
    await warmup(base_url, 50)

    stages = [int(x) for x in args.stages.split(",")]
    duration_s = args.duration

    print_header(scenario, base_url)

    all_results: list[StageResult] = []
    for i, vu in enumerate(stages):
        # Pequeño cooldown entre stages para liberar sockets en TIME_WAIT.
        if i > 0:
            await asyncio.sleep(2.0)
        r = await run_stage(base_url, DEFAULT_WORKLOAD, vu, duration_s, seed=42 + i)
        all_results.append(r)
        print_row(r)

    # Salida JSON para post-proceso
    summary = {
        "scenario": scenario,
        "target": base_url,
        "duration_per_stage_s": duration_s,
        "workload": DEFAULT_WORKLOAD,
        "stages": [r.to_dict() for r in all_results],
    }
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"\n[output] resultados JSON -> {args.output}")


def main() -> None:
    p = argparse.ArgumentParser(description="Load test BookDork.")
    p.add_argument("--url", default="http://127.0.0.1:8000",
                   help="URL base del backend.")
    p.add_argument("--scenario", default="default",
                   help="Etiqueta para el reporte (ej: 'dev', 'prod-like').")
    p.add_argument("--stages", default="10,50,100,200,500,1000",
                   help="Lista de niveles de concurrencia separados por coma.")
    p.add_argument("--duration", type=float, default=15.0,
                   help="Segundos por stage.")
    p.add_argument("--output", default=None, help="Ruta para guardar JSON.")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
