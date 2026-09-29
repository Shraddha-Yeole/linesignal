# LineSignal hackathon build plan

## Product goal

Help a plant operations team answer two questions quickly:

1. Why did a production line slow down?
2. After the team acts, did the line recover?

The product correlates equipment events, service health, and network signals. It presents a likely cause with evidence, a safe next step, and a recovery check. It must say **unknown** when the available evidence is not enough.

## Keep the first version small

Build one incident, one factory line, and one recovery path. Do not build a general chatbot, autonomous network changes, multi-agent orchestration, or live OT control. Use simulated/replayed telemetry in the demo so the team can finish even without access to a real plant.

### Partner tools to use

| Tool | Use in LineSignal | Done when |
|---|---|---|
| Neo4j | Store the dependency graph: site → line → robot → access point → switch → dispatch API | The incident view can show the impacted line and its upstream dependencies from a graph query |
| Crusoe Serverless Inference | Run the evidence-based diagnosis on a Crusoe-hosted model | Model output is grounded in supplied metrics, validated as JSON, and has a deterministic fallback |
| Brave Search | Retrieve a relevant manufacturer or network troubleshooting reference | The incident includes a short title and source link, or clearly says no reference was found |

Use only these three partner integrations for the first build. Crusoe is the inference platform, not just a deployment logo. Keep the telemetry input generic. Add ThousandEyes later as another `TelemetrySource`; do not make the UI, graph, or analysis prompt ThousandEyes-specific.

## Build sequence

### 1. Confirm the judging constraints

- Confirm the hackathon deadline, required deployment location, judging criteria, and any rules about external APIs or synthetic data.
- Ask the organizers whether partner API keys are already provisioned and how integrations are scored.
- Assign one person to own the final demo and backup recording.

**Output:** one-page scope accepted by the team.

### 2. Validate the problem and story

- Describe the target user as a plant operations or network operations engineer supporting mobile robots or connected production equipment.
- Validate the key pain with one or two short conversations or a UserTesting session: when a line slows, what evidence is fragmented, who owns the fix, and how do they confirm recovery?
- Use Similarweb only if the team needs quick market/competitor context; it is not required in the product flow.

**Output:** a one-sentence user problem and one realistic incident story.

### 3. Lock the scenario and success metric

Use the included scenario or replace it with one grounded in user feedback. The demo scenario should have:

- A line slowdown and missed robot heartbeats.
- Network latency/loss/jitter rising in the same time window.
- A dependency path from robot to line and dispatch API.
- A recovery window where the key metrics return to baseline after the operator action.

Track time to a supported diagnosis and whether recovery is verified. Never label recovery based only on the action being submitted.

**Output:** before/after JSON fixture plus expected diagnosis and recovery result.

### 4. Agree on the telemetry contract

Use a small normalized event model so every future data source behaves the same:

```json
{
  "timestamp": "2026-09-28T10:02:00Z",
  "asset_id": "robot-a12",
  "site": "plant-1",
  "target": "dispatch-api",
  "latency_ms": 260,
  "loss_pct": 8,
  "jitter_ms": 90,
  "reachable": true,
  "path_hops": []
}
```

Add a `source` field and preserve the original timestamp and units. Keep machine/robot health as separate events; network telemetry alone cannot establish mechanical health.

**Output:** schema reviewed by everyone before parallel work starts.

### 5. Seed and query Neo4j

- Create nodes for site, line, robot, AP, switch, and dispatch API.
- Add `DEPENDS_ON` relationships in both directions only if the use case needs them; otherwise keep one documented direction.
- Add a query that returns the impacted line and path from the affected robot to its controller/API.
- Show the returned path in the UI as readable labels, not raw Cypher.

**Acceptance check:** changing the affected robot in the incident fixture changes the dependency path shown by the app.

### 6. Build the evidence-first diagnosis with Crusoe

