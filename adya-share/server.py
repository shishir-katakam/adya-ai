"""HTTP entry point for the ADYA Agent Harness demo."""

from __future__ import annotations

import json
import hmac
import mimetypes
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from harness import (
    APP_NAME,
    AgentRuntime,
    CapabilityDenied,
    StateStore,
    sanitize_user_id,
)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = ROOT.parent / "static"


class HarnessHandler(BaseHTTPRequestHandler):
    runtime: AgentRuntime

    server_version = "ADYA-Harness/0.2"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the Cloud Run log useful without leaking request bodies.
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 1_000_000:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Request body must be a JSON object")
        return parsed

    def _is_admin(self) -> bool:
        expected = os.getenv("ADYA_ADMIN_TOKEN", "").strip()
        supplied = self.headers.get("X-Admin-Token", "")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _user_id(self) -> str:
        return sanitize_user_id(self.headers.get("X-User-Id", "anonymous"))

    def _require_admin(self) -> bool:
        if self._is_admin():
            return True
        self._error("Admin token required for this action", HTTPStatus.FORBIDDEN)
        return False

    def _require_admin_for_get(self) -> bool:
        if self._is_admin():
            return True
        self._error("Admin token required for this action", HTTPStatus.FORBIDDEN)
        return False

    def _require_dsh_bridge(self) -> bool:
        expected = self.runtime.model.dsh_bridge_token
        supplied = self.headers.get("X-ADYA-DSH-Token", "")
        local = self.client_address[0] in {"127.0.0.1", "::1"}
        if expected and local and hmac.compare_digest(supplied, expected):
            return True
        self._error("DSH bridge access denied", HTTPStatus.FORBIDDEN)
        return False

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (STATIC_DIR / unquote(relative)).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._error("Not found", HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self._error("Not found", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send_text(candidate.read_bytes(), f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token, X-User-Id")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send_json({"ok": True, "runtime": self.runtime.runtime_info()})
            return
        if path == "/api/state":
            self._send_json(self.runtime.view(self._user_id(), self._is_admin()))
            return
        if path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            try:
                self._send_json(self.runtime.get_run(run_id, self._user_id(), self._is_admin()))
            except PermissionError as exc:
                self._error(str(exc), HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._error(str(exc), HTTPStatus.NOT_FOUND)
            return
        if path == "/api/admin/audit":
            if not self._require_admin_for_get():
                return
            self._send_json(self.runtime.view(self._user_id(), True).get("audit_events", []))
            return
        if path == "/api/admin/errors":
            if not self._require_admin_for_get():
                return
            self._send_json(self.runtime.view(self._user_id(), True).get("errors", []))
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
            if path.startswith("/internal/dsh/"):
                if not self._require_dsh_bridge():
                    return
                if path == "/internal/dsh/agent-admit":
                    self._send_json(self.runtime.admit_dsh_agent(payload), HTTPStatus.OK)
                    return
                if path == "/internal/dsh/workflow-start":
                    self._send_json(self.runtime.start_dsh_workflow(payload), HTTPStatus.OK)
                    return
                if path == "/internal/dsh/lifecycle":
                    self._send_json(self.runtime.record_dsh_lifecycle(payload), HTTPStatus.OK)
                    return
                if path == "/internal/dsh/capability":
                    self._send_json(self.runtime.invoke_dsh_capability(payload), HTTPStatus.OK)
                    return
                if path == "/internal/dsh/complete":
                    self._send_json(self.runtime.complete_dsh_run(payload), HTTPStatus.OK)
                    return
                self._error("Not found", HTTPStatus.NOT_FOUND)
                return
            if path == "/api/runs":
                goal = str(payload.get("goal", ""))
                contract_id = payload.get("contract_id")
                requested_agents = payload.get("requested_agents")
                if requested_agents is not None:
                    requested_agents = int(requested_agents)
                self._send_json(
                    self.runtime.submit(goal, contract_id, requested_agents, user_id=self._user_id()),
                    HTTPStatus.CREATED,
                )
                return
            if path == "/api/feedback":
                run_id = str(payload.get("run_id", ""))
                text = str(payload.get("text", ""))
                score = payload.get("score")
                self._send_json(
                    self.runtime.feedback(run_id, text, score, user_id=self._user_id(), is_admin=self._is_admin()),
                    HTTPStatus.CREATED,
                )
                return
            if path == "/api/reset":
                if not self._require_admin():
                    return
                self.runtime.store.reset()
                self._send_json(self.runtime.view(self._user_id(), True))
                return
            if path.startswith("/api/registries/") and path.count("/") == 3:
                if not self._require_admin():
                    return
                kind = path.rsplit("/", 1)[-1]
                self._send_json(self.runtime.registries.add(kind, payload), HTTPStatus.CREATED)
                return
            if path.startswith("/api/tools/"):
                if not self._require_admin():
                    return
                tool_id = unquote(path.rsplit("/", 1)[-1])
                result = self.runtime.invoke_tool(
                    tool_id,
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                    goal=str(payload.get("goal", "admin tool test")),
                    contract_id=payload.get("contract_id"),
                    role="admin",
                    user_id=self._user_id(),
                    timeout_seconds=float(payload.get("timeout_seconds", 10)),
                    max_retries=int(payload.get("max_retries", 0)),
                    approval_granted=True,
                )
                self._send_json({"tool_id": tool_id, "result": result}, HTTPStatus.OK)
                return
            if path.startswith("/api/skills/"):
                if not self._require_admin():
                    return
                skill_id = unquote(path.rsplit("/", 1)[-1])
                result = self.runtime.invoke_skill(
                    skill_id,
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                    goal=str(payload.get("goal", "admin skill test")),
                    contract_id=payload.get("contract_id"),
                    role="admin",
                    user_id=self._user_id(),
                    timeout_seconds=float(payload.get("timeout_seconds", 10)),
                )
                self._send_json({"skill_id": skill_id, "result": result}, HTTPStatus.OK)
                return
            if path.startswith("/api/mcps/"):
                if not self._require_admin():
                    return
                parts = [unquote(part) for part in path.split("/") if part]
                if len(parts) != 4:
                    raise ValueError("Expected /api/mcps/{id}/{capability}")
                result = self.runtime.invoke_mcp(
                    parts[2],
                    parts[3],
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                    goal=str(payload.get("goal", "admin MCP test")),
                    contract_id=payload.get("contract_id"),
                    role="admin",
                    user_id=self._user_id(),
                    timeout_seconds=float(payload.get("timeout_seconds", 10)),
                )
                self._send_json({"mcp_id": parts[2], "capability": parts[3], "result": result}, HTTPStatus.OK)
                return
            if path.startswith("/api/memory/") and path.endswith("/correct"):
                if not self._require_admin():
                    return
                memory_id = unquote(path.split("/")[-2])
                self._send_json(self.runtime.memory.correct(memory_id, str(payload.get("text", "")), payload.get("tags")), HTTPStatus.OK)
                return
            if path == "/api/memory/search":
                query = str(payload.get("query", ""))
                self._send_json(self.runtime.memory.search(query, int(payload.get("limit", 5)), self._user_id()), HTTPStatus.OK)
                return
            if path == "/api/governance/policies":
                if not self._require_admin():
                    return
                self._send_json(self.runtime.create_policy(payload), HTTPStatus.CREATED)
                return
            if path == "/api/contracts":
                if not self._require_admin():
                    return
                self._send_json(self.runtime.create_contract(payload), HTTPStatus.CREATED)
                return
            self._error("Not found", HTTPStatus.NOT_FOUND)
        except CapabilityDenied as exc:
            self._send_json({"error": str(exc), "decision": exc.decision}, HTTPStatus.FORBIDDEN)
        except PermissionError as exc:
            self._error(str(exc), HTTPStatus.FORBIDDEN)
        except (ValueError, TypeError) as exc:
            self._error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - last-resort API boundary
            self._error(f"Internal error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/memory/"):
            if not self._require_admin():
                return
            memory_id = unquote(path.rsplit("/", 1)[-1])
            if not self.runtime.memory.delete(memory_id):
                self._error("Memory item not found", HTTPStatus.NOT_FOUND)
                return
            self._send_json({"deleted": True, "id": memory_id})
            return
        if not path.startswith("/api/registries/"):
            self._error("Not found", HTTPStatus.NOT_FOUND)
            return
        if not self._require_admin():
            return
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 4:
            self._error("Expected /api/registries/{kind}/{id}", HTTPStatus.BAD_REQUEST)
            return
        kind, entry_id = parts[2], parts[3]
        try:
            deleted = self.runtime.registries.delete(kind, entry_id)
        except ValueError as exc:
            self._error(str(exc), HTTPStatus.BAD_REQUEST)
            return
        if not deleted:
            self._error("Registry entry not found", HTTPStatus.NOT_FOUND)
            return
        self._send_json({"deleted": True, "id": entry_id})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/registries/"):
            self._error("Not found", HTTPStatus.NOT_FOUND)
            return
        if not self._require_admin():
            return
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 4:
            self._error("Expected /api/registries/{kind}/{id}", HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = self._read_json()
            self._send_json(self.runtime.registries.update(parts[2], parts[3], payload))
        except (ValueError, TypeError) as exc:
            self._error(str(exc), HTTPStatus.BAD_REQUEST)


def create_server(host: str = "0.0.0.0", port: Optional[int] = None) -> ThreadingHTTPServer:
    selected_port = int(os.getenv("PORT", "8080")) if port is None else port
    data_file = os.getenv("ADYA_DATA_FILE", "data/state.json")
    legacy_bridge = os.getenv("ADYA_DSH_LEGACY_BRIDGE", "").strip() == "1"
    bridge_token = None
    if legacy_bridge:
        bridge_token = os.getenv("ADYA_DSH_BRIDGE_TOKEN", "").strip() or secrets.token_urlsafe(32)
    runtime = AgentRuntime(StateStore(data_file), dsh_bridge_token=bridge_token)
    server = ThreadingHTTPServer((host, selected_port), HarnessHandler)
    if legacy_bridge and bridge_token is not None:
        runtime.configure_dsh_bridge(
            f"http://127.0.0.1:{server.server_address[1]}/internal/dsh",
            bridge_token,
        )
    HarnessHandler.runtime = runtime
    return server


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = create_server(port=port)
    print(f"{APP_NAME} listening on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
