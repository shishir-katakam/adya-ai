# ADYA architecture

## Runtime boundary

`server.py` is the HTTP entry point. The root file is a compatibility loader for the implementation in `adya-share/`, so the repository-root command and the test import path execute the same code.

`adya-share/harness.py` owns ADYA state, governance, registry data, persistence, and the HTTP-facing run record. It deliberately uses the existing registry data model rather than creating a second MCP, skill, or tool framework.

## Request flow

1. A caller submits a goal to `POST /api/runs` with `X-User-Id`. This header is a local ownership selector, not production authentication.
2. The runtime resolves the selected agent contract and evaluates the goal against its governance policy.
3. The governance decision is written to `audit_events` before execution. A denied goal is persisted as `blocked` with its reason.
4. The runtime selects only the roles required by the goal, subject to the contract's `max_agents` limit.
5. In `ADYA_LLM_MODE=dsh`, the product layer launches the checked-out DSH Node SDK runner. The DSH root Agent owns the model turn; `adya-share/adya_native_plugin.mjs` registers `adya_orchestrate` in the root scope and `adya_capability` in the native DSH tool registry.
6. `adya_orchestrate` uses DSH's native workflow engine and subagent lifecycle. Every temporary DSH child is admitted through ADYA governance before creation, and observed `workflow/agent-start` / `workflow/agent-end` events update the ADYA run graph.
7. DSH child Agents invoke `adya_capability`, which routes into ADYA's existing MCP, skill, and tool registries. Governance and contract checks run immediately before the existing registry executor; the DSH child does not receive a second orchestration tool.
8. The DSH workflow result is reviewed against recorded evidence, written to ADYA retrieval memory, and persisted with the DSH workflow/session identifiers. The reviewer only passes criteria backed by recorded evidence; unverified work is reported as `needs_review`.
9. DSH disposal must leave zero active native children. The native plugin records `cleanup_verified` only when the DSH Agent registry has no live child ids. DSH startup or execution failure is persisted as `failed`; there is no silent local fallback in DSH mode.

## Registry interfaces

The existing registry collections are `registries.mcps`, `registries.skills`, and `registries.tools`.

- MCP entries provide metadata, transport, and safe capability names. The built-in `mcp-memory` entry is executable through `invoke_mcp`.
- Skill entries provide an executor name and a small JSON-schema input definition. Built-in skills are loaded and run through `invoke_skill`.
- Tool entries provide an executor name and a JSON-schema input definition. Built-in tools are invoked through `invoke_tool`.
- Admin endpoints can add, update, inspect, disable, and remove registry entries. Disabled or removed entries fail closed at the execution boundary.

Registered definitions do not execute arbitrary Python from request payloads. Only controlled executor names implemented by the runtime are accepted.

## Governance and contracts

`GovernanceEngine.evaluate` performs goal preflight checks. `GovernanceEngine.evaluate_action` is the middleware used immediately before each agent, skill, MCP, and tool action. It checks the active policy, deny patterns, disabled entries, tool-call limits, approval gates, contract allowlists, and role-specific allowlists.

Each contract contains:

- roles and role-specific responsibilities;
- permitted tools and skills;
- constraints;
- a governance policy reference;
- an agent limit; and
- acceptance criteria.

The server never treats a hidden UI control as authorization. Admin operations require a configured non-empty `ADYA_ADMIN_TOKEN` and a constant-time header comparison. A missing token fails closed.

## Memory and learning

State is stored atomically in the JSON file named by `ADYA_DATA_FILE`.

- Retrieval memory stores run outcomes and approved facts and is searchable in later processes after restart.
- Feedback is stored separately and also creates `feedback_adaptation` memory for the owning user.
- Admin correction and deletion are available for durable memory maintenance.
- `learned_patterns` is a small usage counter only. It is not model-weight training.

User memory is filtered by `user_id`; admin views can inspect all records. Runs and feedback are likewise owner-scoped for ordinary users.

## DeepSeek Harness integration

The DSH path uses the checked-out Node SDK client in `deepseek-harness/scripts/adya-native-runner.mjs` to launch the `sdk` profile and the ADYA-specific plugin at `adya-share/adya_native_plugin.mjs`. The plugin is loaded as a per-launch profile patch; generic DSH core files are not modified. DSH owns the root Agent loop, workflow execution, temporary child Agents, native tool boundary, and session persistence. ADYA remains the product/policy layer: its HTTP boundary, contracts, governance decisions, capability policy, retrieval memory, and run records remain authoritative.

The Python product process passes one JSON request to the DSH Node runner and receives one structured JSON result; no ADYA HTTP bridge or second agent executor is involved. The plugin is installed only in root DSH Agents, so workflow children cannot recursively start another ADYA orchestration. Capability decisions execute inside the native DSH tool boundary before the controlled capability handler runs.

The checked-in unit tests include the retained mocked boundary tests and a conditional real-runtime acceptance test. The keyless prototype acceptance path requires only the checked-out DSH source runtime, Node, and an explicit DSH home; it uses `deepseek-harness/scripts/adya-native-llm.mjs` so provider credentials are not needed. When the DSH runtime is unavailable, `ADYA_LLM_MODE=dsh` fails the run rather than falling back to the local orchestrator.

The default `demo` mode is deterministic and needs no model credentials. `deepseek` mode uses the direct OpenAI-compatible DeepSeek API. No DSH or model credential is committed to the repository.

## Deployment boundary

`Dockerfile` runs the local HTTP service and honors Cloud Run's `PORT`. The JSON file is not a durable shared datastore. A production multi-instance deployment needs an external datastore and a secret manager before it can be claimed as production-ready. No GCP deployment has been performed by this change.
