import json
import os

from flask import Flask, Response, request
from neo4j import GraphDatabase
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from rich.console import Console

# ------------------------------------------------------------------------------
# Workflow:
# 1. Initialize Rich console for colored output.
# 2. Read environment variables (Neo4j URI, credentials, debug flag, port).
#    - If any variables are missing or malformed, fall back to sensible defaults.
# 3. Load metric definitions from metrics.json.
#    - If the file is missing or invalid, run without custom metrics.
# 4. Define Flask hooks:
#    a. before_request: log every incoming HTTP request.
#    b. catch_all: respond to any path other than /metrics.
# 5. On /metrics request:
#    a. Create a fresh Prometheus CollectorRegistry.
#    b. Attempt to connect to Neo4j and verify connectivity.
#       - If connection fails, set a "connection_status=0" gauge and return HTTP 500.
#       - If connection succeeds, set "connection_status=1".
#    c. For each metric definition:
#       i.   Validate required fields ("name", "help", "query", and either "value_field" or "value").
#       ii.  Extract optional "labels" list and "query_params" dict.
#       iii. Execute the Cypher query with parameters.
#       iv.  Convert each returned record into a Prometheus gauge sample.
#    d. Close the Neo4j driver and emit all collected metrics as plaintext.
# ------------------------------------------------------------------------------

# Initialize Rich console for formatted printing
console = Console()

# ----------------------------------------
# 2. Load environment variables with defaults
# ----------------------------------------
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

_debug_env = os.getenv("NEO4J_EXPORTER_DEBUG", "true")
DEBUG = _debug_env.lower() in ("true", "1", "yes")

_port_env_value = os.getenv("NEO4J_EXPORTER_PORT", "8000")
try:
    PORT = int(_port_env_value)
except ValueError:
    console.print(
        f"[ERROR] NEO4J_EXPORTER_PORT is invalid: '{_port_env_value}'. Defaulting to 8000.",
        style="red",
    )
    PORT = 8000


def debug_print(msg):
    """Print debug messages if DEBUG is true."""
    if DEBUG:
        console.print(f"[DEBUG] {msg}", style="dim")


# ----------------------------------------
# 3. Load metric definitions from metrics.json
# ----------------------------------------
CONFIG = {"metrics": []}
try:
    with open("metrics.json", "r") as f:
        CONFIG = json.load(f)
    if "metrics" not in CONFIG or not isinstance(CONFIG["metrics"], list):
        console.print(
            "[ERROR] 'metrics' key missing or not a list in metrics.json. No metrics loaded.",
            style="bold red",
        )
        CONFIG["metrics"] = []
except FileNotFoundError:
    console.print(
        "[ERROR] metrics.json not found. Exporter will run without custom metrics.",
        style="bold red",
    )
except json.JSONDecodeError:
    console.print(
        "[ERROR] metrics.json contains invalid JSON. No metrics loaded.",
        style="bold red",
    )

# ----------------------------------------
# 4. Initialize Flask app and hooks
# ----------------------------------------
app = Flask(__name__)


@app.before_request
def log_request():
    """
    Log every incoming HTTP request.
    This runs before any endpoint handler.
    """
    console.print(
        f"[INFO] HTTP {request.method} request to {request.path}", style="bold green"
    )


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def catch_all(path):
    """
    Handle any route other than /metrics.
    Respond with a simple message so the server doesn't return 404.
    """
    return "This exporter only serves /metrics\n", 200


