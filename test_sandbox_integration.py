"""Run explicitly with ROOT_SCOUT_INTEGRATION=1 after building the image."""
import json
import os
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import root_scout


@unittest.skipUnless(os.environ.get("ROOT_SCOUT_INTEGRATION") == "1", "requires real Docker sandbox")
class SandboxIntegrationTests(unittest.TestCase):
    def run_source(self, source, language="python3", data="", expected="", **environment):
        return root_scout.docker_run(source, language, {"input": data, "expected": expected}, environment)

    def containers(self):
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=root-scout-"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return set(result.stdout.split())

    def test_python_receives_stdin(self):
        result = self.run_source("print(int(input()) + 1)", data="41\n", expected="42\n")
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["matched"], result)

    def test_cpp17_and_cpp20_receive_stdin(self):
        source = '#include <iostream>\nint main(){long long n; std::cin>>n; std::cout<<n+1<<"\\n";}'
        for language in ("cpp17", "cpp20"):
            with self.subTest(language=language):
                result = self.run_source(source, language, "3000000000\n", "3000000001\n")
                self.assertEqual(result["status"], "ok", result)
                self.assertTrue(result["matched"], result)

    def test_cpp_build_artifact_is_reused_across_cases(self):
        source = '#include <iostream>\nint main(){long long n; std::cin>>n; std::cout<<n+1<<"\\n";}'
        cache = root_scout.BuildCache()
        try:
            first = root_scout.docker_run(source, "cpp17", {"input": "1\n", "expected": "2\n"}, {}, "formal", cache)
            second = root_scout.docker_run(source, "cpp17", {"input": "2\n", "expected": "3\n"}, {}, "formal", cache)
            diagnostic = root_scout.docker_run(source, "cpp17", {"input": "2\n", "expected": "3\n"}, {}, "diagnostic", cache)
        finally:
            cache.close()
        self.assertEqual(first["status"], "ok", first)
        self.assertFalse(first["build"].get("cached"))
        self.assertEqual(second["status"], "ok", second)
        self.assertTrue(second["build"].get("cached"))
        self.assertEqual(second["build"]["wall_seconds"], 0.0)
        self.assertTrue(second["matched"])
        self.assertFalse(diagnostic["build"].get("cached"))

    def test_compilation_failure_is_distinct(self):
        result = self.run_source("int main( {", "cpp17")
        self.assertEqual(result["status"], "compile_failed", result)
        self.assertFalse(result.get("matched", False), result)

    def test_python_exit_codes_are_program_failures_not_docker_errors(self):
        for code in (125, 126, 127):
            with self.subTest(code=code):
                result = self.run_source(f"import sys; sys.exit({code})")
                self.assertEqual(result["status"], "program_failed", result)
                self.assertEqual(result["returncode"], code)

    def test_program_stderr_cannot_spoof_compile_failure(self):
        source="import sys; print('ROOT_SCOUT_COMPILE_FAILED', file=sys.stderr); print(9)"
        result = self.run_source(source, expected="9\n")
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["matched"], result)
        cpp = '#include <iostream>\nint main(){std::cerr << "ROOT_SCOUT_COMPILE_FAILED\\n"; std::cout << 9 << "\\n";}'
        result = self.run_source(cpp, "cpp17", expected="9\n")
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["matched"], result)

    def test_missing_image_is_an_environment_failure(self):
        before = self.containers()
        with patch.dict(os.environ, {"ROOT_SCOUT_IMAGE": "root-scout:image-that-does-not-exist"}):
            result = self.run_source("print(9)", expected="9\n")
        self.assertEqual(result["status"], "environment_blocked", result)
        self.assertEqual(self.containers() - before, set())

    def test_timeout_cleans_up_child_processes(self):
        before = self.containers()
        result = self.run_source(
            "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\ntime.sleep(60)",
            time_limit_ms=100,
        )
        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(self.containers() - before, set())

    def test_bash_session_survives_an_in_container_timeout(self):
        session = root_scout.BashSession(lifetime_seconds=120)
        try:
            first = root_scout.docker_bash_run("print(1)", "python3", "echo keep > /tmp/keep.txt; sleep 30",
                                               timeout=3.0, session=session)
            self.assertEqual(first["status"], "timeout", first)
            self.assertTrue(first["timed_out"])
            self.assertEqual(first["timeout_scope"], "process", first)
            self.assertFalse(first["workspace_reset"], first)
            self.assertIsNotNone(session.name)
            second = root_scout.docker_bash_run("print(1)", "python3", "cat /tmp/keep.txt",
                                                timeout=10.0, session=session)
            self.assertEqual(second["status"], "ok", second)
            self.assertEqual(second["stdout"].strip(), "keep")
            self.assertEqual(second["workspace_generation"], first["workspace_generation"])
            self.assertEqual(second["timeout_cap_seconds"], 10.0)
        finally:
            session.close()

    def test_deep_recursion_is_not_reported_as_a_program_defect(self):
        # A judge grants the stack out of the memory limit; Docker's 8 MiB default
        # made legal deep recursion crash here and look like a program defect.
        source = ("#include <bits/stdc++.h>\n"
                  "int n; long long acc;\n"
                  "void dfs(int d){ volatile char pad[256]; pad[0]=(char)d;"
                  " if(d<n) dfs(d+1); acc+=d; pad[255]=(char)acc; }\n"
                  "int main(){ n=200000; dfs(0); std::cout<<acc<<\"\\n\"; return 0; }\n")
        result = self.run_source(source, "cpp17", "", "")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["stdout"].strip(), "20000100000")

        session = root_scout.BashSession(lifetime_seconds=120)
        try:
            bash = root_scout.docker_bash_run(source, "cpp17",
                                              "ulimit -s; g++ -O2 -std=c++17 /work/solution.cpp -o /tmp/sol && /tmp/sol",
                                              timeout=60, session=session)
            self.assertEqual(bash["status"], "ok", bash)
            lines = bash["stdout"].strip().splitlines()
            self.assertEqual(lines[0], "262144", bash)
            self.assertEqual(lines[-1], "20000100000", bash)
        finally:
            session.close()

    def test_output_limit_is_bounded_and_cleans_up(self):
        before = self.containers()
        result = self.run_source("import os\nwhile True: os.write(1, b'x' * 4096)")
        self.assertEqual(result["status"], "output_limit", result)
        self.assertLessEqual(len(result["stdout"].encode()), root_scout.SANDBOX_STDOUT_LIMIT_BYTES)
        self.assertTrue(result.get("stdout_truncated"), result)
        self.assertEqual(self.containers() - before, set())

    def test_valid_40k_stdout_is_not_truncated(self):
        result = self.run_source("print('x' * 39855)", expected="x" * 39855 + "\n")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["stdout"].encode()), 39856)
        self.assertFalse(result.get("stdout_truncated"), result)

    def test_nonreading_program_cannot_block_input_writer(self):
        started = time.monotonic()
        result = self.run_source("import time; time.sleep(60)", data="x" * (1024 * 1024), time_limit_ms=100)
        self.assertEqual(result["status"], "timeout", result)
        self.assertLess(time.monotonic() - started, 15)

    def test_nonroot_readonly_and_network_isolation(self):
        source = """import json, os, socket
readonly = False
try:
    open('/root_scout_write_probe', 'w').close()
except OSError:
    readonly = True
s = socket.socket()
s.settimeout(0.2)
network_blocked = s.connect_ex(('1.1.1.1', 53)) != 0
print(json.dumps([os.getuid(), readonly, network_blocked]))
"""
        result = self.run_source(source)
        self.assertEqual(result["status"], "ok", result)
        uid, readonly, network_blocked = json.loads(result["stdout"])
        self.assertNotEqual(uid, 0)
        self.assertTrue(readonly)
        self.assertTrue(network_blocked)

    def test_legacy_model_protocol_evidence_gate_and_residual_flow(self):
        """Exercise HTTP protocol and real execution without a paid model call."""
        requests = []
        statement = "输出输入整数加一。"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                content = json.loads(body["messages"][-1]["content"])
                if "evidence" not in content:
                    answer = {"contract": [{"clause": "输出输入整数加一", "status": "stated", "statement_quote": statement}],
                              "assumptions": [], "hypotheses": [], "residual_audit": [], "targeted_tests": []}
                elif "interventions" not in content and "roots" in content:
                    answer = {"patches": [{"root_id": "R1", "source_line": 1,
                                           "old_text": "int(input()) - 1", "new_text": "int(input()) + 1"}]}
                elif "roots" in content:
                    clean_source = "+ 1" in content.get("source", "")
                    answer = {"residual_audit": ["无其他可验证方向"], "assumptions": [],
                              "next_checks": [] if clean_source else ["未验证整数输入范围边界"]}
                else:
                    root = {"claim": "加一被写成减一", "contract_quote": statement, "source_line": 1, "source_excerpt": "int(input()) - 1",
                            "trigger": "输入整数41", "causal_explanation": "41减一得到40，题意要求41加一得到42。",
                            "repair_direction": "把减一改成加一", "evidence_ids": [content["evidence"][0]["id"]]}
                    answer = {"roots": [root, dict(root)]}
                payload = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                                      "usage": {"prompt_tokens": 100, "completion_tokens": 100}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = {"statement": statement, "source": "print(int(input()) - 1)", "language": "python3",
                   "known_failing_tests": [{"input": "41\n", "expected": "42\n", "trusted": True,
                                            "expected_source": "手算41+1", "legal_input": True,
                                            "legal_input_source": "整数41符合题面输入域"}]}
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "local-protocol-test", "OPENAI_MODEL": "local-fixture",
                                         "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1"}):
                state = root_scout._diagnose_legacy(request)
                first_call_count = len(requests)
                clean_request = dict(request, source="print(int(input()) + 1)")
                clean_state = root_scout._diagnose_legacy(clean_request)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(first_call_count, 4, state)
        self.assertEqual(len(requests) - first_call_count, 2, clean_state)
        self.assertEqual([x["phase"] for x in state["budget"]["calls"]],
                         ["CONTRACT+PLAN", "ADJUDICATE", "INTERVENE", "RESIDUAL"])
        self.assertEqual(len(state["roots"]), 1, state)
        self.assertEqual(state["roots"][0]["evidence_level"], "E2")
        self.assertEqual(state["roots"][0]["intervention"]["status"], "supported")
        self.assertEqual(len(state["intervention_variants"]), 1)
        self.assertEqual(state["intervention_variants"][0]["status"], "completed")
        self.assertEqual(state["stop_reason"], "oracle_unresolved")
        self.assertEqual(state["budget"]["tokens_total"], 800)
        self.assertTrue(state["budget"]["usage_complete"])
        self.assertIn("未验证整数输入范围边界", state["coverage"]["untested"])
        self.assertIn("加一被写成减一", root_scout.markdown(state))
        self.assertEqual(clean_state["roots"], [], clean_state)
        self.assertEqual(clean_state["stop_reason"], "no_bug_found_within_scope")


if __name__ == "__main__":
    unittest.main()
