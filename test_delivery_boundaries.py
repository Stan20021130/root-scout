"""Regression checks for report, review, sandbox, and trace boundaries."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import native_adapter
import native_transport
import repo_checks
import repo_tasks
import root_scout


class DeliveryBoundaryTests(unittest.TestCase):
    def state(self):
        evidence = {"id": "T0001", "version_id": "original", "input": "2\n-4 -2\n", "expected": "-2\n",
                    "trusted_expected": True, "legal_input_verified": True, "expected_source": "reviewed maximum",
                    "legal_input_source": "two integers in the stated range", "source_kind": "caller_trusted",
                    "result": {"status": "ok", "returncode": 0, "stdout": "0\n"}}
        return {"id": "case", "request_snapshot": {"source": "print(0)\n", "language": "python3"},
                "run_configuration": {"protocol_schema_version": 5, "runtime": {"available": True}},
                "stop_reason": "replay_without_saved_actions", "evidence": [evidence],
                "test_plan": [{"input": evidence["input"], "expected": evidence["expected"]}],
                "candidates": [], "tool_actions": []}

    def test_counterexample_survives_without_a_model_candidate(self):
        state = self.state()
        for view in (root_scout.probe_result(state), root_scout.v2_result(state)):
            self.assertEqual(view["status"], "counterexample_found")
            self.assertEqual(view["stop_reason"], "replay_without_saved_actions")
            self.assertEqual(view["findings"], [])
            self.assertEqual((view["counterexamples"][0]["expected"], view["counterexamples"][0]["actual"]), ("-2\n", "0\n"))
            self.assertIn("replay_without_saved_actions", view["scope"]["gaps"])

    def test_incomplete_or_failed_run_never_looks_clean(self):
        state = self.state()
        state["evidence"][0]["result"]["stdout"] = "-2\n"
        for stop, status in (("no_bug_found_within_scope", "no_failure_observed"),
                             ("replay_without_saved_actions", "inconclusive"), ("failed", "error"),
                             ("environment_blocked", "environment_blocked")):
            state["stop_reason"] = stop
            self.assertEqual(root_scout.v2_result(state)["status"], status)

    def test_unmatched_rules_do_not_certify_a_semantic_bug_as_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wallet.py").write_text("def withdraw(balance, amount):\n    return balance + amount\n", encoding="utf-8")
            diff = ("--- a/wallet.py\n+++ b/wallet.py\n@@ -1,2 +1,2 @@\n def withdraw(balance, amount):\n"
                    "-    return balance - amount\n+    return balance + amount\n")
            result = repo_tasks.execute_review_diff({"task_kind": "review_diff", "repo_root": str(root), "diff": diff,
                                                     "requirements": "withdraw subtracts amount from balance"})
            self.assertEqual(result["findings_count"], 0)
            self.assertFalse(result["fast_path"]["verified_clean"])

    def test_diff_line_number_ignores_removed_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_text("def f():\n    try: return 1\n    except: return 0\n", encoding="utf-8")
            diff = ("--- a/example.py\n+++ b/example.py\n@@ -1,2 +1,3 @@\n def f():\n-    return 1\n"
                    "+    try: return 1\n+    except: return 0\n")
            result = repo_tasks.execute_review_diff({"task_kind": "review_diff", "repo_root": str(root), "diff": diff})
            self.assertEqual(result["findings"][0]["line"], 3)
            self.assertEqual(result["findings"][0]["status"], "unverified")

    def test_missing_docker_does_not_execute_on_host(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(repo_checks.subprocess, "Popen", side_effect=FileNotFoundError("docker missing")):
            result = repo_checks.RepoCheckRunner(Path(directory)).run_check("probe", ["python3", "-c", "print(1)"])
            self.assertEqual(result["classification"], "environment_blocked")
            self.assertFalse(result["passed"])
            self.assertFalse((Path(directory) / ".root-scout").exists())

    def test_native_default_has_no_trace_directory(self):
        response = {"candidates": [], "updates": [], "actions": [], "next_question": "", "stop": True}
        message = {"role": "assistant", "content": json.dumps(response), "reasoning_content": "fixture", "tool_calls": []}
        call = {"status": "ok", "done": True, "finish_reason": "stop", "response_bytes": 1,
                "reasoning_chars": 7, "reasoning_sha256": root_scout.sha("fixture"),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        env = {"OPENAI_MODEL": "fixture", "OPENAI_API_KEY": "fixture", "ROOT_SCOUT_OUTPUT_POLICY": "adaptive",
               "ROOT_SCOUT_MODEL_OUTPUT_LIMIT": "128000"}
        with patch.dict(os.environ, env, clear=True), patch.object(native_transport, "request", return_value=(message, call)) as request:
            client = native_adapter.make_client(root_scout)({"run_configuration": {"protocol_schema_version": 5}})
            client.ask("EXPLORE_V2", "{}", root_scout.Budget(time.monotonic(), max_tokens=None))
        self.assertIsNone(request.call_args.args[1])

    def test_native_transport_writes_trace_only_when_requested(self):
        events = [{"choices": [{"index": 0, "delta": {"reasoning_content": "fixture reasoning"}}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}]
        wire = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
        with patch.object(native_transport.urllib.request, "urlopen", return_value=io.BytesIO(wire)), \
             patch.object(native_transport, "Path", side_effect=AssertionError("unexpected trace path")):
            _, call = native_transport.request({"messages": []}, None, "fixture")
            self.assertEqual(call["status"], "ok")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(native_transport.urllib.request, "urlopen", return_value=io.BytesIO(wire)):
            _, call = native_transport.request({"messages": []}, Path(directory), "fixture")
            self.assertEqual(call["status"], "ok")
            self.assertIn("fixture reasoning", (Path(directory) / "assistant.json").read_text(encoding="utf-8"))


@unittest.skipUnless(os.environ.get("ROOT_SCOUT_INTEGRATION") == "1", "requires real Docker sandbox")
class RepoDockerBoundaryTests(unittest.TestCase):
    def test_readonly_repository_no_network_and_no_host_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.txt"
            original.write_text("unchanged", encoding="utf-8")
            script = ("import json,os,socket\nfrom pathlib import Path\n"
                      "try:\n Path('/workspace/original.txt').write_text('changed'); readonly=False\n"
                      "except OSError: readonly=True\n"
                      "s=socket.socket(); s.settimeout(1)\n"
                      "print(json.dumps({'uid':os.getuid(),'readonly':readonly,"
                      "'network_blocked':s.connect_ex(('1.1.1.1',53)) != 0,"
                      "'host_secret':os.environ.get('ROOT_SCOUT_TEST_HOST_SECRET')}))")
            with patch.dict(os.environ, {"ROOT_SCOUT_TEST_HOST_SECRET": "must-not-leak"}):
                result = repo_checks.RepoCheckRunner(root, timeout_seconds=30).run_check("isolation", ["python3", "-c", script])
            self.assertTrue(result["passed"], result)
            observed = json.loads(result["stdout_preview"])
            self.assertNotEqual(observed["uid"], 0)
            self.assertTrue(observed["readonly"])
            self.assertTrue(observed["network_blocked"])
            self.assertIsNone(observed["host_secret"])
            self.assertEqual(original.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((root / ".root-scout").exists())

    def test_timeout_removes_owned_container(self):
        container_name = "root-scout-check-" + "a12b34c56d78"
        before = subprocess.run(["docker", "inspect", container_name], capture_output=True, timeout=10)
        self.assertNotEqual(before.returncode, 0, "the test container name must be unused")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(repo_checks.uuid, "uuid4", return_value=types.SimpleNamespace(hex="a12b34c56d78")):
            result = repo_checks.RepoCheckRunner(Path(directory), timeout_seconds=2).run_check(
                "timeout", ["python3", "-c", "import time; time.sleep(60)"])
        self.assertEqual(result["classification"], "timeout", result)
        inspected = subprocess.run(["docker", "inspect", container_name], capture_output=True, timeout=10)
        self.assertNotEqual(inspected.returncode, 0)

    def test_output_limit_stops_a_flooding_check(self):
        with tempfile.TemporaryDirectory() as directory:
            result = repo_checks.RepoCheckRunner(Path(directory), timeout_seconds=20).run_check(
                "output", ["python3", "-c", "import os\nwhile True: os.write(1, b'x' * 4096)"])
        self.assertEqual(result["classification"], "output_limit", result)
        self.assertTrue(result["stdout_truncated"])
        self.assertLessEqual(result["stdout_bytes"], 65536)


if __name__ == "__main__":
    unittest.main()
