# ADYA setup

## Demo mode

The default mode has no Python package dependencies beyond the standard library.

```powershell
$env:ADYA_ADMIN_TOKEN = "replace-with-a-long-random-local-token"
$env:ADYA_DATA_FILE = "$pwd\data\state.json"
$env:PORT = "8080"
python server.py
```

Use `X-User-Id: alice` for user-owned runs. The browser UI stores an admin token only in session storage; do not put a real token in source files.

## Optional direct DeepSeek mode

```powershell
$env:ADYA_LLM_MODE = "deepseek"
$env:DEEPSEEK_API_KEY = "set-outside-the-repository"
$env:DEEPSEEK_MODEL = "deepseek-chat"
python server.py
```

If the provider is unavailable, the run remains governed and the adapter error is recorded instead of being treated as model evidence.

## Optional DeepSeek Harness mode

The checkout under `deepseek-harness/` is the source runtime. The ADYA native path uses DSH's Node SDK and existing profile composition; DSH owns the root Agent loop, workflow engine, temporary child Agents, native tool boundary, and session persistence. The ADYA plugin adds the product contract/governance policy and capability definitions inside that same DSH process. No Python SDK install or ADYA HTTP bridge is required.

```powershell
$env:ADYA_LLM_MODE = "dsh"
$env:ADYA_DSH_HOME = "$pwd\.dsh-adya"
$env:ADYA_DSH_TIMEOUT_SECONDS = "120"
python server.py
```

The prototype includes a deterministic keyless DSH adapter at `deepseek-harness/scripts/adya-native-llm.mjs`. It exercises the real DSH Agent loop, workflow engine, child-agent lifecycle, tool registry, and session protocol without a paid provider. To use a real provider, replace the fixture overlay through a provider-specific DSH patch and configure credentials outside the repository.

Optional launch overrides:

- `ADYA_DSH_NATIVE_PLUGIN_PATH`: override the native ADYA plugin path.
- `ADYA_DSH_NATIVE_RUNNER`: override the Node SDK runner path.
- `ADYA_DSH_NATIVE_FIXTURE_PATH`: override the deterministic adapter path.
- `ADYA_NODE_BIN`: Node executable name or absolute path when `node` is not on `PATH`.
- `ADYA_DSH_PERMISSION_MODE`: DSH sandbox mode; the local prototype defaults to `danger-full-access` because all demo capability execution is inside the governed ADYA plugin.
- `ADYA_DSH_TIMEOUT_SECONDS`: native DSH request budget.

## Tests and checks

Run from the repository root:

```powershell
python -m unittest discover -s tests -v
python -m pytest -q
python -m py_compile harness.py server.py adya-share/harness.py adya-share/server.py
```

For the checked-in DSH source checkout, the existing source checks are:

```powershell
pnpm run typecheck
pnpm dsh --version
pnpm dsh web --help
```

## Container

```powershell
```

The image is a local/demo container. Do not expose it publicly until authentication, durable shared storage, secret management, and deployment configuration are reviewed.
