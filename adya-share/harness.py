"""Compact, dependency-free agent harness for the ADYA demo.

The harness keeps the core concepts deliberately small and explicit:

* MCP, skill, and tool registries that can be extended at runtime.
* Governance policies and agent contracts that are checked before execution.
* A goal-driven multi-agent graph that spawns short-lived agents on demand.
* A local memory store that is updated after each run and can be searched.
* A deterministic demo model with an optional DeepSeek-compatible adapter.

The persistence layer is JSON on purpose. It makes the prototype easy to
inspect, deploy, and replace with Firestore/Postgres later without hiding the
governance and orchestration behavior inside a framework.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


APP_NAME = "ADYA Agent Harness"
APP_VERSION = "0.2.0"


class CapabilityDenied(PermissionError):
    """Raised when governance or an agent contract rejects a capability call."""

    def __init__(self, decision: Dict[str, Any]) -> None:
        self.decision = clone(decision)
        super().__init__(decision.get("reason") or "Capability call denied")


class CapabilityExecutionError(RuntimeError):
    """Raised when an approved capability cannot produce a result."""


class CapabilityTimeout(TimeoutError):
    """Raised when a capability exceeds the bounded execution window."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value or new_id("entry")


def schema_errors(value: Any, schema: Optional[Dict[str, Any]], path: str = "$") -> List[str]:
    """Validate the small JSON-schema subset used by registered capabilities."""
    if not schema:
        return []
    errors: List[str] = []
    expected = schema.get("type")
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected in type_matches and not type_matches[expected]:
        return [f"{path} must be {expected}"]
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path} must contain at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path} exceeds the maximum length")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} exceeds the maximum")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} is not an allowed value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required} is required")
        for key, item in value.items():
            if key not in properties and schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key} is not allowed")
            elif key in properties:
                errors.extend(schema_errors(item, properties[key], f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def sanitize_user_id(value: Optional[str]) -> str:
    """Return a bounded, non-empty identifier suitable for ownership checks."""
    clean = str(value or "anonymous").strip()
    clean = re.sub(r"[^a-zA-Z0-9_.:@-]", "_", clean)[:128]
    return clean or "anonymous"


DEFAULT_STATE: Dict[str, Any] = {
    "version": 2,
    "registries": {
        "mcps": [
            {
                "id": "mcp-memory",
                "name": "Memory MCP",
                "version": "0.1.0",
                "endpoint": "builtin://memory",
                "description": "Searches and writes harness memory.",
                "capabilities": ["memory.search", "memory.write"],
                "transport": "builtin",
                "status": "ready",
            }
        ],
        "skills": [
            {
                "id": "goal-decomposition",
                "name": "Goal Decomposition",
                "version": "1.0.0",
                "description": "Turns a user goal into bounded, verifiable steps.",
                "executor": "goal-decomposition",
                "input_schema": {
                    "type": "object",
                    "required": ["goal", "contract"],
                    "properties": {"goal": {"type": "string"}, "contract": {"type": "object"}},
                    "additionalProperties": False,
                },
                "status": "ready",
            },
            {
                "id": "evidence-synthesis",
                "name": "Evidence Synthesis",
                "version": "1.0.0",
                "description": "Combines memory, registry capabilities, and agent outputs.",
                "executor": "evidence-synthesis",
                "input_schema": {
                    "type": "object",
                    "required": ["goal", "evidence"],
                    "properties": {"goal": {"type": "string"}, "evidence": {"type": "object"}},
                    "additionalProperties": False,
                },
                "status": "ready",
            },
            {
                "id": "risk-review",
                "name": "Risk Review",
                "version": "1.0.0",
                "description": "Checks governance policy and contract acceptance criteria.",
                "executor": "risk-review",
                "input_schema": {
                    "type": "object",
                    "required": ["contract", "evidence"],
                    "properties": {"contract": {"type": "object"}, "evidence": {"type": "object"}},
                    "additionalProperties": False,
                },
                "status": "ready",
            },
        ],
        "tools": [
            {
                "id": "memory.search",
                "name": "Memory Search",
                "version": "1.0.0",
                "description": "Retrieves relevant prior run knowledge.",
                "executor": "memory.search",
                "input_schema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
                "status": "ready",
            },
            {
                "id": "memory.write",
                "name": "Memory Write",
                "version": "1.0.0",
                "description": "Stores durable run knowledge and feedback.",
                "executor": "memory.write",
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "kind": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
                "status": "ready",
            },
            {
                "id": "graph.spawn",
                "name": "Graph Spawn",
                "version": "1.0.0",
                "description": "Creates a goal-driven agent node on demand.",
                "executor": "graph.spawn",
                "input_schema": {
                    "type": "object",
                    "required": ["role", "goal"],
                    "properties": {"role": {"type": "string"}, "goal": {"type": "string"}},
                    "additionalProperties": False,
                },
                "status": "ready",
            },
            {
                "id": "graph.destroy",
                "name": "Graph Destroy",
                "version": "1.0.0",
                "description": "Releases an agent after its goal is complete.",
                "executor": "graph.destroy",
                "input_schema": {
                    "type": "object",
                    "required": ["agent_id"],
                    "properties": {"agent_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                "status": "ready",
            },
            {
                "id": "goal.echo",
                "name": "Goal Echo",
                "version": "1.0.0",
                "description": "Returns the approved goal text as a real local tool result.",
                "executor": "goal.echo",
                "input_schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string", "minLength": 1}},
                    "additionalProperties": False,
                },
                "status": "ready",
            },
        ],
    },
    "governance": {
        "policies": [
            {
                "id": "default-safe",
                "name": "Default Safe Operating Policy",
                "version": "1.0.0",
                "description": "Blocks secret access, destructive operations, and approval bypasses.",
                "deny_patterns": [
                    "exfiltrate secrets",
                    "steal credentials",
                    "bypass approval",
                    "delete production",
                    "disable governance",
                ],
                "approval_required_for": [
                    "external_write",
                    "secret_access",
                    "production_change",
                ],
                "deny_tool_ids": ["secret.read", "shell.exec", "filesystem.delete"],
                "max_tool_calls": 20,
                "max_run_seconds": 30,
                "status": "active",
            }
        ]
    },
    "contracts": [
        {
            "id": "research-build-v1",
            "name": "Research + Build Demo Contract",
            "version": "1.0.0",
            "description": "A bounded contract for a demonstrable multi-agent implementation slice.",
            "governance_policy_id": "default-safe",
            "required_skills": [
                "goal-decomposition",
                "evidence-synthesis",
                "risk-review",
            ],
                "allowed_tools": [
                    "memory.search",
                    "memory.write",
                    "graph.spawn",
                    "graph.destroy",
                    "goal.echo",
                ],
                "agent_roles": [
                "planner",
                "researcher",
                "builder",
                "reviewer",
                "synthesizer",
            ],
                "max_agents": 5,
            "role_specs": {
                "planner": {
                    "responsibilities": ["Decompose the goal into bounded steps."],
                    "allowed_tools": ["graph.spawn", "memory.search"],
                    "allowed_skills": ["goal-decomposition"],
                    "constraints": ["Do not write external state."],
                },
                "researcher": {
                    "responsibilities": ["Retrieve relevant memory and approved capabilities."],
                    "allowed_tools": ["memory.search", "graph.spawn"],
                    "allowed_skills": ["evidence-synthesis"],
                    "constraints": ["Read-only capability use."],
                },
                "builder": {
                    "responsibilities": ["Perform the smallest safe local implementation step."],
                    "allowed_tools": ["goal.echo", "memory.write", "graph.spawn"],
                    "allowed_skills": ["evidence-synthesis"],
                    "constraints": ["Only approved registry tools may be invoked."],
                },
                "reviewer": {
                    "responsibilities": ["Check evidence against acceptance criteria."],
                    "allowed_tools": ["memory.search", "graph.spawn"],
                    "allowed_skills": ["risk-review"],
                    "constraints": ["Never mark missing evidence as passed."],
                },
                "synthesizer": {
                    "responsibilities": ["Prepare the final evidence-based handoff."],
                    "allowed_tools": ["memory.write", "graph.spawn"],
                    "allowed_skills": ["evidence-synthesis"],
                    "constraints": ["Do not claim model training or unverified deployment."],
                },
            },
            "acceptance_criteria": [
                "The goal is decomposed into explicit steps.",
                "Prior memory is searched before the build step.",
                "At least one approved registry capability returns evidence.",
                "A reviewer checks governance and contract criteria.",
                "Temporary agents are destroyed after completion.",
                "The final output is stored in memory for later runs.",
            ],
            "status": "active",
        }
    ],
    "runs": [],
    "memory": [],
    "feedback": [],
    "learned_patterns": {},
    "audit_events": [],
    "errors": [],
}


class StateStore:
    """Thread-safe JSON state store used by both the API and the tests."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or os.getenv("ADYA_DATA_FILE", "data/state.json"))
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return clone(DEFAULT_STATE)
        try:
            return self._normalize(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            # A corrupted local demo file should not prevent the harness from
            # starting. The caller can reset state from the admin plane.
            return clone(DEFAULT_STATE)

    @staticmethod
    def _normalize(state: Dict[str, Any]) -> Dict[str, Any]:
        normalized = clone(DEFAULT_STATE)
        if isinstance(state, dict):
            for key, value in state.items():
                if key in {"registries", "governance"} and isinstance(value, dict):
                    normalized[key].update(value)
                else:
                    normalized[key] = value
        if not isinstance(normalized.get("registries"), dict):
            normalized["registries"] = clone(DEFAULT_STATE["registries"])
        if not isinstance(normalized.get("governance"), dict):
            normalized["governance"] = clone(DEFAULT_STATE["governance"])
        for collection in ("mcps", "skills", "tools"):
            entries = normalized["registries"].setdefault(collection, [])
            if not isinstance(entries, list):
                entries = []
            defaults = {item.get("id"): item for item in DEFAULT_STATE["registries"].get(collection, [])}
            normalized["registries"][collection] = [
                {**clone(defaults.get(item.get("id"), {})), **item}
                for item in entries
                if isinstance(item, dict)
            ]
        if not isinstance(normalized["governance"].get("policies"), list):
            normalized["governance"]["policies"] = clone(DEFAULT_STATE["governance"]["policies"])
        if not isinstance(normalized.get("contracts"), list):
            normalized["contracts"] = clone(DEFAULT_STATE["contracts"])
        for contract in normalized.get("contracts", []):
            if isinstance(contract, dict):
                contract.setdefault("role_specs", clone(DEFAULT_STATE["contracts"][0]["role_specs"]))
                contract.setdefault("required_skills", [])
                contract.setdefault("allowed_tools", [])
                contract.setdefault("agent_roles", [])
                contract.setdefault("acceptance_criteria", [])
                contract.setdefault("max_agents", 5)
        normalized.setdefault("contracts", [])
        for collection in ("runs", "memory", "feedback", "audit_events", "errors"):
            if not isinstance(normalized.get(collection), list):
                normalized[collection] = []
        normalized.setdefault("learned_patterns", {})
        normalized.setdefault("audit_events", [])
        normalized.setdefault("errors", [])
        for run in normalized["runs"]:
            if isinstance(run, dict):
                run.setdefault("user_id", "anonymous")
                run.setdefault("tool_invocations", [])
                run.setdefault("skill_invocations", [])
                run.setdefault("errors", [])
                run.setdefault("metrics", {}).setdefault("tool_calls", 0)
        for item in normalized["memory"]:
            if isinstance(item, dict):
                item.setdefault("user_id", "anonymous")
                item.setdefault("learning_type", "retrieval")
        return normalized

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self.path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return clone(self._state)

    def update(self, mutator: Callable[[Dict[str, Any]], Any]) -> Any:
        with self._lock:
            result = mutator(self._state)
            self._save_locked()
            return clone(result)

    def reset(self) -> Dict[str, Any]:
        with self._lock:
            self._state = clone(DEFAULT_STATE)
            self._save_locked()
            return clone(self._state)


class DeepSeekAdapter:
    """Model adapter with demo, direct API, and DSH-native execution modes."""

    def __init__(self, dsh_bridge_url: Optional[str] = None, dsh_bridge_token: Optional[str] = None) -> None:
        self.mode = os.getenv("ADYA_LLM_MODE", "demo").strip().lower()
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self.last_error: Optional[str] = None
        self.last_dsh_execution: Dict[str, Any] = {}
        self.dsh_bridge_url = (dsh_bridge_url or os.getenv("ADYA_DSH_BRIDGE_URL", "")).strip().rstrip("/")
        self.dsh_bridge_token = (dsh_bridge_token or os.getenv("ADYA_DSH_BRIDGE_TOKEN", "")).strip()
        self.enabled = bool(self.api_key) and self.mode == "deepseek"
        self.dsh_enabled = self.mode == "dsh"

    @property
    def name(self) -> str:
        if self.dsh_enabled:
            return "dsh"
        return "deepseek" if self.enabled else "demo"

    def configure_dsh_bridge(self, url: str, token: str) -> None:
        """Set the loopback bridge used by the DSH-native ADYA plugin."""
        self.dsh_bridge_url = url.strip().rstrip("/")
        self.dsh_bridge_token = token.strip()

    @staticmethod
    def _dsh_plugin_path() -> Path:
        configured = os.getenv("ADYA_DSH_PLUGIN_PATH", "").strip()
        return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().with_name("adya_dsh_plugin.mjs")

    @staticmethod
    def _dsh_patch(plugin_path: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Compose the ADYA plugin over the shipped SDK profile without changing DSH core."""
        return [
            {
                "insert": [
                    {
                        "id": "adya-dsh-plugin",
                        "name": plugin_path.as_uri(),
                        "config": config,
                    },
                ],
            },
            {"id": "tool-bash", "disabled": True},
            {"id": "tool-pwsh", "disabled": True},
            {"id": "tool-fs", "disabled": True},
            {"id": "tool-fs-search", "disabled": True},
            {"id": "tool-subagent", "disabled": True},
            {"id": "tool-subagent-fork", "disabled": True},
            {"id": "tool-workflow", "disabled": True},
        ]

    def execute_goal(
        self,
        goal: str,
        contract: Dict[str, Any],
        roles: List[str],
        registry_context: Dict[str, Any],
        run_id: str,
        user_id: str,
    ) -> str:
        """Run the ADYA goal through DSH's native Node SDK and plugin composition."""
        dsh_home = os.getenv("ADYA_DSH_HOME", "").strip()
        if not dsh_home:
            raise CapabilityExecutionError("ADYA_DSH_HOME is required for ADYA_LLM_MODE=dsh")
        dsh_home_path = Path(dsh_home).expanduser()
        if not dsh_home_path.is_absolute():
            dsh_home_path = (Path.cwd() / dsh_home_path).resolve()
        plugin_path = Path(os.getenv("ADYA_DSH_NATIVE_PLUGIN_PATH", "").strip() or Path(__file__).resolve().with_name("adya_native_plugin.mjs")).expanduser().resolve()
        if not plugin_path.is_file():
            raise CapabilityExecutionError(f"ADYA native DSH plugin was not found: {plugin_path}")
        runner = Path(os.getenv("ADYA_DSH_NATIVE_RUNNER", "").strip() or Path(__file__).resolve().parents[1] / "deepseek-harness" / "scripts" / "adya-native-runner.mjs").expanduser().resolve()
        if not runner.is_file():
            raise CapabilityExecutionError(f"ADYA native DSH runner was not found: {runner}")

        timeout = float(os.getenv("ADYA_DSH_TIMEOUT_SECONDS", "30"))
        node = os.getenv("ADYA_NODE_BIN", "node").strip() or "node"
        fixture_path = Path(os.getenv("ADYA_DSH_NATIVE_FIXTURE_PATH", "").strip() or Path(__file__).resolve().parents[1] / "deepseek-harness" / "scripts" / "adya-native-llm.mjs").expanduser().resolve()
        if not fixture_path.is_file():
            raise CapabilityExecutionError(f"ADYA native DSH fixture was not found: {fixture_path}")
        payload = {
            "goal": goal,
            "contract": clone(contract),
            "roles": list(roles),
            "memory_hits": clone(registry_context.get("memory_hits", [])),
            "run_id": run_id,
            "user_id": user_id,
            "dsh_home": str(dsh_home_path),
            "plugin_path": str(plugin_path),
            "fixture_path": str(fixture_path),
            "cwd": str(Path.cwd()),
            "timeout_ms": max(1000, int(timeout * 1000)),
        }
        try:
            completed = subprocess.run(
                [node, "--import", "tsx/esm", str(runner)],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                cwd=str(runner.parents[1]),
                timeout=timeout + 10,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CapabilityTimeout(f"Native DSH runner exceeded {timeout + 10:.1f}s") from exc
        except OSError as exc:
            raise CapabilityExecutionError(f"Unable to start the native DSH runner: {exc}") from exc
        raw = completed.stdout.strip()
        try:
            result = json.loads(raw) if raw else {}
        except ValueError as exc:
            detail = completed.stderr.strip()[-1200:]
            raise CapabilityExecutionError(f"Native DSH runner returned invalid JSON: {detail or raw[-1200:]}") from exc
        if completed.returncode != 0 or not result.get("ok"):
            detail = str(result.get("error") or completed.stderr.strip() or "native DSH runner failed")
            raise CapabilityExecutionError(detail)
        response = str(result.get("final_response") or "").strip()
        native_result = result.get("adya_result")
        if not isinstance(native_result, dict):
            raise CapabilityExecutionError("Native DSH completed without a structured ADYA result")
        self.last_dsh_execution = {
            "session_id": result.get("session_id"),
            "event_count": len(result.get("events", [])) if isinstance(result.get("events"), list) else 0,
            "tool_boundary_reached": True,
            "native_workflow": True,
            "native_child_lifecycle": True,
            "result": clone(native_result),
            "events": clone(result.get("events", [])),
        }
        if not response:
            raise CapabilityExecutionError("Native DSH completed without a final response")
        return response

    def compose(self, system: str, user: str) -> Optional[str]:
        self.last_error = None
        if self.dsh_enabled:
            raise CapabilityExecutionError("DSH mode uses execute_goal(), not final-synthesis fallback")
        if not self.enabled:
            return None
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            choices = parsed.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            content = message.get("content")
            return content.strip() if isinstance(content, str) and content.strip() else None
        except (OSError, ValueError, KeyError, IndexError, urllib.error.URLError) as exc:
            self.last_error = f"DeepSeek API unavailable: {exc}"
            # The harness remains usable if an optional provider is unavailable.
            return None


class RegistryManager:
    COLLECTIONS = {"mcp": "mcps", "mcps": "mcps", "skill": "skills", "skills": "skills", "tool": "tools", "tools": "tools"}

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _collection_name(self, kind: str) -> str:
        collection = self.COLLECTIONS.get(kind.lower())
        if not collection:
            raise ValueError(f"Unknown registry: {kind}")
        return collection

    def list(self, kind: Optional[str] = None) -> Any:
        state = self.store.snapshot()
        if kind is None:
            return state["registries"]
        return state["registries"][self._collection_name(kind)]

    def get(self, kind: str, entry_id: str) -> Dict[str, Any]:
        collection = self._collection_name(kind)
        entry = next(
            (item for item in self.store.snapshot()["registries"][collection] if item.get("id") == entry_id),
            None,
        )
        if not isinstance(entry, dict):
            raise ValueError(f"Registry entry not found: {entry_id}")
        return clone(entry)

    def inspect(self, kind: str, entry_id: str) -> Dict[str, Any]:
        """Return one existing registry entry without creating a parallel registry."""
        return self.get(kind, entry_id)

    def add(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        collection = self._collection_name(kind)
        if not isinstance(payload, dict):
            raise ValueError("Registry entry must be an object")
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Registry entry name is required")
        entry = clone(payload)
        entry["id"] = str(entry.get("id") or slugify(name))
        entry["name"] = name
        entry["version"] = str(entry.get("version") or "0.1.0")
        entry["status"] = str(entry.get("status") or "ready")
        entry["created_at"] = entry.get("created_at") or utc_now()
        if collection in {"tools", "skills"}:
            entry["input_schema"] = entry.get("input_schema") or {"type": "object"}
            entry["executor"] = str(entry.get("executor") or "")
        if collection == "mcps":
            entry["capabilities"] = [str(item) for item in entry.get("capabilities", []) if str(item).strip()]
            entry["transport"] = str(entry.get("transport") or "builtin")

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            entries = state["registries"][collection]
            if any(item.get("id") == entry["id"] for item in entries):
                raise ValueError(f"Registry entry already exists: {entry['id']}")
            entries.append(entry)
            return entry

        return self.store.update(mutate)

    def update(self, kind: str, entry_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        collection = self._collection_name(kind)
        if not isinstance(payload, dict):
            raise ValueError("Registry update must be an object")

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            entries = state["registries"][collection]
            for entry in entries:
                if entry.get("id") == entry_id:
                    entry.update(clone(payload))
                    entry["id"] = entry_id
                    entry["updated_at"] = utc_now()
                    return entry
            raise ValueError(f"Registry entry not found: {entry_id}")

        return self.store.update(mutate)

    def delete(self, kind: str, entry_id: str) -> bool:
        collection = self._collection_name(kind)

        def mutate(state: Dict[str, Any]) -> bool:
            entries = state["registries"][collection]
            before = len(entries)
            state["registries"][collection] = [item for item in entries if item.get("id") != entry_id]
            return len(state["registries"][collection]) != before

        return bool(self.store.update(mutate))


class GovernanceEngine:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _policy(self, state: Dict[str, Any], policy_id: Optional[str]) -> Dict[str, Any]:
        policies = state.get("governance", {}).get("policies", [])
        for policy in policies:
            if policy.get("id") == policy_id:
                return policy
        return policies[0] if policies else {"id": "implicit-safe", "name": "Implicit Safe Policy", "deny_patterns": []}

    def evaluate(
        self,
        goal: str,
        contract: Dict[str, Any],
        requested_agents: Optional[int] = None,
    ) -> Dict[str, Any]:
        state = self.store.snapshot()
        clean_goal = goal.strip()
        policy = self._policy(state, contract.get("governance_policy_id"))
        reasons: List[str] = []
        if not clean_goal:
            reasons.append("A non-empty goal is required.")
        if len(clean_goal) > 4000:
            reasons.append("Goal exceeds the 4,000-character safety limit.")
        lowered = clean_goal.lower()
        for pattern in policy.get("deny_patterns", []):
            if str(pattern).lower() in lowered:
                reasons.append(f"Goal matched blocked pattern: {pattern}")

        registry_skills = {
            item.get("id") for item in state["registries"]["skills"] if item.get("status") != "disabled"
        }
        registry_tools = {
            item.get("id") for item in state["registries"]["tools"] if item.get("status") != "disabled"
        }
        missing_skills = [item for item in contract.get("required_skills", []) if item not in registry_skills]
        missing_tools = [item for item in contract.get("allowed_tools", []) if item not in registry_tools]
        if missing_skills:
            reasons.append(f"Contract requires missing skills: {', '.join(missing_skills)}")
        if missing_tools:
            reasons.append(f"Contract requires missing tools: {', '.join(missing_tools)}")

        max_agents = int(contract.get("max_agents") or 1)
        if requested_agents is not None and requested_agents < 1:
            reasons.append("At least one agent is required.")
        if requested_agents is not None and requested_agents > max_agents:
            reasons.append(f"Requested agents exceed contract max_agents ({max_agents}).")

        return {
            "allowed": not reasons,
            "policy_id": policy.get("id"),
            "policy_name": policy.get("name"),
            "reasons": reasons,
            "approval_required_for": policy.get("approval_required_for", []),
            "max_agents": max_agents,
        }

    def _record_decision(
        self,
        decision: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        event = {
            "id": new_id("audit"),
            "type": "governance_decision",
            "run_id": run_id,
            "user_id": sanitize_user_id(user_id),
            "at": utc_now(),
            **clone(decision),
        }

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            state.setdefault("audit_events", []).insert(0, event)
            state["audit_events"] = state["audit_events"][:500]
            return event

        self.store.update(mutate)
        return clone(decision)

    def evaluate_action(
        self,
        *,
        goal: str,
        contract: Dict[str, Any],
        role: str,
        capability_type: str,
        capability_id: str,
        arguments: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        user_id: str = "anonymous",
        tool_call_count: int = 0,
        approval_granted: bool = False,
    ) -> Dict[str, Any]:
        """Make and persist an allow/deny decision at every execution boundary."""
        state = self.store.snapshot()
        policy = self._policy(state, contract.get("governance_policy_id"))
        reasons: List[str] = []
        normalized_type = capability_type.strip().lower()
        normalized_id = capability_id.strip()
        clean_goal = goal.strip()
        serialized = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False).lower()
        lowered = f"{clean_goal.lower()} {normalized_id.lower()} {serialized}"

        if not clean_goal:
            reasons.append("A non-empty goal is required for a capability action.")
        if policy.get("status") == "disabled":
            reasons.append(f"Governance policy {policy.get('id')} is disabled.")
        for pattern in policy.get("deny_patterns", []):
            if str(pattern).lower() in lowered:
                reasons.append(f"Action matched blocked pattern: {pattern}")
        if normalized_id in {str(item) for item in policy.get("deny_tool_ids", [])}:
            reasons.append(f"Capability is explicitly blocked: {normalized_id}")
        if normalized_type in {"tool", "mcp"}:
            collection = "tools" if normalized_type == "tool" else "mcps"
            entry = next(
                (item for item in state["registries"][collection] if item.get("id") == normalized_id),
                None,
            )
            if not entry:
                reasons.append(f"Registered {normalized_type} does not exist: {normalized_id}")
            elif entry.get("status") == "disabled":
                reasons.append(f"Registered {normalized_type} is disabled: {normalized_id}")
            if normalized_type == "mcp" and entry and normalized_id not in {
                item.get("id") for item in state["registries"]["mcps"]
            }:
                reasons.append(f"MCP server is not registered: {normalized_id}")
        elif normalized_type == "skill":
            skill = next((item for item in state["registries"]["skills"] if item.get("id") == normalized_id), None)
            if not skill:
                reasons.append(f"Registered skill does not exist: {normalized_id}")
            elif skill.get("status") == "disabled":
                reasons.append(f"Registered skill is disabled: {normalized_id}")
        elif normalized_type == "agent":
            if role not in contract.get("agent_roles", []):
                reasons.append(f"Role is outside the agent contract: {role}")
        else:
            reasons.append(f"Unknown capability type: {capability_type}")

        role_spec = (contract.get("role_specs") or {}).get(role, {})
        if normalized_type == "tool":
            allowed = set(contract.get("allowed_tools", []))
            role_allowed = set(role_spec.get("allowed_tools", allowed))
            if normalized_id not in allowed:
                reasons.append(f"Tool is outside the contract: {normalized_id}")
            if normalized_id not in role_allowed:
                reasons.append(f"Role {role} is not permitted to invoke tool: {normalized_id}")
        if normalized_type == "skill":
            allowed_skills = set(contract.get("required_skills", [])) | set(role_spec.get("allowed_skills", []))
            if normalized_id not in allowed_skills:
                reasons.append(f"Skill is outside the contract: {normalized_id}")
        if normalized_type == "mcp":
            allowed_tools = set(contract.get("allowed_tools", []))
            capability = str((arguments or {}).get("capability", ""))
            role_allowed = set(role_spec.get("allowed_tools", []))
            if normalized_id not in allowed_tools and capability not in allowed_tools:
                reasons.append(f"MCP server is outside the contract: {normalized_id}")
            if capability and capability not in role_allowed and role not in {"system", "admin"}:
                reasons.append(f"Role {role} is not permitted to use MCP capability: {capability}")
        max_tool_calls = int(policy.get("max_tool_calls") or 0)
        if max_tool_calls and tool_call_count >= max_tool_calls and normalized_type in {"tool", "mcp"}:
            reasons.append(f"Tool-call limit reached ({max_tool_calls}).")
        approval_classes = {str(item) for item in policy.get("approval_required_for", [])}
        action_class = str((arguments or {}).get("action_class", ""))
        if not approval_granted and (
            normalized_type in approval_classes
            or normalized_id in approval_classes
            or action_class in approval_classes
        ):
            reasons.append(f"Approval is required for {normalized_id}.")

        decision = {
            "action": normalized_id,
            "capability_type": normalized_type,
            "role": role,
            "allowed": not reasons,
            "reason": "allowed by policy and contract" if not reasons else "; ".join(reasons),
            "reasons": reasons,
            "policy_id": policy.get("id"),
            "policy_name": policy.get("name"),
            "contract_id": contract.get("id"),
            "argument_keys": sorted((arguments or {}).keys()),
        }
        return self._record_decision(decision, run_id=run_id, user_id=user_id)


class MemoryStore:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[a-zA-Z0-9_]{3,}", text.lower()) if token not in {"the", "and", "for", "with", "from", "that", "this"}}

    def search(self, query: str, limit: int = 5, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query_tokens = self._tokens(query)
        state = self.store.snapshot()
        scored = []
        for item in state.get("memory", []):
            if user_id is not None and item.get("user_id", "anonymous") not in {sanitize_user_id(user_id), "shared"}:
                continue
            memory_tokens = self._tokens(str(item.get("text", "")))
            score = len(query_tokens & memory_tokens)
            if score or not query_tokens:
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at", "")), reverse=True)
        return [clone(item) for _, item in scored[:limit]]

    def write(
        self,
        text: str,
        kind: str = "run",
        run_id: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        user_id: str = "anonymous",
        learning_type: str = "retrieval",
    ) -> Dict[str, Any]:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Memory text is required")
        item = {
            "id": new_id("memory"),
            "text": clean_text,
            "kind": kind,
            "run_id": run_id,
            "tags": sorted({str(tag) for tag in (tags or []) if str(tag).strip()}),
            "user_id": sanitize_user_id(user_id),
            "learning_type": learning_type,
            "created_at": utc_now(),
        }

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            state.setdefault("memory", []).insert(0, item)
            state["memory"] = state["memory"][:100]
            return item

        return self.store.update(mutate)

    def record_feedback(
        self,
        run_id: str,
        text: str,
        score: Optional[int] = None,
        user_id: str = "anonymous",
        is_admin: bool = False,
    ) -> Dict[str, Any]:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Feedback text is required")
        actor = sanitize_user_id(user_id)
        feedback = {
            "id": new_id("feedback"),
            "run_id": run_id,
            "text": clean_text,
            "score": score,
            "user_id": actor,
            "created_at": utc_now(),
        }

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            run = next((item for item in state.get("runs", []) if item.get("id") == run_id), None)
            if not run:
                raise ValueError(f"Unknown run: {run_id}")
            if not is_admin and run.get("user_id", "anonymous") != actor:
                raise PermissionError("Feedback is limited to the run owner")
            state.setdefault("feedback", []).insert(0, feedback)
            state["feedback"] = state["feedback"][:100]
            state.setdefault("memory", []).insert(
                0,
                {
                    "id": new_id("memory"),
                    "text": f"Feedback for {run_id}: {feedback['text']}",
                    "kind": "feedback",
                    "run_id": run_id,
                    "tags": ["feedback"],
                    "user_id": run.get("user_id", actor),
                    "learning_type": "feedback_adaptation",
                    "created_at": utc_now(),
                },
            )
            if score is not None:
                patterns = state.setdefault("learned_patterns", {})
                patterns[f"feedback:{int(score)}"] = int(patterns.get(f"feedback:{int(score)}", 0)) + 1
            return feedback

        return self.store.update(mutate)

    def correct(self, memory_id: str, text: str, tags: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Corrected memory text is required")

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            for item in state.get("memory", []):
                if item.get("id") == memory_id:
                    item["text"] = clean_text
                    item["tags"] = sorted({str(tag) for tag in (tags or item.get("tags", [])) if str(tag).strip()})
                    item["learning_type"] = "admin_correction"
                    item["updated_at"] = utc_now()
                    return item
            raise ValueError(f"Memory item not found: {memory_id}")

        return self.store.update(mutate)

    def delete(self, memory_id: str) -> bool:
        def mutate(state: Dict[str, Any]) -> bool:
            before = len(state.get("memory", []))
            state["memory"] = [item for item in state.get("memory", []) if item.get("id") != memory_id]
            return len(state["memory"]) != before

        return bool(self.store.update(mutate))


class AgentRuntime:
    """Goal-driven orchestrator with explicit graph and lifecycle events."""

    def __init__(
        self,
        store: StateStore,
        dsh_bridge_url: Optional[str] = None,
        dsh_bridge_token: Optional[str] = None,
    ) -> None:
        self.store = store
        self.registries = RegistryManager(store)
        self.governance = GovernanceEngine(store)
        self.memory = MemoryStore(store)
        self.model = DeepSeekAdapter(dsh_bridge_url, dsh_bridge_token)
        self._active_runs: Dict[str, Dict[str, Any]] = {}
        self._active_runs_lock = threading.RLock()

    def configure_dsh_bridge(self, url: str, token: str) -> None:
        """Configure the loopback endpoint used by a DSH child runtime."""
        self.model.configure_dsh_bridge(url, token)

    def _track_active_run(self, run: Dict[str, Any]) -> None:
        with self._active_runs_lock:
            self._active_runs[str(run["id"])] = run

    def _untrack_active_run(self, run_id: str) -> None:
        with self._active_runs_lock:
            self._active_runs.pop(run_id, None)

    def _active_run(self, run_id: str, user_id: str) -> Dict[str, Any]:
        actor = sanitize_user_id(user_id)
        with self._active_runs_lock:
            run = self._active_runs.get(run_id)
        if run is None:
            raise ValueError("DSH run is no longer active")
        if run.get("user_id", "anonymous") != actor:
            raise PermissionError("DSH run is owned by another user")
        return run

    @staticmethod
    def _active_native_agents(run: Dict[str, Any]) -> int:
        return sum(
            1
            for node in run.get("graph", {}).get("nodes", [])
            if node.get("native") and node.get("status") == "active"
        )

    def start_dsh_workflow(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Reserve one native DSH workflow and its contract roles."""
        run = self._active_run(str(payload.get("run_id", "")), str(payload.get("user_id", "anonymous")))
        if run.get("dsh_workflow_started"):
            raise CapabilityExecutionError("The native DSH workflow may be started only once")
        roles = payload.get("roles")
        if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or not role.strip() for role in roles):
            raise ValueError("Native DSH workflow roles are required")
        allowed_roles = set((run.get("contract_snapshot") or {}).get("agent_roles", []))
        invalid = [role for role in roles if role not in allowed_roles]
        if invalid:
            raise CapabilityDenied({
                "allowed": False,
                "reason": f"Roles are outside the agent contract: {', '.join(invalid)}",
                "capability_type": "agent",
                "action": "workflow",
            })
        run["dsh_workflow_started"] = True
        run["dsh_expected_roles"] = list(dict.fromkeys(roles))
        run["dsh_admitted_roles"] = []
        self._append_event(run, "dsh_workflow_started", "Reserved native DSH workflow")
        return {"started": True, "roles": clone(run["dsh_expected_roles"])}

    def admit_dsh_agent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Authorize one native DSH workflow role before child creation."""
        run = self._active_run(str(payload.get("run_id", "")), str(payload.get("user_id", "anonymous")))
        if not run.get("dsh_workflow_started"):
            raise CapabilityExecutionError("Native DSH workflow was not started")
        role = str(payload.get("role", "")).strip()
        if not role:
            raise ValueError("DSH agent role is required")
        if role not in run.get("dsh_expected_roles", []):
            raise CapabilityDenied({
                "allowed": False,
                "reason": f"Native DSH role was not reserved: {role}",
                "capability_type": "agent",
                "action": role,
            })
        if role in run.get("dsh_admitted_roles", []):
            raise CapabilityExecutionError(f"Native DSH role was already admitted: {role}")
        decision = self.governance.evaluate_action(
            goal=str(run.get("goal", "")),
            contract=clone(run.get("contract_snapshot") or {}),
            role=role,
            capability_type="agent",
            capability_id=role,
            run_id=str(run["id"]),
            user_id=str(run.get("user_id", "anonymous")),
        )
        if not decision["allowed"]:
            raise CapabilityDenied(decision)
        run.setdefault("dsh_admitted_roles", []).append(role)
        self._append_event(run, "dsh_agent_admitted", f"Native DSH role admitted: {role}")
        return {"allowed": True, "role": role}

    def record_dsh_lifecycle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Record observed native workflow child start/end events."""
        run = self._active_run(str(payload.get("run_id", "")), str(payload.get("user_id", "anonymous")))
        event = str(payload.get("event", "")).strip()
        child_id = str(payload.get("child_id", "")).strip()
        role = str(payload.get("role", "")).strip()
        if event not in {"started", "ended"} or not child_id or not role:
            raise ValueError("Invalid DSH lifecycle payload")
        nodes = run.setdefault("graph", {}).setdefault("nodes", [])
        node = next((item for item in nodes if item.get("id") == child_id), None)
        if event == "started":
            if node is not None:
                raise ValueError(f"DSH child already started: {child_id}")
            admitted = run.setdefault("dsh_admitted_roles", [])
            if role not in admitted:
                raise CapabilityDenied({
                    "allowed": False,
                    "reason": f"Native DSH role was not admitted: {role}",
                    "capability_type": "agent",
                    "action": role,
                })
            admitted.remove(role)
            node = {
                "id": child_id,
                "role": role,
                "goal": f"Native DSH workflow role: {role}",
                "parent_id": None,
                "status": "active",
                "native": True,
                "created_at": utc_now(),
                "destroyed_at": None,
                "result": None,
            }
            nodes.append(node)
            run.setdefault("metrics", {})["dsh_agents_started"] = int(run["metrics"].get("dsh_agents_started", 0)) + 1
            self._append_event(run, "dsh_agent_started", f"Native DSH child started for {role}", child_id)
            return {"recorded": True, "active_agents": self._active_native_agents(run)}
        if node is None:
            raise ValueError(f"Unknown DSH child: {child_id}")
        if node.get("role") != role:
            raise ValueError(f"DSH child role mismatch for {child_id}")
        node["status"] = "destroyed"
        node["destroyed_at"] = utc_now()
        node["result"] = {"outcome": payload.get("outcome"), "workflow_id": payload.get("workflow_id")}
        run.setdefault("metrics", {})["dsh_agents_ended"] = int(run["metrics"].get("dsh_agents_ended", 0)) + 1
        self._append_event(run, "dsh_agent_ended", f"Native DSH child ended for {role}", child_id)
        return {"recorded": True, "active_agents": self._active_native_agents(run)}

    def invoke_dsh_capability(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route a DSH-native tool call into the existing governed ADYA registries."""
        run = self._active_run(str(payload.get("run_id", "")), str(payload.get("user_id", "anonymous")))
        kind = str(payload.get("kind", "")).strip().lower()
        entry_id = str(payload.get("id", "")).strip()
        role = str(payload.get("role", "")).strip()
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("DSH capability arguments must be an object")
        if not entry_id or not role:
            raise ValueError("DSH capability id and role are required")
        goal = str(run.get("goal", ""))
        user_id = str(run.get("user_id", "anonymous"))
        if kind == "tool":
            result = self.invoke_tool(
                entry_id,
                arguments,
                goal=goal,
                role=role,
                run=run,
                user_id=user_id,
                timeout_seconds=5,
            )
        elif kind == "skill":
            skill_arguments = clone(arguments)
            if entry_id == "risk-review":
                skill_arguments["contract"] = clone(run.get("contract_snapshot") or {})
                skill_arguments["evidence"] = {
                    "governance_allowed": bool(run.get("governance", {}).get("allowed")),
                    "tool_invocations": clone(run.get("tool_invocations", [])),
                    "active_agents": self._active_native_agents(run),
                }
            result = self.invoke_skill(
                entry_id,
                skill_arguments,
                goal=goal,
                role=role,
                run=run,
                user_id=user_id,
                timeout_seconds=5,
            )
        elif kind == "mcp":
            capability = str(payload.get("capability", "")).strip()
            if not capability:
                raise ValueError("MCP capability is required")
            result = self.invoke_mcp(
                entry_id,
                capability,
                arguments,
                goal=goal,
                role=role,
                run=run,
                user_id=user_id,
                timeout_seconds=5,
            )
        else:
            raise ValueError(f"Unknown DSH capability kind: {kind}")
        return {"result": clone(result), "kind": kind, "id": entry_id, "role": role}

    def complete_dsh_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Review and persist a native DSH workflow after its children settle."""
        run = self._active_run(str(payload.get("run_id", "")), str(payload.get("user_id", "anonymous")))
        if str(payload.get("workflow_stop_reason", "")) != "completed":
            raise CapabilityExecutionError(
                f"Native DSH workflow ended with {payload.get('workflow_stop_reason', 'unknown')}"
            )
        expected_agents = int(payload.get("agents_started", 0))
        observed_agents = int(run.get("metrics", {}).get("dsh_agents_started", 0))
        if observed_agents != expected_agents:
            raise CapabilityExecutionError(
                f"Native DSH lifecycle evidence is incomplete: observed {observed_agents} of {expected_agents} children"
            )
        if self._active_native_agents(run) != 0 or run.get("dsh_admitted_roles"):
            raise CapabilityExecutionError("Native DSH workflow completed before all admitted children settled")
        workflow_result = payload.get("workflow_result")
        if not isinstance(workflow_result, dict):
            workflow_result = {"value": workflow_result}
        review = self.invoke_skill(
            "risk-review",
            {
                "contract": clone(run.get("contract_snapshot") or {}),
                "evidence": {
                    "governance_allowed": bool(run.get("governance", {}).get("allowed")),
                    "tool_invocations": clone(run.get("tool_invocations", [])),
                    "active_agents": self._active_native_agents(run),
                },
            },
            goal=str(run.get("goal", "")),
            role="reviewer",
            run=run,
            user_id=str(run.get("user_id", "anonymous")),
            timeout_seconds=5,
        )
        review["evidence"] = {
            "tool_invocations": len(run.get("tool_invocations", [])),
            "successful_tool_invocations": sum(
                1 for item in run.get("tool_invocations", []) if item.get("status") == "succeeded"
            ),
            "native_agents_started": int(run.get("metrics", {}).get("dsh_agents_started", 0)),
            "native_agents_ended": int(run.get("metrics", {}).get("dsh_agents_ended", 0)),
        }
        run.setdefault("artifacts", {})["dsh_workflow"] = {
            "workflow_id": payload.get("workflow_id"),
            "stop_reason": payload.get("workflow_stop_reason"),
            "agents_started": payload.get("agents_started", 0),
            "result": clone(workflow_result),
        }
        run["artifacts"]["review"] = clone(review)
        run["status"] = "completed" if review.get("passed") else "needs_review"
        output = (
            f"Completed a DSH-native governed run for: {run['goal']}\n\n"
            f"Native DSH workflow children started and ended: {run['metrics'].get('dsh_agents_started', 0)} / "
            f"{run['metrics'].get('dsh_agents_ended', 0)}\n"
            f"Approved ADYA capability evidence: {run['metrics'].get('tool_calls', 0)} calls\n"
            f"Contract review: {review['status']}\n"
            "The DSH session, workflow result, and governed run summary were persisted."
        )
        run["output"] = output
        self.invoke_tool(
            "memory.write",
            {
                "text": f"Run {run['id']} completed through native DSH workflow for goal '{run['goal']}'. Output: {output[:700]}",
                "kind": "run",
                "tags": ["run", run["status"], "dsh-native", str(run.get("contract_id", ""))],
            },
            goal=str(run.get("goal", "")),
            role="synthesizer",
            run=run,
            user_id=str(run.get("user_id", "anonymous")),
            timeout_seconds=5,
        )
        self._learn(str(run.get("goal", "")))
        self._append_event(run, "dsh_workflow_completed", f"Native DSH workflow completed with {review['status']}")
        return {
            "status": run["status"],
            "output": output,
            "review": clone(review),
            "result": clone(workflow_result),
            "native_agents_started": int(run.get("metrics", {}).get("dsh_agents_started", 0)),
            "native_agents_ended": int(run.get("metrics", {}).get("dsh_agents_ended", 0)),
        }

    def runtime_info(self) -> Dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "model_mode": self.model.name,
            "model_adapter_error": self.model.last_error,
            "harness_backend": "deepseek-harness-native-plugin" if self.model.dsh_enabled else "adya-local",
            "dsh_native_execution": self.model.dsh_enabled,
            "execution_mode": "native-dsh" if self.model.dsh_enabled else "local-demo",
            "agent_lifecycle": "native-dsh-subagents" if self.model.dsh_enabled else "simulated-local-demo",
            "continual_learning": True,
            "learning": {
                "retrieval_memory": True,
                "feedback_adaptation": True,
                "model_training": False,
            },
            "memory": "json-store",
            "graphing": True,
            "admin_plane": True,
            "user_plane": True,
        }

    def _contract(self, contract_id: Optional[str], state: Dict[str, Any]) -> Dict[str, Any]:
        contracts = state.get("contracts", [])
        if contract_id:
            for contract in contracts:
                if contract.get("id") == contract_id:
                    if contract.get("status") == "disabled":
                        raise ValueError(f"Agent contract is disabled: {contract_id}")
                    return clone(contract)
            raise ValueError(f"Unknown contract: {contract_id}")
        if not contracts:
            raise ValueError("No agent contract is configured")
        if contracts[0].get("status") == "disabled":
            raise ValueError(f"Agent contract is disabled: {contracts[0].get('id')}")
        return clone(contracts[0])

    @staticmethod
    def _plan(goal: str, contract: Dict[str, Any]) -> Dict[str, Any]:
        words = []
        for token in re.findall(r"[a-zA-Z0-9]{4,}", goal.lower()):
            if token not in words and token not in {"with", "that", "this", "from", "into", "show", "make"}:
                words.append(token)
        steps = [
            "Clarify the goal and constraints",
            "Search relevant harness memory",
            "Select registry capabilities",
            "Produce a bounded implementation slice",
            "Review against governance and contract criteria",
        ]
        return {
            "goal_keywords": words[:12],
            "steps": steps,
            "acceptance_criteria": clone(contract.get("acceptance_criteria", [])),
        }

    @staticmethod
    def _append_event(run: Dict[str, Any], event_type: str, message: str, agent_id: Optional[str] = None) -> None:
        run["events"].append(
            {
                "type": event_type,
                "message": message,
                "agent_id": agent_id,
                "at": utc_now(),
            }
        )

    def _spawn(self, run: Dict[str, Any], role: str, goal: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
        contract = run.get("contract_snapshot") or {}
        decision = self.governance.evaluate_action(
            goal=run.get("goal", ""),
            contract=contract,
            role=role,
            capability_type="agent",
            capability_id=role,
            run_id=run.get("id"),
            user_id=run.get("user_id", "anonymous"),
        )
        if not decision["allowed"]:
            self._append_event(run, "agent_spawn_denied", decision["reason"])
            raise CapabilityDenied(decision)
        node = {
            "id": new_id("agent"),
            "role": role,
            "goal": goal,
            "parent_id": parent_id,
            "status": "active",
            "created_at": utc_now(),
            "destroyed_at": None,
            "result": None,
        }
        run["graph"]["nodes"].append(node)
        if parent_id:
            run["graph"]["edges"].append({"from": parent_id, "to": node["id"], "type": "delegates"})
        self._append_event(run, "agent_spawned", f"Spawned {role} agent", node["id"])
        return node

    def _destroy(self, run: Dict[str, Any], node: Dict[str, Any], result: Any, reason: str = "completion") -> None:
        node["status"] = "destroyed"
        node["destroyed_at"] = utc_now()
        node["result"] = result
        self._append_event(run, "agent_destroyed", f"Destroyed {node['role']} agent after {reason}", node["id"])

    def _registry_context(self, state: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mcps": [item.get("id") for item in state["registries"]["mcps"] if item.get("status") != "disabled"],
            "skills": [item.get("id") for item in state["registries"]["skills"] if item.get("status") != "disabled"],
            "tools": [item.get("id") for item in state["registries"]["tools"] if item.get("status") != "disabled"],
            "contract_tools": clone(contract.get("allowed_tools", [])),
        }

    def _synthesis_prompt(self, goal: str, plan: Dict[str, Any], review: Dict[str, Any], registry_context: Dict[str, List[str]]) -> str:
        return (
            f"Goal: {goal}\n"
            f"Plan: {json.dumps(plan)}\n"
            f"Review: {json.dumps(review)}\n"
            f"Capabilities: {json.dumps(registry_context)}\n"
            "Write a concise demo handoff with outcome, evidence, risks, and next step."
        )

    @staticmethod
    def _select_roles(goal: str, contract: Dict[str, Any], requested_agents: Optional[int]) -> List[str]:
        configured = list(contract.get("agent_roles") or [])
        if not configured:
            return ["planner"]
        lowered = goal.lower()
        complex_markers = (" and ", "multi", "registry", "governance", "contract", "mcp", "memory", "test")
        execution_markers = ("build", "create", "implement", "run", "invoke", "configure", "write")
        needs_research = any(marker in lowered for marker in ("research", "inspect", "find", "memory", "registry", "mcp"))
        needs_build = any(marker in lowered for marker in execution_markers)
        is_complex = needs_research or needs_build or any(marker in lowered for marker in complex_markers)
        wanted = ["planner"]
        if is_complex and needs_research:
            wanted.append("researcher")
        if is_complex and needs_build:
            wanted.append("builder")
        if "reviewer" in configured:
            wanted.append("reviewer")
        if "synthesizer" in configured and is_complex:
            wanted.append("synthesizer")
        roles: List[str] = []
        for role in wanted + configured:
            if role in configured and role not in roles:
                roles.append(role)
        limit = int(contract.get("max_agents") or len(roles))
        if requested_agents is not None:
            limit = min(limit, max(1, requested_agents))
        return roles[:limit]

    def _record_error(self, run: Dict[str, Any], exc: BaseException, code: str) -> Dict[str, Any]:
        error = {
            "id": new_id("error"),
            "run_id": run.get("id"),
            "user_id": run.get("user_id", "anonymous"),
            "code": code,
            "type": type(exc).__name__,
            "message": str(exc),
            "at": utc_now(),
        }
        run.setdefault("errors", []).append(error)
        self.store.update(
            lambda state: state.setdefault("errors", []).insert(0, clone(error))
        )
        self._append_event(run, "run_error", f"{code}: {error['message']}")
        return error

    @staticmethod
    def _run_bounded(callback: Callable[[], Any], timeout_seconds: Optional[float]) -> Any:
        if timeout_seconds is None or timeout_seconds <= 0:
            return callback()
        result: Dict[str, Any] = {}
        finished = threading.Event()

        def worker() -> None:
            try:
                result["value"] = callback()
            except BaseException as exc:  # Propagated on the calling thread below.
                result["error"] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=worker, name="adya-capability", daemon=True)
        thread.start()
        if not finished.wait(timeout_seconds):
            raise CapabilityTimeout(f"Capability exceeded {timeout_seconds:.3f}s")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _execute_registered_tool(
        self,
        tool: Dict[str, Any],
        arguments: Dict[str, Any],
        run: Optional[Dict[str, Any]],
        user_id: str = "anonymous",
    ) -> Any:
        executor = str(tool.get("executor") or "")
        user_id = (run or {}).get("user_id", user_id)
        run_id = (run or {}).get("id")
        if executor == "memory.search":
            items = self.memory.search(
                str(arguments["query"]),
                int(arguments.get("limit", 5)),
                user_id=user_id,
            )
            return {"count": len(items), "items": items}
        if executor == "memory.write":
            return self.memory.write(
                str(arguments["text"]),
                kind=str(arguments.get("kind", "run")),
                run_id=run_id,
                tags=arguments.get("tags", []),
                user_id=user_id,
            )
        if executor == "goal.echo":
            value = str(arguments["value"])
            return {"echo": value, "length": len(value), "verified": True}
        if executor == "graph.spawn":
            return {"managed": True, "role": arguments["role"], "goal": arguments["goal"]}
        if executor == "graph.destroy":
            return {"managed": True, "agent_id": arguments["agent_id"]}
        raise CapabilityExecutionError(f"No controlled executor is registered for tool {tool.get('id')}")

    def invoke_skill(
        self,
        skill_id: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        goal: str = "",
        contract_id: Optional[str] = None,
        role: str = "system",
        run: Optional[Dict[str, Any]] = None,
        user_id: str = "anonymous",
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        """Load and execute a registered built-in skill through governance."""
        skill = self.registries.get("skill", skill_id)
        args = clone(arguments or {})
        errors = schema_errors(args, skill.get("input_schema"))
        if errors:
            raise ValueError(f"Invalid arguments for {skill_id}: {'; '.join(errors)}")
        state = self.store.snapshot()
        contract = run.get("contract_snapshot") if run else None
        contract = clone(contract) if contract else self._contract(contract_id, state)
        decision = self.governance.evaluate_action(
            goal=goal,
            contract=contract,
            role=role,
            capability_type="skill",
            capability_id=skill_id,
            arguments=args,
            run_id=(run or {}).get("id"),
            user_id=user_id,
        )
        invocation = {
            "id": new_id("skill-call"),
            "skill_id": skill_id,
            "role": role,
            "arguments": clone(args),
            "decision": clone(decision),
            "status": "denied" if not decision["allowed"] else "running",
            "started_at": utc_now(),
        }
        if run is not None:
            run.setdefault("skill_invocations", []).append(invocation)
        if not decision["allowed"]:
            invocation["finished_at"] = utc_now()
            if run is not None:
                self._append_event(run, "skill_denied", decision["reason"])
            raise CapabilityDenied(decision)

        def execute() -> Any:
            executor = str(skill.get("executor") or skill_id)
            if executor == "goal-decomposition":
                return self._plan(str(args["goal"]), args["contract"])
            if executor == "evidence-synthesis":
                evidence = args.get("evidence") or {}
                return {
                    "summary": f"Evidence collected for {args['goal']}",
                    "evidence_keys": sorted(evidence.keys()),
                    "verified": bool(evidence),
                }
            if executor == "risk-review":
                evidence = args.get("evidence") or {}
                criteria = [
                    {"text": "A governance decision exists.", "passed": bool(evidence.get("governance_allowed", True))},
                    {"text": "At least one tool result is recorded.", "passed": bool(evidence.get("tool_invocations"))},
                    {"text": "Temporary agents are cleaned up.", "passed": bool(evidence.get("active_agents", 0) == 0)},
                ]
                return {
                    "passed": all(item["passed"] for item in criteria),
                    "status": "passed" if all(item["passed"] for item in criteria) else "needs_review",
                    "criteria": criteria,
                }
            raise CapabilityExecutionError(f"No controlled executor is registered for skill {skill_id}")

        try:
            result = self._run_bounded(execute, timeout_seconds if timeout_seconds is not None else 10.0)
        except BaseException as exc:
            invocation["status"] = "failed"
            invocation["error"] = {"type": type(exc).__name__, "message": str(exc)}
            invocation["finished_at"] = utc_now()
            if run is not None:
                self._append_event(run, "skill_failed", f"{skill_id}: {exc}")
            raise
        invocation["status"] = "succeeded"
        invocation["result"] = clone(result)
        invocation["finished_at"] = utc_now()
        if run is not None:
            self._append_event(run, "skill_succeeded", f"Loaded and invoked skill {skill_id}")
        return result

    def invoke_mcp(
        self,
        mcp_id: str,
        capability: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        goal: str = "",
        contract_id: Optional[str] = None,
        role: str = "system",
        run: Optional[Dict[str, Any]] = None,
        user_id: str = "anonymous",
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        """Invoke a safe capability exposed by an existing registered MCP."""
        mcp = self.registries.inspect("mcp", mcp_id)
        if capability not in set(mcp.get("capabilities", [])):
            raise CapabilityExecutionError(f"MCP {mcp_id} does not expose capability {capability}")
        state = self.store.snapshot()
        contract = run.get("contract_snapshot") if run else None
        contract = clone(contract) if contract else self._contract(contract_id, state)
        decision = self.governance.evaluate_action(
            goal=goal,
            contract=contract,
            role=role,
            capability_type="mcp",
            capability_id=mcp_id,
            arguments={"capability": capability, **(arguments or {})},
            run_id=(run or {}).get("id"),
            user_id=user_id,
        )
        if not decision["allowed"]:
            if run is not None:
                self._append_event(run, "mcp_denied", decision["reason"])
            raise CapabilityDenied(decision)
        if run is not None:
            self._append_event(run, "mcp_selected", f"Selected registered MCP {mcp_id} ({capability})")
        if mcp.get("transport") == "builtin" and capability in {"memory.search", "memory.write"}:
            result = self.invoke_tool(
                capability,
                arguments or {},
                goal=goal,
                role=role,
                run=run,
                user_id=user_id,
                timeout_seconds=timeout_seconds,
            )
            if run is not None:
                self._append_event(run, "mcp_succeeded", f"Invoked approved MCP capability {capability}")
            return result
        raise CapabilityExecutionError(f"No safe local transport is configured for MCP {mcp_id}")

    def _invoke_tool_once(
        self,
        tool_id: str,
        arguments: Dict[str, Any],
        *,
        goal: str,
        contract: Dict[str, Any],
        role: str,
        run: Optional[Dict[str, Any]],
        user_id: str,
        timeout_seconds: Optional[float],
        approval_granted: bool = False,
    ) -> Any:
        tool = self.registries.get("tool", tool_id)
        errors = schema_errors(arguments, tool.get("input_schema"))
        if errors:
            raise ValueError(f"Invalid arguments for {tool_id}: {'; '.join(errors)}")
        count = int((run or {}).get("metrics", {}).get("tool_calls", 0))
        decision = self.governance.evaluate_action(
            goal=goal,
            contract=contract,
            role=role,
            capability_type="tool",
            capability_id=tool_id,
            arguments=arguments,
            run_id=(run or {}).get("id"),
            user_id=user_id,
            tool_call_count=count,
            approval_granted=approval_granted,
        )
        invocation = {
            "id": new_id("tool-call"),
            "tool_id": tool_id,
            "role": role,
            "arguments": clone(arguments),
            "decision": clone(decision),
            "status": "denied" if not decision["allowed"] else "running",
            "started_at": utc_now(),
        }
        if run is not None:
            run.setdefault("tool_invocations", []).append(invocation)
        if not decision["allowed"]:
            if run is not None:
                invocation["finished_at"] = utc_now()
                self._append_event(run, "tool_denied", decision["reason"])
            raise CapabilityDenied(decision)
        if run is not None:
            run.setdefault("metrics", {})["tool_calls"] = count + 1
        try:
            result = self._run_bounded(
                lambda: self._execute_registered_tool(tool, arguments, run, user_id),
                timeout_seconds,
            )
        except BaseException as exc:
            invocation["status"] = "failed"
            invocation["error"] = {"type": type(exc).__name__, "message": str(exc)}
            invocation["finished_at"] = utc_now()
            if run is not None:
                run.setdefault("metrics", {})["tool_failures"] = int(run["metrics"].get("tool_failures", 0)) + 1
                self._append_event(run, "tool_failed", f"{tool_id}: {exc}")
            raise
        invocation["status"] = "succeeded"
        invocation["result"] = clone(result)
        invocation["finished_at"] = utc_now()
        if run is not None:
            self._append_event(run, "tool_succeeded", f"Invoked approved tool {tool_id}")
        return result

    def invoke_tool(
        self,
        tool_id: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        goal: str = "",
        contract_id: Optional[str] = None,
        role: str = "system",
        run: Optional[Dict[str, Any]] = None,
        user_id: str = "anonymous",
        timeout_seconds: Optional[float] = None,
        max_retries: int = 0,
        approval_granted: bool = False,
    ) -> Any:
        """Invoke an existing registered tool through governance and its schema."""
        state = self.store.snapshot()
        contract = run.get("contract_snapshot") if run else None
        contract = clone(contract) if contract else self._contract(contract_id, state)
        attempts = max(0, min(int(max_retries), 3)) + 1
        effective_timeout = timeout_seconds if timeout_seconds is not None else 10.0
        last_error: Optional[BaseException] = None
        for attempt in range(attempts):
            if run is not None:
                run.setdefault("metrics", {})["tool_attempts"] = int(run["metrics"].get("tool_attempts", 0)) + 1
                if attempt:
                    run["metrics"]["retries"] = int(run["metrics"].get("retries", 0)) + 1
                    self._append_event(run, "tool_retry", f"Retry {attempt} for {tool_id}")
            try:
                return self._invoke_tool_once(
                    tool_id,
                    clone(arguments or {}),
                    goal=goal,
                    contract=contract,
                    role=role,
                    run=run,
                    user_id=user_id,
                    timeout_seconds=effective_timeout,
                    approval_granted=approval_granted,
                )
            except CapabilityDenied:
                raise
            except (CapabilityTimeout, CapabilityExecutionError, OSError, ValueError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
        raise CapabilityExecutionError(str(last_error or "Tool execution failed"))

    def _apply_native_dsh_result(self, run: Dict[str, Any], native: Dict[str, Any], response: str) -> None:
        """Project native DSH evidence into the ADYA product run record."""
        graph = clone(native.get("graph")) if isinstance(native.get("graph"), dict) else {"nodes": [], "edges": []}
        for node in graph.get("nodes", []):
            node["native"] = True
            if node.get("status") == "completed":
                node["status"] = "destroyed"
                node["destroyed_at"] = node.get("destroyed_at") or utc_now()
        run["graph"] = graph
        native_events = native.get("lifecycleEvents")
        if isinstance(native_events, list):
            run.setdefault("events", []).extend(clone(native_events))
        capability_calls = native.get("capabilityCalls")
        if isinstance(capability_calls, list):
            run["tool_invocations"] = [
                {
                    "tool_id": f"{item.get('kind', 'capability')}:{item.get('id', '')}",
                    "status": item.get("status", "failed"),
                    "role": item.get("role"),
                    "arguments": {},
                    "result": clone(item.get("result")),
                    "error": clone(item.get("error")),
                }
                for item in capability_calls
            ]
            run.setdefault("metrics", {})["tool_calls"] = len(capability_calls)
            run["metrics"]["tool_attempts"] = len(capability_calls)
            run["metrics"]["tool_failures"] = sum(1 for item in capability_calls if item.get("status") != "succeeded")
        nodes = graph.get("nodes", [])
        started = int(native.get("agentsStarted", len(nodes)))
        ended = sum(1 for node in nodes if node.get("status") == "destroyed")
        metrics = run.setdefault("metrics", {})
        metrics["dsh_agents_started"] = started
        metrics["dsh_agents_ended"] = ended
        metrics["agents_spawned"] = started
        metrics["agents_destroyed"] = ended
        metrics["active_agents"] = sum(1 for node in nodes if node.get("status") == "active")
        run.setdefault("artifacts", {})["dsh_workflow"] = {
            "workflow_id": native.get("workflowId"),
            "stop_reason": native.get("stopReason"),
            "agents_started": started,
            "result": clone(native.get("workflowResult")),
            "native": True,
        }
        run["artifacts"]["review"] = clone(native.get("review") or {"status": "needs_review", "passed": False})
        run["artifacts"]["native_dsh"] = clone(native)
        run["governance_rejection"] = clone(native.get("governanceRejection"))
        run["status"] = str(native.get("status") or "needs_review")
        run["output"] = (
            f"Native DSH completed a governed run for: {run['goal']}\n\n"
            f"Native child agents started and disposed: {started} / {ended}\n"
            f"Approved native capabilities: {sum(1 for item in capability_calls or [] if item.get('status') == 'succeeded')}\n"
            f"Contract review: {run['artifacts']['review'].get('status', 'needs_review')}\n"
            f"Cleanup verified: {bool(native.get('cleanupVerified'))}\n"
            "Detailed graph, lifecycle events, capability calls, and governance rejection are in the run record."
        )
        if native.get("cleanupVerified"):
            self._append_event(run, "cleanup_verified", "No native DSH children remain active")
        else:
            self._append_event(run, "cleanup_invariant_failed", "Native DSH children remain active")
        self.memory.write(
            f"Run {run['id']} completed through native DSH workflow for goal '{run['goal']}'. Output: {response[:700]}",
            kind="run",
            run_id=str(run["id"]),
            tags=["run", run["status"], "dsh-native", str(run.get("contract_id", ""))],
            user_id=str(run.get("user_id", "anonymous")),
        )
        self._learn(str(run.get("goal", "")))
        self._append_event(run, "dsh_workflow_completed", f"Native DSH workflow completed with {run['status']}")

    def _submit_dsh(
        self,
        run: Dict[str, Any],
        state: Dict[str, Any],
        contract: Dict[str, Any],
        roles: List[str],
    ) -> Dict[str, Any]:
        """Execute one goal through the DSH-native ADYA composition."""
        self._track_active_run(run)
        try:
            registry_context: Dict[str, Any] = self._registry_context(state, contract)
            memory_hits = self.memory.search(str(run["goal"]), user_id=str(run.get("user_id", "anonymous")))
            registry_context["memory_hits"] = memory_hits
            run["memory_hits"] = clone(memory_hits)
            self._append_event(run, "memory_retrieved", f"Retrieved {len(memory_hits)} relevant memories for native DSH")
            response = self.model.execute_goal(
                str(run["goal"]),
                contract,
                roles,
                registry_context,
                str(run["id"]),
                str(run.get("user_id", "anonymous")),
            )
            run.setdefault("artifacts", {})["dsh_model_response"] = {
                "text": response[:2000],
                "truncated": len(response) > 2000,
            }
            native = self.model.last_dsh_execution.get("result")
            if not isinstance(native, dict):
                # Preserve the older injected boundary used by unit tests and
                # integrations that complete the ADYA run through the native
                # bridge methods directly. Real DSH runs always provide the
                # structured native result above.
                if run.get("status") == "running":
                    raise CapabilityExecutionError("DSH returned without native ADYA workflow evidence")
                run["output"] = response or run.get("output", "")
            else:
                self._apply_native_dsh_result(run, native, response)
            self._append_event(run, "dsh_model_completed", "Native DSH root agent returned a final response")
        except BaseException as exc:
            self._record_error(run, exc, "dsh_execution_error")
            run["status"] = "timed_out" if isinstance(exc, CapabilityTimeout) else "failed"
            run["output"] = f"Run {run['status']}: {exc}"
        finally:
            active_nodes = [
                node for node in run.get("graph", {}).get("nodes", [])
                if node.get("native") and node.get("status") == "active"
            ]
            for node in active_nodes:
                self._destroy(run, node, {"cleanup": True}, reason="cleanup after DSH failure")
            metrics = run.setdefault("metrics", {})
            metrics["agents_spawned"] = int(metrics.get("dsh_agents_started", 0))
            metrics["agents_destroyed"] = int(metrics.get("dsh_agents_ended", 0))
            metrics["active_agents"] = self._active_native_agents(run)
            run["finished_at"] = utc_now()
            if metrics["active_agents"]:
                run["status"] = "needs_review"
                self._append_event(run, "cleanup_invariant_failed", "Native DSH children remain active")
            else:
                self._append_event(run, "cleanup_verified", "No native DSH children remain active")
            self._untrack_active_run(str(run["id"]))
            self._persist_run(run)
        return clone(run)

    def submit(
        self,
        goal: str,
        contract_id: Optional[str] = None,
        requested_agents: Optional[int] = None,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        state = self.store.snapshot()
        actor = sanitize_user_id(user_id)
        contract = self._contract(contract_id, state)
        governance = self.governance.evaluate(goal, contract, requested_agents)
        governance = self.governance._record_decision(
            {
                **governance,
                "action": "submit_goal",
                "capability_type": "goal",
                "role": "user",
                "reason": "; ".join(governance.get("reasons", [])) or "goal allowed",
            },
            user_id=actor,
        )
        run: Dict[str, Any] = {
            "id": new_id("run"),
            "user_id": actor,
            "goal": goal.strip(),
            "contract_id": contract.get("id"),
            "contract_name": contract.get("name"),
            "contract_snapshot": clone(contract),
            "status": "blocked" if not governance["allowed"] else "running",
            "started_at": utc_now(),
            "finished_at": None,
            "model": self.model.name,
            "governance": governance,
            "graph": {"nodes": [], "edges": []},
            "events": [],
            "memory_hits": [],
            "artifacts": {},
            "tool_invocations": [],
            "skill_invocations": [],
            "errors": [],
            "output": "",
            "metrics": {
                "agents_spawned": 0,
                "agents_destroyed": 0,
                "active_agents": 0,
                "dsh_agents_started": 0,
                "dsh_agents_ended": 0,
                "tool_calls": 0,
                "tool_attempts": 0,
                "tool_failures": 0,
                "retries": 0,
            },
        }

        if not governance["allowed"]:
            self._append_event(run, "governance_blocked", governance.get("reason") or "; ".join(governance["reasons"]))
            run["finished_at"] = utc_now()
            self._persist_run(run)
            return clone(run)

        roles = self._select_roles(goal, contract, requested_agents)
        if self.model.dsh_enabled:
            return self._submit_dsh(run, state, contract, roles)
        policy = self.governance._policy(state, contract.get("governance_policy_id"))
        max_run_seconds = policy.get("max_run_seconds")
        deadline = time.monotonic() + float(max_run_seconds) if max_run_seconds else None
        parent: Optional[str] = None
        try:
            plan = self.invoke_skill(
                "goal-decomposition",
                {"goal": goal, "contract": contract},
                goal=goal,
                role="planner",
                run=run,
                user_id=actor,
                timeout_seconds=5,
            )
            run["artifacts"]["plan"] = plan
            planner = self._spawn(run, "planner", f"Decompose goal: {goal}", parent)
            run["metrics"]["agents_spawned"] += 1
            self._destroy(run, planner, plan)
            parent = planner["id"]

            memory_result = self.invoke_mcp(
                "mcp-memory",
                "memory.search",
                {"query": goal, "limit": 5},
                goal=goal,
                role="researcher" if "researcher" in roles else "planner",
                run=run,
                user_id=actor,
                timeout_seconds=5,
            )
            memory_hits = memory_result.get("items", [])
            run["memory_hits"] = memory_hits
            self._append_event(run, "memory_retrieved", f"Retrieved {len(memory_hits)} relevant memories")

            if "researcher" in roles:
                researcher = self._spawn(run, "researcher", "Find relevant prior knowledge and capabilities", parent)
                run["metrics"]["agents_spawned"] += 1
                research_result = {
                    "memory_hits": len(memory_hits),
                    "highlights": [item.get("text", "")[:180] for item in memory_hits[:3]],
                    "registry_context": self._registry_context(state, contract),
                }
                self._destroy(run, researcher, research_result)
                parent = researcher["id"]

            if "builder" in roles:
                builder = self._spawn(run, "builder", "Create the smallest verifiable implementation slice", parent)
                run["metrics"]["agents_spawned"] += 1
                tool_evidence = self.invoke_tool(
                    "goal.echo",
                    {"value": goal.strip()},
                    goal=goal,
                    role="builder",
                    run=run,
                    user_id=actor,
                    timeout_seconds=5,
                )
                synthesis_evidence = self.invoke_skill(
                    "evidence-synthesis",
                    {
                        "goal": goal,
                        "evidence": {
                            "memory_hits": len(memory_hits),
                            "tool_evidence": tool_evidence,
                            "registry_context": self._registry_context(state, contract),
                        },
                    },
                    goal=goal,
                    role="builder",
                    run=run,
                    user_id=actor,
                    timeout_seconds=5,
                )
                build_result = {
                    "deliverable": "A registry-backed, governed agent graph execution",
                    "approved_tool": "goal.echo",
                    "tool_evidence": tool_evidence,
                    "skill_evidence": synthesis_evidence,
                    "activated_capabilities": self._registry_context(state, contract),
                    "continual_learning": "Retrieval memory and feedback adaptation; no model-weight training.",
                }
                run["artifacts"]["build"] = build_result
                self._destroy(run, builder, build_result)
                parent = builder["id"]

            review = self.invoke_skill(
                "risk-review",
                {
                    "contract": contract,
                    "evidence": {
                        "governance_allowed": True,
                        "tool_invocations": run["tool_invocations"],
                        "active_agents": 0,
                    },
                },
                goal=goal,
                role="reviewer",
                run=run,
                user_id=actor,
                timeout_seconds=5,
            )
            review["evidence"] = {
                "tool_invocations": len(run["tool_invocations"]),
                "successful_tool_invocations": sum(
                    1 for item in run["tool_invocations"] if item.get("status") == "succeeded"
                ),
            }
            if "reviewer" in roles:
                reviewer = self._spawn(run, "reviewer", "Check governance and acceptance criteria", parent)
                run["metrics"]["agents_spawned"] += 1
                self._destroy(run, reviewer, review)
                parent = reviewer["id"]
            run["artifacts"]["review"] = review

            final_output = (
                f"Completed a governed multi-agent run for: {goal.strip()}\n\n"
                f"Agents spawned and destroyed: {run['metrics']['agents_spawned']} / "
                f"{run['metrics']['agents_spawned']}\n"
                f"Memory hits: {len(memory_hits)}\n"
                f"Approved tool evidence: {run['metrics']['tool_calls']} calls\n"
                f"Contract review: {review['status']}\n"
                "The run summary was written to retrieval memory for future executions."
            )
            if "synthesizer" in roles:
                synthesizer = self._spawn(run, "synthesizer", "Prepare the final handoff", parent)
                run["metrics"]["agents_spawned"] += 1
                generated = self.model.compose(
                    "You are the final synthesis agent in a governed demo harness. Never claim unverified evidence.",
                    self._synthesis_prompt(goal, plan, review, self._registry_context(state, contract)),
                )
                if generated:
                    final_output = generated
                self._destroy(run, synthesizer, {"output_length": len(final_output)})

            if deadline and time.monotonic() > deadline:
                raise CapabilityTimeout(f"Run exceeded {max_run_seconds}s")
            run["status"] = "completed" if review["passed"] else "needs_review"
            run["output"] = final_output
            self.invoke_tool(
                "memory.write",
                {
                    "text": f"Run {run['id']} completed for goal '{goal.strip()}'. Output: {final_output[:700]}",
                    "kind": "run",
                    "tags": ["run", run["status"], contract.get("id", "")],
                },
                goal=goal,
                role="synthesizer" if "synthesizer" in roles else "system",
                run=run,
                user_id=actor,
                timeout_seconds=5,
            )
            self._learn(goal)
            self._append_event(run, "run_completed", f"Run completed with status {run['status']}")
        except BaseException as exc:
            self._record_error(run, exc, "timeout" if isinstance(exc, CapabilityTimeout) else "execution_error")
            run["status"] = "timed_out" if isinstance(exc, CapabilityTimeout) else "failed"
            run["output"] = f"Run {run['status']}: {exc}"
        finally:
            for node in run["graph"]["nodes"]:
                if node.get("status") == "active":
                    self._destroy(run, node, {"cleanup": True}, reason="cleanup after failure")
            run["metrics"]["agents_destroyed"] = sum(
                1 for node in run["graph"]["nodes"] if node.get("status") == "destroyed"
            )
            run["metrics"]["active_agents"] = sum(
                1 for node in run["graph"]["nodes"] if node.get("status") == "active"
            )
            run["finished_at"] = utc_now()
            if run["metrics"]["active_agents"]:
                run["status"] = "needs_review"
                self._append_event(run, "cleanup_invariant_failed", "Temporary agents remain active")
            else:
                self._append_event(run, "cleanup_verified", "No temporary agents remain active")
            self._persist_run(run)
        return clone(run)

    def _learn(self, goal: str) -> None:
        tokens = [token for token in re.findall(r"[a-zA-Z0-9]{4,}", goal.lower()) if token not in {"with", "that", "this", "from"}]

        def mutate(state: Dict[str, Any]) -> None:
            patterns = state.setdefault("learned_patterns", {})
            for token in set(tokens):
                patterns[token] = int(patterns.get(token, 0)) + 1

        self.store.update(mutate)

    def _persist_run(self, run: Dict[str, Any]) -> None:
        def mutate(state: Dict[str, Any]) -> None:
            state.setdefault("runs", []).insert(0, run)
            state["runs"] = state["runs"][:50]

        self.store.update(mutate)

    def create_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Policy name is required")
        policy = clone(payload)
        policy["id"] = str(policy.get("id") or slugify(name))
        policy["name"] = name
        policy["version"] = str(policy.get("version") or "0.1.0")
        policy["deny_patterns"] = [str(item).strip() for item in policy.get("deny_patterns", []) if str(item).strip()]
        policy["approval_required_for"] = [str(item).strip() for item in policy.get("approval_required_for", []) if str(item).strip()]
        policy["deny_tool_ids"] = [str(item).strip() for item in policy.get("deny_tool_ids", []) if str(item).strip()]
        policy["max_tool_calls"] = max(0, min(int(policy.get("max_tool_calls") or 20), 1000))
        policy["max_run_seconds"] = max(0, min(float(policy.get("max_run_seconds") or 30), 3600))
        policy["status"] = str(policy.get("status") or "active")
        policy["created_at"] = policy.get("created_at") or utc_now()

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            policies = state.setdefault("governance", {}).setdefault("policies", [])
            if any(item.get("id") == policy["id"] for item in policies):
                raise ValueError(f"Policy already exists: {policy['id']}")
            policies.append(policy)
            return policy

        return self.store.update(mutate)

    def create_contract(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Contract name is required")
        contract = clone(payload)
        contract["id"] = str(contract.get("id") or slugify(name))
        contract["name"] = name
        contract["version"] = str(contract.get("version") or "0.1.0")
        contract["governance_policy_id"] = str(contract.get("governance_policy_id") or "default-safe")
        contract["required_skills"] = [str(item).strip() for item in contract.get("required_skills", []) if str(item).strip()]
        contract["allowed_tools"] = [str(item).strip() for item in contract.get("allowed_tools", []) if str(item).strip()]
        contract["agent_roles"] = [str(item).strip() for item in contract.get("agent_roles", []) if str(item).strip()]
        contract["role_specs"] = clone(contract.get("role_specs") or {})
        contract["acceptance_criteria"] = [str(item).strip() for item in contract.get("acceptance_criteria", []) if str(item).strip()]
        contract["max_agents"] = max(1, min(int(contract.get("max_agents") or 5), 12))
        contract["status"] = str(contract.get("status") or "active")
        contract["created_at"] = contract.get("created_at") or utc_now()

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            contracts = state.setdefault("contracts", [])
            if any(item.get("id") == contract["id"] for item in contracts):
                raise ValueError(f"Contract already exists: {contract['id']}")
            contracts.append(contract)
            return contract

        return self.store.update(mutate)

    def feedback(
        self,
        run_id: str,
        text: str,
        score: Optional[int] = None,
        user_id: str = "anonymous",
        is_admin: bool = False,
    ) -> Dict[str, Any]:
        clean = text.strip()
        if not clean:
            raise ValueError("Feedback text is required")
        if score is not None:
            score = max(1, min(5, int(score)))
        return self.memory.record_feedback(run_id, clean, score, user_id=user_id, is_admin=is_admin)

    def get_run(self, run_id: str, user_id: str = "anonymous", is_admin: bool = False) -> Dict[str, Any]:
        actor = sanitize_user_id(user_id)
        run = next((item for item in self.store.snapshot().get("runs", []) if item.get("id") == run_id), None)
        if not run:
            raise ValueError("Run not found")
        if not is_admin and run.get("user_id", "anonymous") != actor:
            raise PermissionError("Run is not visible to this user")
        return clone(run)

    def view(self, user_id: str = "anonymous", is_admin: bool = False) -> Dict[str, Any]:
        state = self.store.snapshot()
        actor = sanitize_user_id(user_id)
        registries = state["registries"] if is_admin else {
            kind: [clone(item) for item in entries if item.get("status") != "disabled"]
            for kind, entries in state["registries"].items()
        }
        all_runs = state.get("runs", [])
        runs = all_runs if is_admin else [item for item in all_runs if item.get("user_id", "anonymous") == actor]
        all_memory = state.get("memory", [])
        memory = all_memory if is_admin else [
            item for item in all_memory if item.get("user_id", "anonymous") in {actor, "shared"}
        ]
        contracts = state.get("contracts", [])
        public_contracts = contracts if is_admin else [
            {
                key: clone(contract.get(key))
                for key in ("id", "name", "version", "description", "max_agents", "acceptance_criteria", "status")
                if key in contract
            }
            for contract in contracts
            if contract.get("status") != "disabled"
        ]
        return {
            "runtime": self.runtime_info(),
            "overview": {
                "mcps": len(registries.get("mcps", [])),
                "skills": len(registries.get("skills", [])),
                "tools": len(registries.get("tools", [])),
                "policies": len(state.get("governance", {}).get("policies", [])),
                "contracts": len(state.get("contracts", [])),
                "runs": len(runs),
                "memory_items": len(memory),
                "audit_events": len(state.get("audit_events", [])) if is_admin else 0,
                "errors": len(state.get("errors", [])) if is_admin else 0,
                "active_agents": sum(
                    1
                    for run in runs
                    for node in run.get("graph", {}).get("nodes", [])
                    if node.get("status") == "active"
                ),
            },
            "registries": registries,
            "policies": state.get("governance", {}).get("policies", []) if is_admin else [],
            "contracts": public_contracts,
            "runs": runs[:12],
            "memory": memory[:12],
            "feedback": state.get("feedback", [])[:12] if is_admin else [
                item for item in state.get("feedback", []) if item.get("user_id") == actor
            ][:12],
            "learned_patterns": state.get("learned_patterns", {}) if is_admin else {},
            "audit_events": state.get("audit_events", [])[:50] if is_admin else [],
            "errors": state.get("errors", [])[:50] if is_admin else [],
        }


def build_service(data_file: Optional[str] = None) -> AgentRuntime:
    return AgentRuntime(StateStore(data_file))
