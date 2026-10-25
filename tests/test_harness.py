import os
import sys
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

# Always test the implementation shipped in adya-share, not similarly named
# files that may exist in the repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADYA_SHARE = PROJECT_ROOT / "adya-share"
sys.path.insert(0, str(ADYA_SHARE))
import tempfile
import unittest
from unittest.mock import patch

from server import HarnessHandler
from harness import AgentRuntime, CapabilityDenied, StateStore


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(HarnessHandler)

    def test_admin_denied_when_token_is_not_configured(self):
        self.handler.headers = {}
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.handler._is_admin())

    def test_admin_denied_when_token_is_not_configured_even_with_empty_header(self):
        self.handler.headers = {"X-Admin-Token": ""}
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.handler._is_admin())

    def test_admin_denied_for_wrong_token(self):
        with patch.dict(os.environ, {"ADYA_ADMIN_TOKEN": "correct-token"}, clear=True):
            self.handler.headers = {"X-Admin-Token": "wrong-token"}
            self.assertFalse(self.handler._is_admin())

    def test_admin_allowed_for_matching_token(self):
        with patch.dict(os.environ, {"ADYA_ADMIN_TOKEN": "correct-token"}, clear=True):
            self.handler.headers = {"X-Admin-Token": "correct-token"}
            self.assertTrue(self.handler._is_admin())


