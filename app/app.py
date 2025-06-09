#!/usr/bin/env python3
"""
neo4j_exporter_ce.py – Prometheus exporter implemented with FastAPI.

CLI flags always override environment variables.

Example:

    python neo4j_exporter_fastapi.py \
        --neo4j-uri bolt://db:7687 \
        --neo4j-user neo4j \
        --neo4j-password secret \
        --port 9100 \
        --debug
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

import fastapi
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from neo4j import Driver, GraphDatabase
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from rich.console import Console
from starlette.concurrency import run_in_threadpool

# --------------------------------------------------------------------------- #
# 1. Command-line flags (highest priority)                                    #
# --------------------------------------------------------------------------- #


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter that scrapes metrics from Neo4j."
    )
    parser.add_argument("--neo4j-uri", help="Neo4j bolt URI")
    parser.add_argument("--neo4j-user", help="Neo4j username")
    parser.add_argument("--neo4j-password", help="Neo4j password")
    parser.add_argument("--port", type=int, help="Exporter listen port")
    parser.add_argument("--debug", action="store_true", help="Enable debug logs")
    return parser.parse_args()  # abort on unknown flags


_cli = _parse_cli_args()

# --------------------------------------------------------------------------- #
# 2. Configuration (CLI ▶ ENV ▶ default)                                      #
# --------------------------------------------------------------------------- #

console = Console()

NEO4J_URI: str = _cli.neo4j_uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER: str = _cli.neo4j_user or os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD: str = _cli.neo4j_password or os.getenv("NEO4J_PASSWORD", "password")


def _resolve_port() -> int:
    if _cli.port is not None:  # argparse already ensured int
        return _cli.port
    port_str = os.getenv("NEO4J_EXPORTER_PORT", "8000")
    try:
        return int(port_str)
    except ValueError:
        console.print(
            f"[WARN] Invalid NEO4J_EXPORTER_PORT='{port_str}'. Defaulting to 8000.",
            style="yellow",
        )
        return 8000


PORT: int = _resolve_port()

DEBUG: bool = (
    _cli.debug
    if _cli.debug is not None
    else os.getenv("NEO4J_EXPORTER_DEBUG", "false").lower() in {"1", "true", "yes"}
)

# --------------------------------------------------------------------------- #
# 3. Utilities                                                                #
# --------------------------------------------------------------------------- #


def debug(msg: str) -> None:
    if DEBUG:
        console.print(f"[DEBUG] {msg}", style="dim")


# --------------------------------------------------------------------------- #
# 4. Load metric definitions (optional)                                       #
# --------------------------------------------------------------------------- #

CONFIG: Dict[str, Any] = {"metrics": []}
try:
    with open("metrics.json", "r", encoding="utf-8") as fh:
        CONFIG = json.load(fh)
    if not isinstance(CONFIG.get("metrics"), list):
        raise ValueError("'metrics' key must be a list")
except FileNotFoundError:
    console.print(
        "[WARN] metrics.json not found – exporter will expose only built-in metrics.",
        style="yellow",
    )
except (json.JSONDecodeError, ValueError) as err:
    console.print(
        f"[WARN] metrics.json is invalid ({err}) – exporter will expose only built-in metrics.",
        style="yellow",
    )
    CONFIG["metrics"] = []

# --------------------------------------------------------------------------- #
# 5. Neo4j driver (single instance, thread-safe sessions per request)         #
# --------------------------------------------------------------------------- #

try:
    driver: Driver | None = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
    )
    driver.verify_connectivity()
    debug(f"Connected to Neo4j at {NEO4J_URI} as '{NEO4J_USER}'")
except Exception as exc:
    console.print(
        f"[ERROR] Failed to initialise Neo4j driver: {exc!r}", style="bold red"
    )
    driver = None  # health endpoint will report Unhealthy

# --------------------------------------------------------------------------- #
# 6. FastAPI application                                                      #
# --------------------------------------------------------------------------- #

app = fastapi.FastAPI()


@app.on_event("shutdown")
def _close_driver() -> None:
    if driver:
        driver.close()
        debug("Neo4j driver closed on shutdown")


@app.middleware("http")
async def log_requests(request: fastapi.Request, call_next):
    console.print(
        f"[INFO] HTTP {request.method} {request.url.path}", style="bold green"
    )
    return await call_next(request)


# --------------------------------------------------------------------------- #
# 7. /metrics                                                                 #
# --------------------------------------------------------------------------- #


@app.get("/metrics")
def metrics() -> Response:
    """Collect metrics from Neo4j and expose them to Prometheus."""
    registry = CollectorRegistry()

    conn_gauge = Gauge(
        "neo4j_exporter_connection_status",
        "Connection status to Neo4j (1 = OK, 0 = Error)",
        registry=registry,
    )
    scrape_error_gauge = Gauge(
        "neo4j_exporter_metric_errors",
        "Number of metric processing errors in this scrape",
        registry=registry,
    )
    scrape_errors = 0

    # Connectivity check
    try:
        if driver is None:
            raise RuntimeError("Driver not initialised")
        driver.verify_connectivity()
        conn_gauge.set(1)
    except Exception as fatal:
        conn_gauge.set(0)
        debug(f"Connectivity check failed: {fatal!r}")
        return Response(
            content=generate_latest(registry),
            media_type="text/plain",
            status_code=500,
        )

    # Collect configured metrics
    with driver.session() as session:
        for idx, m in enumerate(CONFIG.get("metrics", [])):
            metric_name_for_log = m.get("name", f"Unnamed metric #{idx}")
            try:
                name = m["name"]
                help_text = m["help"]
                query = m["query"]
                value_field = m.get("value_field", m.get("value"))
                if value_field is None:
                    raise KeyError("value_field/value missing")
                labels: List[str] = m.get("labels", [])
                params: Dict[str, Any] = m.get("query_params", {})
                if not isinstance(params, dict):
                    debug(f"Metric '{name}' has non-dict 'query_params'. Ignoring.")
                    params = {}
            except KeyError as miss:
                debug(
                    f"Metric '{metric_name_for_log}' skipped – bad definition: {miss}"
                )
                scrape_errors += 1
                continue

            gauge = Gauge(name, help_text, labelnames=labels, registry=registry)
            debug(f"Running '{name}' – params={params or '∅'}")
            try:
                rows = list(session.run(query, parameters=params))
            except Exception as qerr:
                debug(f"Query failed for '{name}': {qerr!r}")
                scrape_errors += 1
                continue

            if not rows:
                debug(f"'{name}' returned zero rows")
                continue

            if labels:
                for row_idx, row in enumerate(rows):
                    try:
                        gauge.labels(*[str(row[l]) for l in labels]).set(
                            float(row[value_field])
                        )
                    except (KeyError, TypeError, ValueError) as row_conv_err:
                        debug(
                            f"Skipping row #{row_idx} for metric '{name}' "
                            f"due to conversion error: {row_conv_err!r}"
                        )
                        scrape_errors += 1
            else:
                try:
                    gauge.set(float(rows[0][value_field]))
                except (KeyError, TypeError, ValueError) as conv_err:
                    debug(
                        f"Skipping metric '{name}' due to conversion error: {conv_err!r}"
                    )
                    scrape_errors += 1

    scrape_error_gauge.set(scrape_errors)

    return Response(
        content=generate_latest(registry),
        media_type="text/plain",
        status_code=200,
    )


# --------------------------------------------------------------------------- #
# 8. /healthz                                                                 #
# --------------------------------------------------------------------------- #

router = fastapi.APIRouter()


@router.get("/healthz")
async def healthcheck() -> JSONResponse:
    """Return 200 if the exporter can reach Neo4j."""
    try:
        if driver is None:
            raise RuntimeError("Driver not initialised")
        await run_in_threadpool(driver.verify_connectivity)
        return JSONResponse(content={"status": "Healthy"})
    except Exception as exc:
        debug(f"Health check failed: {exc!r}")
        return JSONResponse(content={"status": "Unhealthy"}, status_code=500)


app.include_router(router)

# --------------------------------------------------------------------------- #
# 9. Catch-all                                                                #
# --------------------------------------------------------------------------- #


@app.get("/{_path:path}")
def everything_else() -> PlainTextResponse:
    return PlainTextResponse(
        "This exporter only serves /metrics and /healthz\n", status_code=200
    )


# --------------------------------------------------------------------------- #
# 10. Script entry-point (optional)                                           #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn

    if DEBUG:
        console.print(f"[INFO] Starting FastAPI on 0.0.0.0:{PORT}", style="bold blue")
        console.print(
            f"[INFO] NEO4J_URI='{NEO4J_URI}', USER='{NEO4J_USER}'", style="bold blue"
        )

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        log_level="debug" if DEBUG else "info",
    )