- Create a Crusoe Intelligence API key and select a model currently available for Serverless Inference in the Crusoe console.
- Use the Crusoe OpenAI-compatible inference endpoint from the backend; this keeps the key off the browser and avoids provisioning a GPU VM for the demo.
- Pass the model only structured incident evidence, dependency path, and the allowed action list.
- Ask for JSON fields: `likely_cause`, `confidence`, `evidence[]`, `alternatives[]`, `recommended_action`, and `what_to_check_next`.
- Validate the JSON and reject unsupported actions. Keep a local deterministic fallback for live demo reliability if the Crusoe endpoint is unavailable.
- Show at least two pieces of evidence and one alternative explanation in the UI.

**Acceptance check:** the model cannot claim that a physical fault is fixed just because network metrics improved.

### 7. Add Brave Search as supporting evidence

- Search with the specific device family, alert/error phrase, or documented symptom—not a broad prompt.
- Display the result title, source, and link.
- Treat search results as references for the operator, not as proof of root cause.
- If the key is missing, show a clear “reference lookup unavailable” state and continue the incident analysis.

### 8. Add a human-approved action and recovery check

- Present a safe recommended next step (for the demo, route/escalate to the network owner or apply the simulated fix).
- Require the operator to click **Apply demo fix**.
- Load the after-window and compare latency, loss, jitter, robot heartbeat, and line status with their expected ranges.
- Return `recovered`, `not_recovered`, `pending`, or `inconclusive`, with the evidence for each check.

**Acceptance check:** clicking the action does not itself mark the incident recovered.

### 9. Finish the interface

Keep one screen with:

- Current line status and impact.
- Incident timeline.
- Dependency path.
- Likely cause, confidence, evidence, and alternative.
- Safe action requiring approval.
- Recovery status and evidence.

Use a clear distinction between measured facts and model explanation. Avoid a chat-only UI.

### 10. Test failure cases

- Network is degraded but robot events are missing.
- Robot reports a fault while network measurements are normal.
- Data is stale or incomplete.
- Crusoe inference or Brave is unavailable.
- Neo4j is unavailable.

**Acceptance check:** the app falls back cleanly and reports uncertainty rather than inventing a cause.

### 11. Deploy and prepare the backup

- Deploy the app using one platform the team can access; DuploCloud is optional if the team already has credentials and can deploy there quickly.
- Store keys as server-side environment secrets.
- Never put secrets in the front end, Git, screenshots, or demo recording.
- Keep a local demo mode and a screen recording ready in case deployment or a partner API fails.

### 12. Rehearse the 90-second demo

1. “Line 4 is slowing, but the team does not know whether it is the robot, network, or controller.”
2. Show the robot heartbeat and network measurements changing together.
3. Show Neo4j tracing the robot to the line and dispatch API.
4. Show Crusoe-hosted inference producing the evidence-based diagnosis and Brave’s reference.
5. Approve the simulated fix.
6. Show the recovery checks and say exactly which signals recovered.
7. Close with: “The system verifies recovery; it does not assume the fix worked.”

## Suggested team split

For a four-person team:

1. **Product/demo owner:** user validation, incident story, UI text, judge narrative.
2. **Data/graph owner:** telemetry fixture and Neo4j graph/query.
3. **Agent owner:** Crusoe prompt, structured output, uncertainty, Brave Search.
4. **App/deployment owner:** UI, backend wiring, secrets, deployment, smoke test.

For a smaller team, combine product with demo and combine graph with backend.

## Priority order if time is short

1. Working before/after demo with evidence and recovery verification.
2. Neo4j graph query visible in the output.
3. Crusoe structured diagnosis with fallback.
4. Brave reference lookup.
5. Deployment polish and user feedback.

Never sacrifice the recovery check to add another integration.

## Future ThousandEyes path

Create a `ThousandEyesTelemetrySource` that maps ThousandEyes test results to the telemetry contract. Keep credentials server-side. Start with a controlled test target that represents the dispatch API or a service endpoint. The existing incident analysis, graph, UI, and verification flow should keep working unchanged.
