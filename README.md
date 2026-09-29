# LineSignal

**Find why a factory line slowed down, then verify the recovery.**

A vendor-neutral incident console for plant network operators. It correlates robot, network and production-line telemetry, traces the dependency path in **Neo4j**, explains the likely cause with **Crusoe Serverless Inference** behind deterministic guardrails, and only declares recovery after checking post-action measurements.

The look and feel come from the CyclOps mining-ops dashboard: React + TypeScript + Vite, Recharts, and the same dark operations theme.

## Run the demo

Requires Python 3.9+ and, only to rebuild the UI, Node 18+.

```bash
cd LineSignal
python3 app.py
```

Open <http://localhost:8000>. The compiled UI in `web/dist` is served by the Python API, so no Node step is needed to demo. Everything works without credentials using replayed telemetry and a deterministic fallback analysis; each panel says whether it used a live partner or the fallback.

### Develop the UI

```bash
python3 app.py          # API on :8000
cd web
npm install
npm run dev             # Vite on :5173, proxies /api to :8000
npm run build           # refresh web/dist for the Python server
```

## Demo walkthrough (90 seconds)

1. **Wi-Fi problem** opens on one question: Line 4 is at 52%. Is it the network, the robot or the data? The map shows the failing robot → AP-07 → SW-04 → Dispatch API path, drawn from Neo4j.
2. Click **Find the cause**. The Network tile lights up as the likely cause; Robot and Data are ruled out. The proof chips show the path came from Neo4j, the explanation from Crusoe, and all safety checks passed.
3. Click **Approve fix & verify**. Post-action data arrives, the map reroutes A12 through AP-08, and the big number turns green: **Fixed, and proven by the data.**
4. Scroll to **The evidence** for the step-by-step pipeline, measured facts versus the Crusoe explanation, the recovery checks, and a Crusoe-drafted shift handoff note.
5. Switch to **Robot problem**: the network is healthy, the robot is the cause, and the check honestly reports **Not recovered yet**. Switch to **Missing data**: LineSignal says it can't tell yet because the data is stale.

## How each partner is used

| Partner | Role in LineSignal | Without credentials |
|---|---|---|
| Neo4j | Topology and Cypher path traversal from the affected robot to the service and line (`neo4j/seed.cypher`) | Bundled `topology.json`, same shape |
| Crusoe Serverless Inference | Structured diagnosis (cause, domain, confidence, evidence, action) and the shift handoff note | Deterministic analysis and template note |
Crusoe output must pass five guardrails before it is shown: valid JSON with evidence, domain matches the strongest measured signal, action is on the approved list, confidence is high/medium/low, and no recovery is claimed before post-action data. A failed guardrail falls back to the deterministic analysis and is shown in the UI.

## Connect the partner tools

Copy `.env.example` to `.env` and set the credentials you have. `app.py` reads `.env` on start; keys never reach the browser.

- **Neo4j:** set `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`; run `neo4j/seed.cypher` in Neo4j Browser; `pip install -r requirements.txt`.
- **Crusoe:** create an Intelligence API key, set `CRUSOE_API_KEY`, and choose an available `CRUSOE_MODEL` (default `openai/gpt-oss-120b`).
## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/scenarios` | Scenario list |
| GET | `/api/scenario?id=` | Pre-action telemetry, timeline, topology (post-action data is withheld) |
| GET | `/api/integrations` | Which partners are configured (booleans only) |
| POST | `/api/analyze` `{scenario_id}` | Diagnosis and pipeline trace |
| POST | `/api/verify` `{scenario_id}` | Post-action telemetry and recovery checks |
| POST | `/api/report` `{scenario_id}` | Shift handoff note |

## Future ThousandEyes connector

Implement `ThousandEyesTelemetrySource` behind the existing `TelemetrySource` seam. Map ThousandEyes network test results to the normalized fields used in `scenarios/*.json` (`timestamp`, `asset_id`, `target`, `latency_ms`, `loss_pct`, `jitter_ms`, `reachable`, optional `path_hops`). Keep the bearer token server-side. The UI, graph, diagnosis and verification do not depend on the source vendor.

## Project files

- `app.py` — JSON API and static server for the built UI.
- `connectors.py` — Neo4j, Crusoe, evidence scoring and recovery checks.
- `scenarios/*.json` — replayable incidents (wireless degradation, robot servo fault, stale telemetry).
- `topology.json` / `neo4j/seed.cypher` — dependency graph (demo copy and Neo4j seed).
- `web/` — React + Vite UI (`src/components/*`, `src/styles/theme.css`); `web/dist` is the built bundle.
- `PLAN.md` — build sequence, acceptance checks and judge demo.
