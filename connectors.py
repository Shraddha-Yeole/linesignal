"""Partner integrations and incident logic for the LineSignal hackathon demo."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from typing import Any, Optional

CRUSOE_URL = "https://api.inference.crusoecloud.com/v1/chat/completions"
DEFAULT_CRUSOE_MODEL = "openai/gpt-oss-120b"

DOMAINS = ("network", "robot", "data", "unknown")
CONFIDENCE_LEVELS = ("high", "medium", "low")
ALLOWED_ACTIONS = {
    "escalate_network": "Route to the network owner: inspect the robot's access point and move its traffic to a healthy access path.",
    "dispatch_maintenance": "Open a maintenance ticket for the robot and inspect the reported fault before restoring full line speed.",
    "restore_telemetry": "Restore the telemetry collector first; the current data cannot support a diagnosis.",
    "monitor": "Keep monitoring; the evidence does not support a corrective action.",
}
DOMAIN_ACTION = {"network": "escalate_network", "robot": "dispatch_maintenance", "data": "restore_telemetry", "unknown": "monitor"}

STALE_AFTER_S = 60
TARGET_LATENCY_MS = 80
TARGET_LOSS_PCT = 1
TARGET_JITTER_MS = 30
TARGET_HEARTBEAT_S = 5
TARGET_THROUGHPUT_PCT = 95

PATH_RELATIONSHIPS = ("CONNECTS_THROUGH", "UPLINKS_TO", "REACHES")
TOPOLOGY_RELATIONSHIPS = ("CONNECTS_THROUGH", "CAN_ROAM_TO", "UPLINKS_TO", "REACHES", "DISPATCHES_TO", "CONTROLS")


class DemoTelemetrySource:
    """Replay fixture source. Replaceable by another TelemetrySource later."""

    def __init__(self, scenario: dict[str, Any]):
        self.scenario = scenario

    def get_incident(self) -> dict[str, Any]:
        return self.scenario


class ThousandEyesTelemetrySource:
    """Future adapter seam; intentionally not used by the hackathon MVP."""

    def get_incident(self) -> dict[str, Any]:
        raise NotImplementedError(
            "Map ThousandEyes test results to the normalized telemetry event schema."
        )


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _clock(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%H:%M")


def integration_status() -> dict[str, Any]:
    """Report which partners are configured without exposing any credential values."""
    return {
        "crusoe": {"configured": bool(os.getenv("CRUSOE_API_KEY")), "model": os.getenv("CRUSOE_MODEL", DEFAULT_CRUSOE_MODEL)},
        "neo4j": {"configured": all(os.getenv(k) for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"))},
    }


# ---------------------------------------------------------------------------
# Neo4j dependency graph
# ---------------------------------------------------------------------------

def _demo_path(topology: dict[str, Any], robot_id: str) -> list[str]:
    kinds = {node["id"]: node["kind"] for node in topology["nodes"]}
    adjacency: dict[str, list[str]] = {}
    for edge in topology["edges"]:
        if edge["rel"] in PATH_RELATIONSHIPS:
            adjacency.setdefault(edge["from"], []).append(edge["to"])
    queue = deque([[robot_id]])
    seen = {robot_id}
    while queue:
        path = queue.popleft()
        if kinds.get(path[-1]) == "Service":
            line = next((e["to"] for e in topology["edges"] if e["from"] == path[-1] and e["rel"] == "DISPATCHES_TO"), None)
            return path + ([line] if line else [])
        for nxt in adjacency.get(path[-1], []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return [robot_id]


_NEO4J_DRIVER: Any = None


def _neo4j_driver() -> Any:
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is None:
        from neo4j import GraphDatabase

        _NEO4J_DRIVER = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
            connection_timeout=5,
            connection_acquisition_timeout=8,
            max_transaction_retry_time=4,
        )
    return _NEO4J_DRIVER


def _reset_neo4j_driver() -> None:
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is not None:
        try:
            _NEO4J_DRIVER.close()
        except Exception:  # noqa: BLE001
            pass
    _NEO4J_DRIVER = None


def _neo4j_topology(robot_id: str) -> Optional[dict[str, Any]]:
    if not (os.getenv("NEO4J_URI") and os.getenv("NEO4J_USERNAME") and os.getenv("NEO4J_PASSWORD")):
        return None

    try:
        with _neo4j_driver().session() as session:
            rows = session.run(
                "MATCH (a)-[r]->(b) WHERE type(r) IN $rels AND a.id IS NOT NULL AND b.id IS NOT NULL "
                "RETURN a.id AS from_id, coalesce(a.name, a.id) AS from_name, labels(a)[0] AS from_kind, "
                "type(r) AS rel, b.id AS to_id, coalesce(b.name, b.id) AS to_name, labels(b)[0] AS to_kind",
                rels=list(TOPOLOGY_RELATIONSHIPS),
            ).data()
            path_record = session.run(
                "MATCH p=(r:Robot {id: $robot_id})-[:CONNECTS_THROUGH|UPLINKS_TO|REACHES*1..4]->(s:Service) "
                "OPTIONAL MATCH (s)-[:DISPATCHES_TO]->(l:ProductionLine) "
                "RETURN [n IN nodes(p) | n.id] AS ids, l.id AS line_id LIMIT 1",
                robot_id=robot_id,
            ).single()
    except Exception:
        _reset_neo4j_driver()
        raise

    if not rows:
        return None
    nodes: dict[str, dict[str, str]] = {}
    edges = []
    for row in rows:
        nodes[row["from_id"]] = {"id": row["from_id"], "name": row["from_name"], "kind": row["from_kind"]}
        nodes[row["to_id"]] = {"id": row["to_id"], "name": row["to_name"], "kind": row["to_kind"]}
        edges.append({"from": row["from_id"], "to": row["to_id"], "rel": row["rel"]})
    path = [robot_id]
    if path_record:
        path = list(path_record["ids"]) + ([path_record["line_id"]] if path_record["line_id"] else [])
    return {"nodes": list(nodes.values()), "edges": edges, "path": path}


def get_topology(scenario: dict[str, Any], demo_topology: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    robot_id = scenario["robot_id"]
    detail = "Neo4j not configured; using the bundled demo graph."
    if integration_status()["neo4j"]["configured"]:
        try:
            live = _neo4j_topology(robot_id)
            if live:
                return {**live, "source": "neo4j", "status": "live", "ms": _elapsed_ms(start),
                        "detail": f"Cypher traversal returned {len(live['nodes'])} assets and a {len(live['path'])}-hop dependency path."}
            detail = "Neo4j returned no graph; run neo4j/seed.cypher. Using the demo graph."
        except Exception as exc:  # driver import, auth, or connectivity failure
            detail = f"Neo4j unavailable ({type(exc).__name__}); using the demo graph."
    return {
        "nodes": demo_topology["nodes"],
        "edges": demo_topology["edges"],
        "path": _demo_path(demo_topology, robot_id),
        "source": "demo-graph",
        "status": "fallback",
        "ms": _elapsed_ms(start),
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Deterministic evidence scoring (measured facts, never model output)
# ---------------------------------------------------------------------------

def _norm(value: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _is_stale(point: dict[str, Any]) -> bool:
    return (point.get("telemetry_age_s") or 0) > STALE_AFTER_S or point.get("reachable") is None


def score_evidence(scenario: dict[str, Any]) -> dict[str, Any]:
    points = scenario["before"]
    latest = points[-1]
    stale = _is_stale(latest)
    fresh = [p for p in points if not _is_stale(p)] or points
    worst = max(fresh, key=lambda p: p["latency_ms"])
    heartbeat_values = [p["heartbeat_age_s"] for p in fresh if p.get("heartbeat_age_s") is not None]
    worst_heartbeat = max(heartbeat_values) if heartbeat_values else None
    lowest_throughput = min(p["line_throughput_pct"] for p in points)
    fault_points = [p for p in points if p.get("robot_fault_code")]

    network = (_norm(worst["latency_ms"], TARGET_LATENCY_MS, 200) + _norm(worst["loss_pct"], TARGET_LOSS_PCT, 5)
               + _norm(worst["jitter_ms"], TARGET_JITTER_MS, 70)) / 3
    if network > 0.3 and worst_heartbeat is not None and worst_heartbeat > TARGET_HEARTBEAT_S:
        network = min(1.0, network + 0.1)
    robot = 0.9 if fault_points else 0.05
    data = 0.9 if stale else 0.05
    if stale:
        network *= 0.5
        robot *= 0.5

    scores = {"network": round(network, 2), "robot": round(robot, 2), "data": round(data, 2)}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_domain, top_score = ranked[0]
    domain = top_domain if top_score >= 0.4 else "unknown"
    gap = top_score - ranked[1][1]
    if domain in ("data", "unknown"):
        confidence = "low"
    elif top_score >= 0.7 and gap >= 0.3:
        confidence = "high"
    else:
        confidence = "medium" if top_score >= 0.5 else "low"

    evidence: list[str] = []
    if network >= 0.3:
        evidence.append(
            f"Latency to the dispatch API peaked at {worst['latency_ms']} ms with {worst['loss_pct']}% loss and "
            f"{worst['jitter_ms']} ms jitter via {worst['access_point'].upper()} (targets: <{TARGET_LATENCY_MS} ms, <{TARGET_LOSS_PCT}%, <{TARGET_JITTER_MS} ms)."
        )
    elif not stale:
        evidence.append("Network latency, loss and jitter stayed within targets for the whole window.")
    if worst_heartbeat is not None and worst_heartbeat > TARGET_HEARTBEAT_S:
        evidence.append(f"{scenario['robot']} heartbeat age reached {worst_heartbeat} s (target ≤{TARGET_HEARTBEAT_S} s).")
    if fault_points:
        evidence.append(f"{scenario['robot']} reported fault code {fault_points[0]['robot_fault_code']} from {_clock(fault_points[0]['timestamp'])}.")
    if stale:
        evidence.append(f"Telemetry last updated {latest['telemetry_age_s']} s ago; heartbeat and reachability are unknown.")
    evidence.append(f"{scenario['line']} throughput fell to {lowest_throughput}%.")

    return {
        "domain": domain,
        "confidence": confidence,
        "scores": scores,
        "evidence": evidence,
        "worst": worst,
        "fault_code": fault_points[0]["robot_fault_code"] if fault_points else None,
        "stale": stale,
    }


def build_timeline(scenario: dict[str, Any]) -> list[dict[str, str]]:
    points = scenario["before"]
    events = [{"timestamp": points[0]["timestamp"], "title": "Normal operation",
               "detail": f"Latency {points[0]['latency_ms']} ms · throughput {points[0]['line_throughput_pct']}%", "tone": "healthy"}]
    for point in points[1:]:
        if _is_stale(point):
            events.append({"timestamp": point["timestamp"], "title": "Telemetry stops updating",
                           "detail": f"Data age {point['telemetry_age_s']} s · heartbeat unknown", "tone": "warning"})
            break
        if point.get("robot_fault_code"):
            events.append({"timestamp": point["timestamp"], "title": "Robot fault reported",
                           "detail": f"{point['robot_fault_code']} · network normal ({point['latency_ms']} ms)", "tone": "critical"})
            break
        if point["latency_ms"] >= TARGET_LATENCY_MS or (point.get("heartbeat_age_s") or 0) > TARGET_HEARTBEAT_S:
            events.append({"timestamp": point["timestamp"], "title": "Robot heartbeat delayed",
                           "detail": f"Heartbeat age {point['heartbeat_age_s']} s · latency {point['latency_ms']} ms", "tone": "critical"})
            break
    slow = next((p for p in points if p["line_throughput_pct"] < 80), None)
    if slow:
        events.append({"timestamp": slow["timestamp"], "title": f"{scenario['line']} slows",
                       "detail": f"Throughput falls to {slow['line_throughput_pct']}%", "tone": "critical"})
    return events


# ---------------------------------------------------------------------------
# Crusoe Serverless Inference
# ---------------------------------------------------------------------------

def _request_json(url: str, headers: dict[str, str], body: Optional[dict[str, Any]] = None, timeout: int = 12) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(type(exc).__name__) from exc


def _crusoe_chat(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, str]:
    key = os.getenv("CRUSOE_API_KEY")
    if not key:
        raise LookupError("CRUSOE_API_KEY not set")
    model = os.getenv("CRUSOE_MODEL", DEFAULT_CRUSOE_MODEL)
    result = _request_json(
        CRUSOE_URL,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens},
        timeout=20,
    )
    return str(result["choices"][0]["message"]["content"]).strip(), model


def _extract_json(content: str) -> Any:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(content[start:end + 1])


def _clean_list(values: Any, limit: int = 5) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v)[:400] for v in values if isinstance(v, (str, int, float))][:limit]


def _validate_model_output(parsed: Any, measured: dict[str, Any]) -> list[dict[str, Any]]:
    shaped = isinstance(parsed, dict) and isinstance(parsed.get("likely_cause"), str) and bool(_clean_list(parsed.get("evidence")))
    text = (
        " ".join([str(parsed.get("likely_cause", "")), str(parsed.get("plain_summary", ""))] + _clean_list(parsed.get("evidence")))
        if isinstance(parsed, dict)
        else ""
    )
    get = parsed.get if isinstance(parsed, dict) else (lambda *_: None)
    return [
        {"check": "Valid JSON with cause and evidence", "passed": shaped},
        {"check": "Fault domain matches strongest measured signal", "passed": get("domain") == measured["domain"]},
        {"check": "Action is on the approved list", "passed": get("recommended_action_id") in ALLOWED_ACTIONS},
        {"check": "Confidence is high, medium or low", "passed": get("confidence") in CONFIDENCE_LEVELS},
        {"check": "No recovery claimed before post-action data", "passed": not re.search(r"\b(recovered|resolved|fixed)\b", text, re.I)},
    ]


def _fallback_analysis(scenario: dict[str, Any], measured: dict[str, Any]) -> dict[str, Any]:
    domain = measured["domain"]
    ap = measured["worst"]["access_point"].upper()
    causes = {
        "network": f"Network path degradation between {scenario['robot']} and the dispatch API via {ap} is the strongest supported cause.",
        "robot": f"{scenario['robot']} hardware fault ({measured['fault_code']}) is the strongest supported cause; the network path is healthy.",
        "data": "Unknown. Telemetry is stale, so the cause of the slowdown cannot be established yet.",
        "unknown": "No supported cause. Measurements are within targets.",
    }
    alternatives = {
        "network": [f"A robot-side issue is still possible; inspect {scenario['robot']} diagnostics if heartbeats stay late after the network recovers."],
        "robot": ["A PLC timing problem could also slow the line; check PLC cycle logs if the fault clears but throughput stays low."],
        "data": ["The telemetry collector may have failed on its own, unrelated to the slowdown; restore it and re-check before acting."],
        "unknown": ["The slowdown may come from a system that is not instrumented here, such as upstream material supply."],
    }
    next_checks = {
        "network": f"Check {ap} radio utilisation and interference, and confirm {scenario['robot']} roams cleanly to AP-08.",
        "robot": f"Check joint 3 temperature trend and cooling on {scenario['robot']}; confirm the fault clears after maintenance.",
        "data": "Confirm the telemetry collector is running and that new measurements arrive within 60 seconds.",
        "unknown": "Widen the telemetry window and add PLC cycle-time data.",
    }
    plain = {
        "network": f"{scenario['robot']}'s Wi-Fi hotspot is dropping messages, so the robot waits for its next job and the line slows down.",
        "robot": f"{scenario['robot']} has a motor that is overheating. The Wi-Fi is fine; the machine needs maintenance.",
        "data": "The sensors stopped reporting, so nobody can see why the line slowed. Get the data flowing again first.",
        "unknown": "Everything we can measure looks normal, so the cause is somewhere we are not watching.",
    }
    action_id = DOMAIN_ACTION[domain]
    return {
        "plain_summary": plain[domain],
        "likely_cause": causes[domain],
        "domain": domain,
        "confidence": measured["confidence"],
        "evidence": measured["evidence"],
        "alternatives": alternatives[domain],
        "recommended_action_id": action_id,
        "recommended_action": ALLOWED_ACTIONS[action_id],
        "what_to_check_next": next_checks[domain],
    }


def analyze(scenario: dict[str, Any], path_names: list[str], measured: dict[str, Any]) -> dict[str, Any]:
    """Return the diagnosis plus how it was produced. Recovery is never decided here."""
    start = time.perf_counter()
    fallback = _fallback_analysis(scenario, measured)
    base = {"scores": measured["scores"], "measured_evidence": measured["evidence"]}
    if not os.getenv("CRUSOE_API_KEY"):
        return {**fallback, **base, "source": "demo-fallback", "status": "fallback", "model": None,
                "detail": "CRUSOE_API_KEY not set; deterministic analysis used.", "guardrails": [], "ms": _elapsed_ms(start)}

    prompt = {
        "incident": scenario["summary"],
        "robot": scenario["robot"],
        "dependency_path": path_names,
        "pre_action_measurements": scenario["before"],
        "measured_evidence": measured["evidence"],
        "domain_scores": measured["scores"],
        "strongest_measured_domain": measured["domain"],
        "allowed_actions": ALLOWED_ACTIONS,
        "instruction": (
            "Return one JSON object only with keys: plain_summary (one sentence under 25 words that a plant manager with no "
            "networking knowledge understands: say 'Wi-Fi hotspot' not 'access point', no acronyms, no jargon, no numbers except "
            "percentages), likely_cause (string), domain (one of network, robot, data, unknown), "
            "confidence (high, medium or low), evidence (array of short strings citing the supplied numbers), "
            "alternatives (array of strings), recommended_action_id (one key from allowed_actions), what_to_check_next (string). "
            "Use only the supplied pre-action evidence. Network metrics cannot establish mechanical health. "
            "Set domain to strongest_measured_domain unless the evidence clearly contradicts it. "
            "If telemetry is stale, set domain to data and state in likely_cause that the root cause cannot be established until "
            "fresh telemetry arrives. Do not claim anything is recovered or fixed."
        ),
    }
    messages = [
        {"role": "system", "content": "You are an industrial network incident analyst. You answer with JSON only and never invent measurements."},
        {"role": "user", "content": json.dumps(prompt)},
    ]
    try:
        content, model = _crusoe_chat(messages, max_tokens=1200)
        parsed = _extract_json(content)
    except (RuntimeError, LookupError, KeyError, IndexError, TypeError, ValueError) as exc:
        return {**fallback, **base, "source": "demo-fallback", "status": "fallback", "model": None,
                "detail": f"Crusoe request failed ({type(exc).__name__}); deterministic analysis used.", "guardrails": [], "ms": _elapsed_ms(start)}

    guardrails = _validate_model_output(parsed, measured)
    if not all(check["passed"] for check in guardrails):
        return {**fallback, **base, "source": "demo-fallback", "status": "rejected", "model": model,
                "detail": "Crusoe answer failed a guardrail; deterministic analysis shown instead.", "guardrails": guardrails, "ms": _elapsed_ms(start)}

    action_id = parsed["recommended_action_id"]
    return {
        **base,
        "plain_summary": str(parsed.get("plain_summary") or fallback["plain_summary"])[:240],
        "likely_cause": parsed["likely_cause"][:500],
        "domain": parsed["domain"],
        "confidence": parsed["confidence"],
        "evidence": _clean_list(parsed.get("evidence")),
        "alternatives": _clean_list(parsed.get("alternatives"), 3) or fallback["alternatives"],
        "recommended_action_id": action_id,
        "recommended_action": ALLOWED_ACTIONS[action_id],
        "what_to_check_next": str(parsed.get("what_to_check_next") or fallback["what_to_check_next"])[:400],
        "source": "crusoe-serverless-inference",
        "status": "live",
        "model": model,
        "detail": f"{model} on Crusoe Serverless Inference; all guardrails passed.",
        "guardrails": guardrails,
        "ms": _elapsed_ms(start),
    }


def draft_handoff(scenario: dict[str, Any], measured: dict[str, Any], recovery: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    fallback_text = (
        f"{scenario['incident_id']} · {scenario['title']} ({scenario['site']}, {scenario['line']}). "
        f"Measured evidence: {' '.join(measured['evidence'])} "
        f"Action taken: {scenario['simulated_operator_action']} "
        f"Recovery status: {recovery['status'].replace('_', ' ')}. {recovery['summary']}"
    )
    if not os.getenv("CRUSOE_API_KEY"):
        return {"text": fallback_text, "source": "template", "model": None, "ms": _elapsed_ms(start)}
    prompt = {
        "incident": {k: scenario[k] for k in ("incident_id", "title", "site", "line", "robot", "summary")},
        "measured_evidence": measured["evidence"],
        "operator_action": scenario["simulated_operator_action"],
        "recovery": recovery,
        "instruction": "Write a shift handoff note of at most 5 short sentences for the next operations engineer. "
                       "Use only the supplied facts. State the recovery status exactly as given. Plain text, no markdown.",
    }
    try:
        content, model = _crusoe_chat([{"role": "user", "content": json.dumps(prompt)}], max_tokens=600)
        if not content:
            raise ValueError("empty response")
        return {"text": content[:1500], "source": "crusoe-serverless-inference", "model": model, "ms": _elapsed_ms(start)}
    except (RuntimeError, LookupError, KeyError, IndexError, TypeError, ValueError):
        return {"text": fallback_text, "source": "template", "model": None, "ms": _elapsed_ms(start)}


# ---------------------------------------------------------------------------
# Recovery verification (post-action data only, requested by the operator)
# ---------------------------------------------------------------------------

def _fmt(value: Any, unit: str) -> str:
    return "unknown" if value is None else f"{value}{unit}"


def verify_recovery(scenario: dict[str, Any]) -> dict[str, Any]:
    after = scenario.get("after", [])
    if not after:
        return {"status": "inconclusive", "summary": "No post-action measurements are available.", "checks": [], "evidence": []}
    latest = after[-1]
    before_latest = scenario["before"][-1]
    measured = score_evidence(scenario)
    worst = measured["worst"]
    fresh_before = [p for p in scenario["before"] if not _is_stale(p)] or scenario["before"]
    heartbeat_before = max((p["heartbeat_age_s"] for p in fresh_before if p.get("heartbeat_age_s") is not None), default=None)
    if measured["stale"]:
        heartbeat_before = None

    checks = [
        {"name": "Network latency", "before": _fmt(worst["latency_ms"], " ms"), "after": _fmt(latest["latency_ms"], " ms"),
         "target": f"< {TARGET_LATENCY_MS} ms", "passed": latest["latency_ms"] < TARGET_LATENCY_MS},
        {"name": "Packet loss", "before": _fmt(worst["loss_pct"], "%"), "after": _fmt(latest["loss_pct"], "%"),
         "target": f"< {TARGET_LOSS_PCT}%", "passed": latest["loss_pct"] < TARGET_LOSS_PCT},
        {"name": "Jitter", "before": _fmt(worst["jitter_ms"], " ms"), "after": _fmt(latest["jitter_ms"], " ms"),
         "target": f"< {TARGET_JITTER_MS} ms", "passed": latest["jitter_ms"] < TARGET_JITTER_MS},
        {"name": "Robot heartbeat", "before": _fmt(heartbeat_before, " s"), "after": _fmt(latest.get("heartbeat_age_s"), " s"),
         "target": f"≤ {TARGET_HEARTBEAT_S} s", "passed": latest.get("heartbeat_age_s") is not None and latest["heartbeat_age_s"] <= TARGET_HEARTBEAT_S},
        {"name": "Robot fault code", "before": measured["fault_code"] or "none", "after": latest.get("robot_fault_code") or "none",
         "target": "none", "passed": not latest.get("robot_fault_code")},
        {"name": "Telemetry freshness", "before": _fmt(before_latest.get("telemetry_age_s"), " s"), "after": _fmt(latest.get("telemetry_age_s"), " s"),
         "target": f"≤ {STALE_AFTER_S} s", "passed": not _is_stale(latest)},
        {"name": "Line throughput", "before": f"{min(p['line_throughput_pct'] for p in scenario['before'])}%", "after": f"{latest['line_throughput_pct']}%",
         "target": f"≥ {TARGET_THROUGHPUT_PCT}%", "passed": latest["line_throughput_pct"] >= TARGET_THROUGHPUT_PCT},
    ]
    passing = [c["name"] for c in checks if c["passed"]]
    failing = [c["name"] for c in checks if not c["passed"]]
    at = _clock(latest["timestamp"])
    if _is_stale(latest):
        status = "inconclusive"
        summary = f"Post-action telemetry is still stale at {at}; recovery cannot be confirmed."
    elif not failing:
        status = "recovered"
        summary = f"All {len(checks)} recovery checks passed on post-action data at {at}."
    elif not next(c for c in checks if c["name"] == "Line throughput")["passed"]:
        status = "not_recovered"
        summary = f"{scenario['line']} is still at {latest['line_throughput_pct']}% at {at}. Still outside target: {', '.join(failing)}."
    else:
        status = "pending"
        summary = f"{scenario['line']} throughput is back, but {', '.join(failing)} still outside target at {at}."
    evidence = [f"{c['name']}: {c['after']} ({'within target' if c['passed'] else 'outside target'} {c['target']})." for c in checks]
    return {"status": status, "summary": summary, "checks": checks, "evidence": evidence, "passing": passing, "failing": failing}
