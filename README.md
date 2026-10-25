# ADYA Agent Harness

ADYA is a compact governance and product layer built around the existing registry surfaces, with a deterministic local mode and a DSH-native execution mode.

## What is implemented

- A server-enforced governance decision for goals, agents, skills, MCP capabilities, and tool calls.
- Agent contracts with roles, responsibilities, allowed tools, allowed skills, constraints, and acceptance criteria.
- Dynamic role selection and temporary-agent lifecycle cleanup with a zero-active-agent invariant.
- Executable existing MCP, skill, and tool registry entries with input-schema validation.
- Evidence-based review that returns `completed` only when evidence exists, otherwise `needs_review` or a persisted failure state.
- JSON-backed retrieval memory, feedback adaptation, restart/reload retrieval, and admin correction/deletion.
- Separate user and admin planes with user-owned runs and fail-closed admin-token authorization.
- DSH-native `ADYA_LLM_MODE=dsh` execution through the checked-out DeepSeek Harness Node SDK: DSH owns the Agent loop, workflow, temporary child Agents, model-facing tools, and sessions; ADYA owns the product API, contracts, native capability policy, evidence projection, memory, and the HTTP/user/admin planes.

The existing DSH tool registry, workflow engine, subagent lifecycle, and session persistence are reused. The ADYA native plugin adds one governed capability surface over the existing DSH registry instead of maintaining a second agent executor or an HTTP bridge.

## Run locally

```powershell
$env:ADYA_ADMIN_TOKEN = "replace-with-a-long-random-local-token"
$env:ADYA_DATA_FILE = "data/state.json"
python server.py
```

Open `http://localhost:8080`. Send `X-User-Id: alice` when using the user plane. Send `X-Admin-Token` for admin operations.

### Deterministic demo workflow

The default mode is dependency-free and deterministic. It is explicitly labeled `local-demo`; its temporary graph agents are simulated ADYA demo agents, not proof of DSH-native execution.

1. Start the server with the command above.
2. Submit a multi-step goal:

```powershell
$body = @{ goal = "Build a governed registry-backed multi-agent memory demo"; requested_agents = 5 } | ConvertTo-Json
Invoke-RestMethod http://localhost:8080/api/runs -Method Post -Headers @{ "X-User-Id" = "alice" } -ContentType "application/json" -Body $body
```

3. Inspect the returned `graph.nodes`, `events`, `governance`, `artifacts.review`, `memory_hits`, and cleanup event.
4. Add feedback, then run another goal to retrieve the stored experience:

```powershell
$feedback = @{ run_id = "<run-id-from-response>"; text = "Prefer registry evidence before synthesis"; score = 5 } | ConvertTo-Json
Invoke-RestMethod http://localhost:8080/api/feedback -Method Post -Headers @{ "X-User-Id" = "alice" } -ContentType "application/json" -Body $feedback
$body = @{ goal = "Use prior registry memory to build another governed demo" } | ConvertTo-Json
Invoke-RestMethod http://localhost:8080/api/runs -Method Post -Headers @{ "X-User-Id" = "alice" } -ContentType "application/json" -Body $body
```

5. Set `ADYA_ADMIN_TOKEN` and use the admin plane to inspect `/api/admin/audit`, `/api/admin/errors`, registries, contracts, and memory.

### DSH mode

Set `ADYA_LLM_MODE=dsh` and follow `docs/setup.md`. This path fails closed if the checked-out DSH runtime is unavailable; it does not fall back to the deterministic local orchestrator.

## Check the implementation

```powershell
python -m unittest discover -s tests -v
python -m pytest -q
python -m py_compile harness.py server.py adya-share/harness.py adya-share/server.py
```

The architecture and configuration details are in [`docs/architecture.md`](docs/architecture.md) and [`docs/setup.md`](docs/setup.md). The native DSH acceptance test is conditional; run it with `ADYA_DSH_ACCEPTANCE=1` when Node and the checked-out DSH source runtime are available. The checked-in fixture is keyless; a live provider run is a separate, unclaimed check.

## Deployment status

The service is container-ready through `Dockerfile`, but no GCP deployment is claimed here. A GCP project, billing/free-tier choice, region, and authenticated `gcloud` account must be confirmed before an external deployment is performed. JSON state on local disk is suitable for a demo, not a multi-instance Cloud Run deployment without a durable datastore.
