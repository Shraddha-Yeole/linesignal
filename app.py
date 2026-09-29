from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Load local development secrets before connector modules read environment.
def load_dotenv_file() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv_file()

from connectors import (  # noqa: E402
    DemoTelemetrySource,
    analyze,
    build_timeline,
    draft_handoff,
    get_topology,
    integration_status,
    score_evidence,
    verify_recovery,
)

ROOT = Path(__file__).parent
DIST = (ROOT / "web" / "dist").resolve()
SCENARIO_ORDER = ("wireless_degradation", "robot_servo_fault", "stale_telemetry")
SCENARIOS = {path.stem: json.loads(path.read_text()) for path in sorted((ROOT / "scenarios").glob("*.json"))}
SCENARIO_IDS = [sid for sid in SCENARIO_ORDER if sid in SCENARIOS] + sorted(set(SCENARIOS) - set(SCENARIO_ORDER))
DEMO_TOPOLOGY = json.loads((ROOT / "topology.json").read_text())
MAX_BODY_BYTES = 4096


def public_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Post-action measurements must never leak into the pre-action view."""
    return {k: v for k, v in scenario.items() if k != "after"}


def names_for(path: list[str], nodes: list[dict[str, str]]) -> list[str]:
    by_id = {node["id"]: node["name"] for node in nodes}
    return [by_id.get(node_id, node_id) for node_id in path]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content_type: str, body: bytes, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode())

    def _scenario_from(self, scenario_id: Any) -> dict[str, Any] | None:
        if not isinstance(scenario_id, str) or scenario_id not in SCENARIOS:
            self._json({"error": "unknown scenario"}, 400)
            return None
        return SCENARIOS[scenario_id]

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json({"error": "invalid request body"}, 400)
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, 400)
            return None
        if not isinstance(body, dict):
            self._json({"error": "invalid JSON"}, 400)
            return None
        return body

    def _serve_static(self, request_path: str) -> None:
        if not DIST.exists():
            self._send(503, "text/plain; charset=utf-8",
                       b"Front end not built. Run: cd web && npm install && npm run build  (or npm run dev for development)")
            return
        relative = request_path.lstrip("/") or "index.html"
        candidate = (DIST / relative).resolve()
        if not candidate.is_relative_to(DIST) or not candidate.is_file():
            candidate = DIST / "index.html"
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
            content_type += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if "/assets/" in candidate.as_posix() else "no-store"
        self._send(200, content_type, candidate.read_bytes(), cache)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/api/scenarios":
            self._json([{k: SCENARIOS[sid][k] for k in ("id", "name", "description", "expected_domain", "incident_id")} for sid in SCENARIO_IDS])
            return
        if url.path == "/api/integrations":
            self._json(integration_status())
            return
        if url.path == "/api/scenario":
            scenario = self._scenario_from((query.get("id") or [SCENARIO_IDS[0]])[0])
            if scenario is None:
                return
            incident = DemoTelemetrySource(scenario).get_incident()
            topology = get_topology(incident, DEMO_TOPOLOGY)
            self._json({"scenario": public_scenario(incident), "timeline": build_timeline(incident), "topology": topology})
            return
        if url.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        self._serve_static(url.path)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path not in ("/api/analyze", "/api/verify", "/api/report"):
            self._json({"error": "not found"}, 404)
            return
        body = self._read_body()
        if body is None:
            return
        scenario = self._scenario_from(body.get("scenario_id"))
        if scenario is None:
            return

        if url.path == "/api/verify":
            self._json({
                "recovery": verify_recovery(scenario),
                "action": scenario["simulated_operator_action"],
                "after": scenario.get("after", []),
                "action_at": scenario["action_at"],
                "recovery_access_point": scenario.get("recovery_access_point"),
            })
            return

        if url.path == "/api/report":
            self._json(draft_handoff(scenario, score_evidence(scenario), verify_recovery(scenario)))
            return

        trace = []
        start = time.perf_counter()
        incident = DemoTelemetrySource(scenario).get_incident()
        trace.append({"id": "telemetry", "label": "Telemetry replay", "partner": None, "source": "demo-replay", "status": "ok",
                      "ms": int((time.perf_counter() - start) * 1000),
                      "detail": f"{len(incident['before'])} pre-action measurements for {incident['robot']}."})

        topology = get_topology(incident, DEMO_TOPOLOGY)
        path_names = names_for(topology["path"], topology["nodes"])
        trace.append({"id": "graph", "label": "Dependency graph", "partner": "Neo4j", "source": topology["source"],
                      "status": topology["status"], "ms": topology["ms"], "detail": topology["detail"]})

        start = time.perf_counter()
        measured = score_evidence(incident)
        top = max(measured["scores"], key=measured["scores"].get)
        trace.append({"id": "scoring", "label": "Evidence scoring", "partner": None, "source": "deterministic", "status": "ok",
                      "ms": int((time.perf_counter() - start) * 1000),
                      "detail": f"Strongest measured signal: {top} ({measured['scores'][top]:.2f})."})

        analysis = analyze(incident, path_names, measured)
        trace.append({"id": "inference", "label": "Diagnosis", "partner": "Crusoe", "source": analysis["source"],
                      "status": analysis["status"], "ms": analysis["ms"], "detail": analysis["detail"]})
        guardrails = analysis["guardrails"]
        if guardrails:
            passed = sum(1 for g in guardrails if g["passed"])
            trace.append({"id": "guardrails", "label": "Guardrail validation", "partner": None, "source": "deterministic",
                          "status": "ok" if passed == len(guardrails) else "rejected", "ms": 0,
                          "detail": f"{passed}/{len(guardrails)} checks passed."})

        self._json({
            "incident": {k: incident[k] for k in ("incident_id", "title", "site", "line", "robot", "summary")},
            "dependency_path": path_names,
            "analysis": analysis,
            "recovery": {"status": "pending", "summary": "Recovery has not been checked. Approve the action to load post-action measurements.",
                         "checks": [], "evidence": []},
            "trace": trace,
        })

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    print(f"LineSignal API is running at http://{host}:{port}", flush=True)
    threading.Thread(target=get_topology, args=(SCENARIOS[SCENARIO_IDS[0]], DEMO_TOPOLOGY), daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
