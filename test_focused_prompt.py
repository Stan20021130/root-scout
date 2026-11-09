"""Offline checks for the opt-in Prompt and its actual transport wiring."""
import copy
import io
import json
import os
import time
import unittest
from unittest.mock import patch

import native_adapter
import native_transport
import root_scout as core


def answer():
    return {"candidates": [], "updates": [], "actions": [], "next_question": "", "stop": True}


class FocusedPromptTests(unittest.TestCase):
    def test_baseline_is_unchanged_and_clarification_shares_the_prefix(self):
        baseline = core.SYSTEM + " " + core.v2_protocol_text()
        self.assertEqual(core.system_for_phase("EXPLORE_V2", native_tool=True, prompt_profile="baseline"), baseline)
        for native in (False, True):
            self.assertEqual(core.system_for_phase("EXPLORE_V2", native_tool=native, prompt_profile="focused-v1"),
                             core.system_for_phase("EXPLORE_V2_CLARIFY", native_tool=native, prompt_profile="focused-v1"))

    def test_focused_prompt_preserves_schema_and_marks_model_memory_unverified(self):
        native = core.system_for_phase("EXPLORE_V2", native_tool=True, prompt_profile="focused-v1")
        plain = core.system_for_phase("EXPLORE_V2", native_tool=False, prompt_profile="focused-v1")
        for prompt in (native, plain):
            self.assertIn("unverified working hypotheses", prompt)
            self.assertIn("process timeout can preserve the workspace", prompt)
            self.assertIn("not a trusted expected value", prompt)
            self.assertIn("performance concern does not settle functional correctness", prompt)
            self.assertNotIn("confirmed working-memory facts", prompt)
            self.assertNotIn("Timeout or output-limit failure resets the workspace", prompt)
        prefix = "Return one JSON object using this controller protocol: "
        schema = json.loads(plain.split(prefix, 1)[1].split("\n\n", 1)[0])
        self.assertEqual(schema["actions"], core.v2_protocol_spec()["actions"])
        self.assertEqual(schema["limits"], core.v2_protocol_spec()["limits"])
        self.assertNotIn('"example_response"', native)

    def test_timeout_feedback_does_not_claim_the_workspace_was_lost(self):
        req = {"source": "print(1)\n", "statement": "Print 1.", "language": "python3"}
        action = {"id": "A0001", "turn": 1, "kind": "run_bash", "status": "blocked", "blocked_reason": "timeout",
                  "proposal": {"command": "sleep 40"}, "result": {"timed_out": True, "timeout_cap_seconds": 40.0,
                  "workspace_reset": False, "status": "timeout", "exit_code": 124, "stdout": "", "stderr": ""}}
        state = {"run_configuration": {"protocol_schema_version": 5, "prompt_profile": "focused-v1"},
                 "tool_actions": [action], "evidence": [], "candidates": [], "versions": {},
                 "protocol_issues": [], "model_turns": [], "invariants": []}
        notes = json.loads(core.v2_prompt(req, state, 2))["controller_notes"]
        self.assertTrue(any("in-container deadline may have been shorter" in note for note in notes))
        self.assertTrue(any("Check workspace_reset" in note for note in notes))
        self.assertFalse(any("everything it computed was lost" in note for note in notes))

    def test_real_native_request_uses_the_chosen_profile_and_unchanged_tool_schema(self):
        sent = []
        def request(body, _folder, _conversation_id, timeout=90):
            sent.append(copy.deepcopy(body))
            message = {"role": "assistant", "content": json.dumps(answer()), "reasoning_content": "", "tool_calls": []}
            return message, {"status": "ok", "done": True, "finish_reason": "stop", "response_bytes": 1,
                             "reasoning_chars": 0, "reasoning_sha256": core.sha(""),
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        env = {"OPENAI_MODEL": "fixture", "OPENAI_API_KEY": "fixture", "ROOT_SCOUT_OUTPUT_POLICY": "adaptive",
               "ROOT_SCOUT_MODEL_OUTPUT_LIMIT": "128000", "ROOT_SCOUT_PROMPT_PROFILE": "baseline"}
        with patch.dict(os.environ, env, clear=True), patch.object(native_transport, "request", side_effect=request):
            client_type = native_adapter.make_client(core, feedback_mode="delta")
            for profile in ("baseline", "focused-v1"):
                client = client_type({"run_configuration": {"protocol_schema_version": 5, "prompt_profile": profile}})
                client.ask("EXPLORE_V2", "{}", core.Budget(time.monotonic(), max_tokens=None))
        self.assertEqual(sent[0]["tools"], sent[1]["tools"])
        self.assertEqual(sent[0]["messages"][1], sent[1]["messages"][1])
        self.assertIn("unverified working hypotheses", sent[1]["messages"][0]["content"])
        self.assertLess(len(json.dumps(sent[1], ensure_ascii=False).encode()), len(json.dumps(sent[0], ensure_ascii=False).encode()))

    def test_plain_json_transport_receives_full_focused_schema(self):
        env = {"OPENAI_MODEL": "fixture", "OPENAI_API_KEY": "fixture", "ROOT_SCOUT_OUTPUT_POLICY": "fixed",
               "ROOT_SCOUT_PROMPT_PROFILE": "baseline"}
        response = (json.dumps(answer()), "stop", {"prompt_tokens": 1, "completion_tokens": 1},
                    {"done": True, "usage_final": True})
        with patch.dict(os.environ, env, clear=True), \
             patch.object(core.urllib.request, "urlopen", return_value=io.BytesIO()) as urlopen, \
             patch.object(core, "read_chat_sse", return_value=response):
            client = core.OpenAICompatible({"run_configuration": {"prompt_profile": "focused-v1"}})
            self.assertEqual(client.ask("EXPLORE_V2", "{}", core.Budget(time.monotonic(), max_tokens=None)), answer())
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertIn('"run_variant"', body["messages"][0]["content"])
        self.assertIn("unverified working hypotheses", body["messages"][0]["content"])
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_profile_is_recorded_and_invalid_profile_fails_before_sandbox(self):
        req = {"statement": "Print 1.", "source": "print(1)\n", "language": "python3",
               "known_failing_tests": [{"input": "", "expected": "1\n", "trusted": True,
                                        "expected_source": "statement", "legal_input": True,
                                        "legal_input_source": "no input required"}]}
        runner = {"status": "ok", "returncode": 0, "stdout": "1\n", "stderr": "", "wall_seconds": 0}
        with patch.dict(os.environ, {"ROOT_SCOUT_PROMPT_PROFILE": "focused-v1"}, clear=True), \
             patch.object(core, "docker_preflight", return_value={"available": True, "image_id": "fixture"}), \
             patch.object(core, "run_case", return_value=runner), \
             patch.object(core.OpenAICompatible, "ask", return_value=answer()):
            state = core.diagnose_v2(req)
        self.assertEqual(state["run_configuration"]["prompt_profile"], "focused-v1")
        self.assertEqual(core.verify_saved_case_state(state), state)
        with patch.dict(os.environ, {"ROOT_SCOUT_PROMPT_PROFILE": "typo"}, clear=True), \
             patch.object(core, "docker_preflight") as preflight:
            with self.assertRaisesRegex(ValueError, "ROOT_SCOUT_PROMPT_PROFILE"):
                core.diagnose_v2(req)
            preflight.assert_not_called()


@unittest.skipUnless(os.environ.get("ROOT_SCOUT_INTEGRATION") == "1", "requires real Docker sandbox")
class FocusedPromptDockerTests(unittest.TestCase):
    def test_native_fixture_preserves_counterexample_and_isolated_contrast(self):
        req = core.load_json("examples/probe_max.json")
        first = {"candidates": [{"ref": "init", "kind": "defect_candidate", "claim": "Zero masks negative maxima.",
                                  "trigger": "all values are negative", "locations": ["line 3"], "evidence_ids": ["T0001"]}],
                 "updates": [], "actions": [{"kind": "run_variant", "ref": "contrast", "parent_version_id": "original",
                    "candidate_refs": ["init"], "edits": [{"start_line": 3, "end_line": 3, "old_text": "best = 0",
                    "new_text": "best = -101"}], "input_refs": ["T0001", "T0002"], "inputs": [],
                    "question": "Does changing only the initializer remove the mismatch and preserve the control?"}],
                 "next_question": "Review the isolated contrast.", "stop": False}
        replies = iter((first, answer()))
        sent = []
        def request(body, _folder, _conversation_id, timeout=90):
            sent.append(copy.deepcopy(body))
            message = {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": [{
                "id": f"call-{len(sent)}", "type": "function", "function": {
                    "name": "submit_diagnostic_step", "arguments": json.dumps(next(replies))}}]}
            return message, {"status": "ok", "done": True, "finish_reason": "tool_calls", "response_bytes": 1,
                             "reasoning_chars": 0, "reasoning_sha256": core.sha(""),
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        env = {k: v for k, v in os.environ.items() if not k.startswith("ROOT_SCOUT_")}
        env.update({"ROOT_SCOUT_PROMPT_PROFILE": "focused-v1", "ROOT_SCOUT_OUTPUT_POLICY": "adaptive",
                    "ROOT_SCOUT_MODEL_OUTPUT_LIMIT": "128000", "OPENAI_MODEL": "fixture",
                    "OPENAI_API_KEY": "fixture", "OPENAI_REASONING_EFFORT": "low"})
        original_source = req["source"]
        client_type = native_adapter.make_client(core, feedback_mode="delta")
        with patch.dict(os.environ, env, clear=True), patch.object(core, "OpenAICompatible", client_type), \
             patch.object(native_transport, "request", side_effect=request):
            state = core.diagnose_v2(req)
        report = core.probe_result(state)
        self.assertEqual(state["run_configuration"]["prompt_profile"], "focused-v1")
        self.assertEqual(core.verify_saved_case_state(state), state)
        self.assertEqual(report["status"], "counterexample_found")
        self.assertEqual(report["findings"][0]["causal_check"]["status"], "isolated_contrast_observed")
        self.assertFalse(report["findings"][0]["causal_check"]["semantic_cause_verified"])
        self.assertEqual(req["source"], original_source)
        self.assertEqual(len(sent), 2)
        self.assertIn("unverified working hypotheses", sent[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