class RuntimeSmokeTests(unittest.TestCase):
    def test_mocked_dsh_boundary_uses_native_lifecycle_and_existing_registries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ADYA_LLM_MODE": "dsh"},
            clear=True,
        ):
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))

            def fake_dsh(goal, contract, roles, _registry_context, run_id, user_id):
                runtime.start_dsh_workflow({"run_id": run_id, "user_id": user_id, "roles": roles})
                for role in roles:
                    runtime.admit_dsh_agent({"run_id": run_id, "user_id": user_id, "role": role})
                for index, role in enumerate(roles):
                    child_id = f"dsh-child-{index}"
                    runtime.record_dsh_lifecycle({
                        "run_id": run_id,
                        "user_id": user_id,
                        "event": "started",
                        "workflow_id": "workflow-test",
                        "child_id": child_id,
                        "role": role,
                    })
                    if role == "planner":
                        runtime.invoke_dsh_capability({
                            "run_id": run_id,
                            "user_id": user_id,
                            "kind": "skill",
                            "id": "goal-decomposition",
                            "role": role,
                            "arguments": {"goal": goal, "contract": contract},
                        })
                    if role == "builder":
                        runtime.invoke_dsh_capability({
                            "run_id": run_id,
                            "user_id": user_id,
                            "kind": "tool",
                            "id": "goal.echo",
                            "role": role,
                            "arguments": {"value": goal},
                        })
                    runtime.record_dsh_lifecycle({
                        "run_id": run_id,
                        "user_id": user_id,
                        "event": "ended",
                        "workflow_id": "workflow-test",
                        "child_id": child_id,
                        "role": role,
                        "outcome": "completed",
                    })
                completed = runtime.complete_dsh_run({
                    "run_id": run_id,
                    "user_id": user_id,
                    "workflow_id": "workflow-test",
                    "workflow_stop_reason": "completed",
                    "agents_started": len(roles),
                    "workflow_result": {"verified": True},
                })
                return completed["output"]

            with patch.object(runtime.model, "execute_goal", side_effect=fake_dsh):
                result = runtime.submit("Build a governed DSH registry demo", user_id="alice")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["metrics"]["dsh_agents_started"], result["metrics"]["dsh_agents_ended"])
            self.assertEqual(result["metrics"]["active_agents"], 0)
            self.assertTrue(result["artifacts"]["dsh_workflow"]["result"]["verified"])
            self.assertTrue(any(item["type"] == "dsh_agent_started" for item in result["events"]))
            self.assertTrue(any(item["type"] == "cleanup_verified" for item in result["events"]))

    def test_dsh_mode_fails_closed_without_native_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ADYA_LLM_MODE": "dsh", "ADYA_DSH_HOME": os.path.join(tmp, "dsh-home")},
            clear=True,
        ):
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            result = runtime.submit("Build a governed registry demo", user_id="alice")
            self.assertEqual(result["status"], "failed")
            self.assertIn("native dsh", result["output"].lower())
            self.assertTrue(any(item["code"] == "dsh_execution_error" for item in result["errors"]))
            self.assertTrue(any(item["type"] == "cleanup_verified" for item in result["events"]))

    @unittest.skipUnless(
        os.getenv("ADYA_DSH_ACCEPTANCE") == "1",
        "set ADYA_DSH_ACCEPTANCE=1 with Node and the checked-out DSH source runtime to run the native acceptance path",
    )
    def test_real_dsh_native_vertical_slice_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "ADYA_LLM_MODE": "dsh",
                "ADYA_DSH_HOME": os.path.join(tmp, "dsh-home"),
                "ADYA_DSH_TIMEOUT_SECONDS": "120",
            },
            clear=False,
        ):
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            result = runtime.submit("Build a governed DSH registry demo", user_id="alice")
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["artifacts"].get("native_dsh", {}).get("native"))
            self.assertGreaterEqual(result["metrics"].get("dsh_agents_started", 0), 2)
            self.assertEqual(result["metrics"].get("active_agents"), 0)

    def test_governance_blocks_empty_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            result = runtime.submit("   ")
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["governance"]["allowed"])

    def test_valid_goal_completes_and_agents_are_destroyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            result = runtime.submit("Create a small example project")
            self.assertEqual(result["status"], "completed")
            self.assertGreater(result["metrics"]["agents_spawned"], 0)
            self.assertEqual(result["metrics"]["agents_spawned"], result["metrics"]["agents_destroyed"])
            self.assertEqual(result["metrics"]["active_agents"], 0)
            self.assertTrue(runtime.store.snapshot()["memory"])

    def test_vertical_slice_invokes_real_registered_tools_and_reviews_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            result = runtime.submit("Build a registry-backed governed memory demo", user_id="alice")
            self.assertEqual(result["status"], "completed")
            self.assertTrue(any(item["tool_id"] == "goal.echo" and item["status"] == "succeeded" for item in result["tool_invocations"]))
            self.assertTrue(result["artifacts"]["build"]["tool_evidence"]["verified"])
            self.assertEqual(result["artifacts"]["review"]["status"], "passed")
            self.assertTrue(any(item["type"] == "cleanup_verified" for item in result["events"]))
            self.assertTrue(any(item.get("allowed") for item in runtime.store.snapshot()["audit_events"]))

    def test_governance_and_contract_block_out_of_contract_tool_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            with self.assertRaises(CapabilityDenied) as raised:
                runtime.invoke_tool(
                    "goal.echo",
                    {"value": "should be denied"},
                    goal="safe review",
                    contract_id="research-build-v1",
                    role="reviewer",
                    user_id="alice",
                )
            self.assertFalse(raised.exception.decision["allowed"])
            self.assertIn("reviewer", raised.exception.decision["reason"])
            self.assertFalse(runtime.store.snapshot()["audit_events"][0]["allowed"])

    def test_registered_skill_and_mcp_are_executable_through_existing_registries(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            plan = runtime.invoke_skill(
                "goal-decomposition",
                {"goal": "Inspect registry memory", "contract": runtime.store.snapshot()["contracts"][0]},
                goal="Inspect registry memory",
                role="planner",
                contract_id="research-build-v1",
                user_id="alice",
            )
            self.assertTrue(plan["steps"])
            runtime.memory.write("approved registry fact", user_id="alice")
            result = runtime.invoke_mcp(
                "mcp-memory",
                "memory.search",
                {"query": "approved registry fact"},
                goal="Inspect registry memory",
                role="researcher",
                contract_id="research-build-v1",
                user_id="alice",
            )
            self.assertEqual(result["count"], 1)

    def test_schema_validation_and_disabled_registry_entry_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            with self.assertRaises(ValueError):
                runtime.invoke_tool(
                    "goal.echo",
                    {},
                    goal="test schema",
                    contract_id="research-build-v1",
                    role="builder",
                )
            runtime.registries.update("tools", "goal.echo", {"status": "disabled"})
            with self.assertRaises(CapabilityDenied):
                runtime.invoke_tool(
                    "goal.echo",
                    {"value": "disabled"},
                    goal="test disabled tool",
                    contract_id="research-build-v1",
                    role="builder",
                )

    def test_failure_cleanup_leaves_no_active_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            runtime.registries.update("tools", "goal.echo", {"executor": "missing.executor"})
            result = runtime.submit("Build a governed registry demo", user_id="alice")
            self.assertIn(result["status"], {"failed", "timed_out"})
            self.assertEqual(result["metrics"]["active_agents"], 0)
            self.assertEqual(result["metrics"]["agents_spawned"], result["metrics"]["agents_destroyed"])
            self.assertTrue(result["errors"])

    def test_memory_survives_restart_and_feedback_is_user_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            first = AgentRuntime(StateStore(path))
            first.memory.write("approved fact for Alice", user_id="alice", kind="approved_fact")
            second = AgentRuntime(StateStore(path))
            self.assertEqual(len(second.memory.search("approved fact", user_id="alice")), 1)
            self.assertEqual(second.memory.search("approved fact", user_id="bob"), [])
            run = second.submit("Build a small memory demo", user_id="alice")
            with self.assertRaises(PermissionError):
                second.feedback(run["id"], "not your run", 1, user_id="bob")
            second.feedback(run["id"], "useful result", 5, user_id="alice")
            self.assertTrue(any(item["kind"] == "feedback" for item in second.store.snapshot()["memory"]))


class HttpPlaneTests(unittest.TestCase):
    def _request(self, server, method, path, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_user_runs_are_owned_and_admin_routes_are_server_protected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ADYA_ADMIN_TOKEN": "admin-secret"}, clear=True):
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            HarnessHandler.runtime = runtime
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), HarnessHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, run = self._request(
                    server,
                    "POST",
                    "/api/runs",
                    {"goal": "Build a small registry demo"},
                    {"X-User-Id": "alice"},
                )
                self.assertEqual(status, 201)
                self.assertEqual(run["user_id"], "alice")
                denied_status, _ = self._request(server, "GET", f"/api/runs/{run['id']}", headers={"X-User-Id": "bob"})
                self.assertEqual(denied_status, 403)
                allowed_status, _ = self._request(server, "GET", f"/api/runs/{run['id']}", headers={"X-User-Id": "alice"})
                self.assertEqual(allowed_status, 200)
                admin_denied, _ = self._request(server, "POST", "/api/contracts", {"name": "blocked"})
                self.assertEqual(admin_denied, 403)
                admin_allowed, contract = self._request(
                    server,
                    "POST",
                    "/api/contracts",
                    {"name": "admin-created", "agent_roles": ["planner"], "max_agents": 1},
                    {"X-Admin-Token": "admin-secret"},
                )
                self.assertEqual(admin_allowed, 201)
                self.assertEqual(contract["name"], "admin-created")
                audit_denied, _ = self._request(server, "GET", "/api/admin/audit")
                self.assertEqual(audit_denied, 403)
                audit_allowed, audit = self._request(server, "GET", "/api/admin/audit", headers={"X-Admin-Token": "admin-secret"})
                self.assertEqual(audit_allowed, 200)
                self.assertTrue(audit)
            finally:
                server.shutdown()
                server.server_close()

    def test_admin_endpoint_fails_closed_when_environment_token_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            runtime = AgentRuntime(StateStore(os.path.join(tmp, "state.json")))
            HarnessHandler.runtime = runtime
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), HarnessHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, payload = self._request(server, "POST", "/api/reset", {})
                self.assertEqual(status, 403)
                self.assertIn("token required", payload["error"].lower())
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