@app.route("/metrics")
def metrics():
    """
    Serve Prometheus metrics on /metrics.
    """
    registry = CollectorRegistry()
    driver = None  # Ensure driver is defined for cleanup

    try:
        # a. Connect to Neo4j and verify connectivity
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
        debug_print(f"Connected to Neo4j at {NEO4J_URI} as '{NEO4J_USER}'.")

        # b. Expose a gauge indicating connection success
        connection_gauge = Gauge(
            "neo4j_exporter_connection_status",
            "Exporter connection state to Neo4j (1=ok, 0=failed)",
            registry=registry,
        )
        connection_gauge.set(1)

        # c. Iterate over each metric definition
        with driver.session() as session:
            for idx, metric_cfg in enumerate(CONFIG.get("metrics", [])):
                # i. Validate required keys
                try:
                    name = metric_cfg["name"]
                    help_text = metric_cfg["help"]
                    query = metric_cfg["query"]
                    # Accept either "value_field" or "value"
                    value_field = metric_cfg.get("value_field", metric_cfg.get("value"))
                    if value_field is None:
                        raise KeyError("No 'value_field' or 'value' provided")
                    labels = metric_cfg.get("labels", [])
                    # Optional parameters for the Cypher query
                    query_params = metric_cfg.get("query_params", {})
                    if not isinstance(query_params, dict):
                        debug_print(
                            f"Metric #{idx} ('{name}') 'query_params' is not a dict. Ignoring parameters: {query_params}"
                        )
                        query_params = {}
                except KeyError as e:
                    debug_print(
                        f"Metric #{idx} missing required key {e}. Skipping: {metric_cfg}"
                    )
                    continue

                try:
                    # ii. Create or register the gauge (with or without labels)
                    if labels:
                        gauge = Gauge(
                            name, help_text, labelnames=labels, registry=registry
                        )
                    else:
                        gauge = Gauge(name, help_text, registry=registry)

                    # iii. Run the Cypher query with any provided parameters
                    debug_print(
                        f"Running query for '{name}' with params: {query_params or 'None'}"
                    )
                    result = session.run(query, parameters=query_params)
                    rows = list(result)  # Fetch all records at once

                    if not rows:
                        debug_print(
                            f"No rows returned for metric '{name}'. Query: {query}, Params: {query_params}"
                        )
                        continue

                    # iv. Convert records into gauge samples
                    if labels:
                        for row_idx, record in enumerate(rows):
                            try:
                                label_values = [str(record[k]) for k in labels]
                                raw = record[value_field]
                                val = float(raw)
                                gauge.labels(*label_values).set(val)
                            except KeyError as e:
                                debug_print(
                                    f"Record #{row_idx} for metric '{name}' missing key {e}. "
                                    f"Record: {dict(record)}. Labels: {labels}, ValueField: '{value_field}'. Params: {query_params}"
                                )
                            except (TypeError, ValueError) as e:
                                debug_print(
                                    f"Cannot convert value for record #{row_idx} in metric '{name}': raw='{raw}', error={e!r}. "
                                    f"Record: {dict(record)}. Params: {query_params}"
                                )
                            except Exception as err:
                                debug_print(
                                    f"Unexpected error processing record #{row_idx} for metric '{name}': {err!r}. "
                                    f"Record: {dict(record)}. Params: {query_params}"
                                )
                    else:
                        if len(rows) > 1:
                            debug_print(
                                f"Unlabeled metric '{name}' returned {len(rows)} rows. Using only the first. Params: {query_params}"
                            )
                        record = rows[0]
                        try:
                            raw = record[value_field]
                            val = float(raw)
                            gauge.set(val)
                            debug_print(
                                f"Metric '{name}' set to {val}. Params: {query_params}"
                            )
                        except KeyError as e:
                            debug_print(
                                f"Missing key for value in '{name}': {e!r}. Expected '{value_field}'. "
                                f"Record: {dict(record)}. Params: {query_params}"
                            )
                        except (TypeError, ValueError) as e:
                            debug_print(
                                f"Cannot convert metric '{name}' raw='{raw}': {e!r}. Record: {dict(record)}. Params: {query_params}"
                            )
                        except Exception as err:
                            debug_print(
                                f"Unexpected error processing '{name}': {err!r}. Record: {dict(record)}. Params: {query_params}"
                            )

                except Exception as e:
                    metric_name = metric_cfg.get("name", f"# {idx}")
                    query_text = metric_cfg.get("query", "unknown")
                    params_text = metric_cfg.get("query_params", {})
                    debug_print(
                        f"Failed to process metric '{metric_name}'. Error: {e!r}. "
                        f"Query: {query_text}. Params: {params_text}"
                    )
                    continue

    except Exception as e:
        # If driver creation or connectivity check fails
        debug_print(f"Critical failure connecting to Neo4j: {e!r}")
        connection_gauge_fail = Gauge(
            "neo4j_exporter_connection_status",
            "Exporter connection state to Neo4j (1=ok, 0=failed)",
            registry=registry,
        )
        connection_gauge_fail.set(0)
        return Response(generate_latest(registry), mimetype="text/plain", status=500)

    finally:
        if driver:
            driver.close()
            debug_print("Neo4j driver closed.")

    # Return all collected metrics as plain text
    return Response(generate_latest(registry), mimetype="text/plain")


if __name__ == "__main__":
    if DEBUG:
        console.print(
            f"[INFO] Starting Flask server on 0.0.0.0:{PORT}", style="bold blue"
        )
        console.print(
            f"[INFO] Using NEO4J_URI='{NEO4J_URI}', NEO4J_USER='{NEO4J_USER}'",
            style="bold blue",
        )
        console.print("[INFO] Debug mode is ON.", style="bold blue")
    app.run(host="0.0.0.0", port=PORT, debug=False)
