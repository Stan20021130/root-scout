"""Evidence/authority boundaries for the coding-agent QA probe; no model API calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import mcp_server as mcp
except ModuleNotFoundError as exc:
    if exc.name != "mcp_server":
        raise
    mcp = None
import root_scout as core


SOURCE = "n = int(input())\nvalues = list(map(int, input().split()))\nbest = 0\nfor value in values:\n    best = max(best, value)\nprint(best)\n"


def trusted_case(input_text="2\n-4 -2\n", expected="-2\n"):
    return {"input": input_text, "expected": expected, "trusted": True,
            "expected_source": "reviewed maximum of the supplied integers",
            "legal_input": True, "legal_input_source": "n=2; values within [-100,100]"}


def request():
    return {"source": SOURCE, "language": "python3",
            "statement": "Given n integers, output their maximum. 1 <= n <= 8; -100 <= values <= 100.",
            "known_failing_tests": [trusted_case(), trusted_case("2\n-4 2\n", "2\n")]}


def answer(**fields):
    return {"candidates": [], "updates": [], "actions": [], "next_question": "", "stop": True, **fields}


def intervention_step():
    return answer(candidates=[{"ref": "init", "kind": "defect_candidate",
        "claim": "Zero initialization masks the maximum on all-negative inputs.",
        "trigger": "all values negative", "locations": ["line 3"], "evidence_ids": ["T0001"]}],
        actions=[{"kind": "run_variant", "ref": "contrast", "parent_version_id": "original",
                  "candidate_refs": ["init"], "edits": [{"start_line": 3, "end_line": 3,
                  "old_text": "best = 0", "new_text": "best = -101"}],
                  "input_refs": ["T0001", "T0002"], "inputs": [],
                  "question": "Does changing only the initializer remove the negative-input mismatch and preserve the control?"}],
        next_question="Review the intervention and passing control.", stop=False)


def fixture_state(real_docker=False):
    responses = iter([intervention_step(), answer()])
    def ask(*_args, **_kwargs):
        return copy.deepcopy(next(responses))
    def run(source, _language, case, _env, profile="formal", build_cache=None):
        values = list(map(int, case["input"].split()))[1:]
        value = max([0] + values) if "best = 0" in source else max(values)
        return {"status": "ok", "stdout": f"{value}\n", "stderr": "", "returncode": 0, "wall_seconds": 0}
    with patch.object(core.OpenAICompatible, "ask", side_effect=ask):
        if real_docker:
            return core.diagnose(request())
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(core, "docker_preflight", return_value={"available": True, "image_id": "fixture"}), \
             patch.object(core, "run_case", side_effect=run):
            state = core.diagnose(request())
    core.verify_saved_case_state(state)
    return state


class EvidenceProbeTests(unittest.TestCase):
    def setUp(self):
        self.state = fixture_state()

    def test_exact_counterexample_and_isolated_contrast_preserve_unverified_semantics(self):
        before = copy.deepcopy(self.state)
        report = core.probe_result(self.state)
        self.assertEqual(report["status"], "counterexample_found")
        self.assertEqual(report["counterexamples"][0]["actual"], "0\n")
        self.assertEqual(report["counterexamples"][0]["expected"], "-2\n")
        self.assertEqual(report["counterexamples"][0]["minimality"], "not_established")
        finding = report["findings"][0]
        self.assertEqual(finding["source_anchors"][0]["excerpt"], "best = 0")
        self.assertEqual(finding["causal_check"]["status"], "isolated_contrast_observed")
        self.assertFalse(finding["causal_check"]["semantic_cause_verified"])
        self.assertEqual(finding["status"]["verification"], "unverified")
        self.assertEqual(self.state, before)

    def test_static_suspicion_is_not_a_confirmed_defect(self):
        self.state["evidence"] = []
        report = core.probe_result(self.state)
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["unverified_candidate_ids"], ["C0001"])

    def test_missing_trust_illegal_input_and_execution_failures_never_become_counterexamples(self):
        for alteration in ({"trusted_expected": False}, {"legal_input_verified": False},
                           {"expected_source": ""}, {"result": {"status": "timeout"}},
                           {"result": {"status": "compile_error"}},
                           {"result": {"status": "ok", "stdout": "0", "stdout_truncated": True}}):
            with self.subTest(alteration=alteration):
                state = copy.deepcopy(self.state)
                state["evidence"][0].update(alteration)
                self.assertEqual(core.probe_result(state)["counterexamples"], [])

    def test_joint_or_derived_interventions_cannot_claim_isolation(self):
        for joint in (True, False):
            with self.subTest(joint=joint):
                state = copy.deepcopy(self.state)
                if joint:
                    state["tool_actions"][0]["candidate_links"].append({"candidate_id": "C0002"})
                else:
                    state["versions"]["V1"]["parent_version_id"] = "V0"
                check = core.probe_result(state)["findings"][0]["causal_check"]
                self.assertEqual(check["status"], "not_established")
                self.assertEqual(check["comparisons"][0]["isolation"], "joint_or_derived_intervention")

    def test_regression_is_reported_even_if_the_original_counterexample_is_removed(self):
        variant_control = self.state["evidence"][-1]
        variant_control["result"].update(stdout="-101\n", matched=False)
        check = core.probe_result(self.state)["findings"][0]["causal_check"]
        self.assertTrue(check["regression_observed"])
        self.assertIn("regression_observed", [c["outcome"] for c in check["comparisons"]])

    def test_locations_are_original_exact_anchors_and_not_guessed(self):
        anchors = core.probe_source_anchors(SOURCE, ["line 999", "near initialization", "line 3 or 5", "L3", "lines 3-4"])
        self.assertEqual([(a["start_line"], a["end_line"]) for a in anchors], [(3, 3), (3, 4)])
        self.assertTrue(all(a["causal_attribution"] == "unverified" for a in anchors))

    def test_malformed_model_location_does_not_lose_real_counterexamples(self):
        self.state["candidates"][0]["source_location_claims"] = None
        report = core.probe_result(self.state)
        self.assertEqual(report["status"], "counterexample_found")
        self.assertEqual(report["findings"][0]["source_anchors"], [])
        self.assertIn("source_location_unresolved", report["scope"]["gaps"])

    def test_missing_exit_status_cannot_be_promoted_to_a_completed_comparison(self):
        del self.state["evidence"][0]["result"]["returncode"]
        self.assertEqual(core.probe_result(self.state)["counterexamples"], [])

    def test_passing_finite_tests_never_become_a_global_correctness_claim(self):
        self.state["candidates"] = []
        self.state["evidence"][0]["result"].update(stdout="-2\n", matched=True)
        self.state["stop_reason"] = "no_bug_found_within_scope"
        report = core.probe_result(self.state)
        self.assertEqual(report["status"], "no_failure_observed")
        self.assertEqual(report["scope"]["global_correctness"], "not_established")

    def test_analysis_failure_preserves_already_observed_counterexamples(self):
        self.state["stop_reason"] = "failed"
        self.state["analysis_errors"] = [{"type": "fixture"}]
        report = core.probe_result(self.state)
        self.assertEqual(report["status"], "counterexample_found")
        self.assertIn("analysis_error", report["scope"]["gaps"])

    def test_blocked_variant_or_action_remains_visible_after_candidates_are_withdrawn(self):
        self.state["candidates"] = []
        self.state["evidence"][0]["result"].update(stdout="-2\n", matched=True)
        self.state["evidence"][-1]["result"]["status"] = "timeout"
        self.state["tool_actions"][0]["status"] = "blocked"
        report = core.probe_result(self.state)
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["scope"]["blocked_action_ids"], ["A0001"])
        self.assertIn(self.state["evidence"][-1]["id"], report["scope"]["blocked_evidence_ids"])

    def test_truncated_reproducer_is_explicit_and_full_evidence_remains_saved(self):
        evidence = self.state["evidence"][0]
        evidence["input"] = "1 " * 2000
        with tempfile.TemporaryDirectory() as directory:
            report = core.write_probe_artifacts(self.state, directory)
            self.assertFalse(report["counterexamples"][0]["reproducer_complete"])
            path = Path(report["artifacts"]["case_state"]["path"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["evidence"][0]["input"], evidence["input"])
            self.assertEqual(report["artifacts"]["case_state"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_empty_or_partial_run_cannot_report_no_failure(self):
        for state in ({}, {"stop_reason": "failed"}, {"stop_reason": "budget_exhausted"}):
            self.assertNotEqual(core.probe_result(state)["status"], "no_failure_observed")


@unittest.skipUnless(mcp is not None, "MCP service is deprecated and absent from this checkout")
class MCPProbeBoundaryTests(unittest.TestCase):
    def test_rejects_implicit_test_trust_and_privileged_environment_or_engine(self):
        for fields in ({"known_failing_tests": [{"input": "1", "expected": "1"}]},
                       {"environment": {"_root_scout_image_id": "arbitrary"}},
                       {"environment": {"time_limit_ms": True}}, {"engine": "legacy"},
                       {"provider": "anything"}, {"known_failing_tests": [dict(trusted_case(), trusted=1)]}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                mcp.prepare_request({"source": SOURCE, "statement": "spec", **fields}, "rootscout_diagnose")

    def test_caller_guarantees_do_not_rewrite_statement_or_oracle_binding(self):
        req = mcp.prepare_request({"source": SOURCE, "statement": "original spec",
                                  "input_format_guarantees": ["n > 0"]}, "rootscout_diagnose")
        self.assertEqual(req["statement"], "original spec")
        self.assertEqual(req["input_format_guarantees"], ["n > 0"])
        state = fixture_state()
        prompt = json.loads(core.v2_prompt(req, state, 1))
        self.assertEqual(prompt["caller_input_guarantees"], ["n > 0"])

    def test_unavailable_sandbox_stops_before_model_and_host_execution(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"ROOT_SCOUT_ARTIFACT_DIR": directory}), \
             patch.object(core, "docker_preflight", return_value={"available": False, "reason": "fixture missing Docker"}), \
             patch.object(core.OpenAICompatible, "ask", side_effect=AssertionError("must not call model")), \
             patch.object(core, "run_case", side_effect=AssertionError("must not execute")):
            report = mcp.execute_diagnose({"source": SOURCE, "statement": "spec"})
            self.assertEqual(report["status"], "environment_blocked")
            self.assertTrue(Path(report["artifacts"]["case_state"]["path"]).is_file())

    def test_controller_failure_is_persisted_and_not_reported_as_clean(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"ROOT_SCOUT_ARTIFACT_DIR": directory}), \
             patch.object(core, "diagnose", side_effect=RuntimeError("fixture controller failure")):
            report = mcp.execute_diagnose({"source": SOURCE, "statement": "spec"})
            self.assertEqual(report["status"], "error")
            saved = core.load_json(report["artifacts"]["case_state"]["path"])
            self.assertEqual(saved["analysis_errors"][0]["type"], "RuntimeError")

    def test_source_file_is_read_only_and_evidence_survives_tool_return(self):
        fixture = fixture_state()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "solution.py"
            source.write_text(SOURCE, encoding="utf-8")
            before = source.read_bytes()
            def diagnose(req, **_kwargs):
                state = copy.deepcopy(fixture)
                state["request_snapshot"] = req
                state["integrity"] = core.v2_integrity(state)
                return state
            with patch.dict(os.environ, {"ROOT_SCOUT_ARTIFACT_DIR": str(Path(directory) / "artifacts")}), \
                 patch.object(core, "diagnose", side_effect=diagnose):
                report = mcp.execute_diagnose({"source": str(source), "statement": request()["statement"],
                                              "known_failing_tests": request()["known_failing_tests"]})
            self.assertEqual(source.read_bytes(), before)
            saved = core.load_json(report["artifacts"]["case_state"]["path"])
            core.verify_saved_case_state(saved)
            self.assertEqual(report["source"]["path"], str(source.resolve()))
            self.assertTrue(Path(report["artifacts"]["probe"]["path"]).exists())


@unittest.skipUnless(os.environ.get("ROOT_SCOUT_INTEGRATION") == "1", "requires real Docker sandbox")
class DockerProbeTests(unittest.TestCase):
    def test_real_execution_confirms_contrast_without_a_model_api_call(self):
        state = fixture_state(real_docker=True)
        core.verify_saved_case_state(state)
        report = core.probe_result(state)
        self.assertEqual(report["status"], "counterexample_found")
        self.assertEqual(report["findings"][0]["causal_check"]["status"], "isolated_contrast_observed")
        self.assertFalse(report["scope"]["source_modified"])


if __name__ == "__main__":
    unittest.main()
