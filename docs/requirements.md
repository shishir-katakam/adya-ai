# ADYA CEO requirement inventory

This document maps the CEO call and the corrected implementation brief to the current local evidence. It distinguishes what is implemented and tested from what still needs an external provider or deployment credential.

| Requirement | Current status | Evidence |
|---|---|---|
| Agent harness with goal-driven execution | Implemented locally and through the native DSH runtime | `AgentRuntime.submit`, `adya-share/adya_native_plugin.mjs`, `deepseek-harness/scripts/adya-native-runner.mjs`, native HTTP smoke |
| MCP registry | Implemented and reused | `RegistryManager`, `invoke_mcp`, `mcp-memory`, `test_registered_skill_and_mcp_are_executable_through_existing_registries` |
| Skill registry | Implemented and executable | `invoke_skill`, schemas and controlled executors, same registry test |
| Tool registry | Implemented and executable | `invoke_tool`, schema validation, disable/remove fail-closed tests |
| Governance protocol | Implemented at goal and action boundaries | `GovernanceEngine.evaluate`, `evaluate_action`, audit events, denied-tool test |
| Agent contracts | Implemented with role-specific tool/skill allowlists | `role_specs`, contract checks, denied reviewer call test |
| Dynamic multi-agent graph | DSH-native path uses native workflow/subagent lifecycle; local demo path remains available | `workflowEngine.start`, `ctx.subagents`, observed DSH lifecycle events, zero-active-child cleanup evidence |
| Memory and continual learning | Implemented as retrieval memory plus feedback adaptation | Restart/reload test, owner filtering, correction/deletion endpoints; no model-weight training claim |
| Admin plane | Implemented and fail-closed | `ADYA_ADMIN_TOKEN`, protected registry/policy/contract/memory/audit/error routes, HTTP plane test |
| User plane | Implemented with run ownership | `X-User-Id`, owner-only run reads and feedback, HTTP plane test |
| Evidence-based review | Implemented | Review artifact records criteria and tool evidence; unverifiable work becomes `needs_review` |
| Real safe tool invocation | Implemented locally | `goal.echo`, `memory.search`, `memory.write` invocations are executed and recorded, not mocked responses |
| DeepSeek Harness provider | Native DSH source runtime exercised with a deterministic keyless adapter; paid-provider path remains unverified | `adya_native_plugin.mjs`, DSH Node SDK runner, native workflow/child lifecycle smoke, `adya-native-llm.mjs`; no provider credentials were used |
| Architecture/setup documentation | Implemented | `docs/architecture.md`, `docs/setup.md`, root `README.md`, `.env.example` |
| GCP deployment | Not performed | `Dockerfile` is prepared; no project/credential/deployment receipt is available |

## Local checks

From the repository root:

```text
python -m unittest discover -s tests -v
python -m pytest -q
python -m py_compile harness.py server.py adya-share/harness.py adya-share/server.py
```

From `deepseek-harness/`:

```text
pnpm run typecheck
pnpm dsh --version
pnpm dsh web --help
```

The application checks exercise the local end-to-end path and a mocked DSH boundary path. The conditional native acceptance test exercises the real checked-out DSH Node runtime with five native child agents, native workflow lifecycle, governed capability calls, cleanup, and the deterministic keyless adapter when `ADYA_DSH_ACCEPTANCE=1` is set. The native test is real DSH execution, but it is not evidence of a live paid-provider response.
