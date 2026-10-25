# ADYA Agent Harness: implementation notes

The root `harness.py` and `server.py` files are compatibility entry points. The implementation lives in `adya-share/` and is shared by the tests and the root launch command.

## Run locally (Windows PowerShell)

```powershell
$env:ADYA_ADMIN_TOKEN = "replace-with-a-long-random-local-token"
$env:ADYA_DATA_FILE = "data/state.json"
python server.py
```

Open `http://localhost:8080`.

Admin API calls must send `X-Admin-Token: <the configured token>`. If `ADYA_ADMIN_TOKEN` is missing, admin actions are denied instead of being publicly accessible.

Run tests:

```powershell
python -m unittest discover -s tests -v
```

## Current boundaries

- Demo mode is dependency-free and deterministic. Direct DeepSeek mode is configuration-dependent. DSH mode is a native DSH execution path and requires Node, the checked-out DSH source runtime, and an explicit DSH home; the checked-in keyless fixture avoids provider credentials for the prototype demo.
- The registered MCP, skill, and tool entries use controlled local executors. Request payloads cannot execute arbitrary Python.
- In DSH mode, the DSH root Agent calls the ADYA native plugin's workflow and capability tools. Governance and contracts are enforced inside that native tool boundary before capability execution; DSH startup/execution failures fail the run instead of falling back locally.
- JSON persistence is restart-safe for a single instance but is not a durable shared Cloud Run datastore.
- Retrieval memory and feedback adaptation are implemented; `learned_patterns` is not model-weight training.
- No GCP deployment is claimed. Configure authentication, durable storage, secret management, and a project before public deployment.
