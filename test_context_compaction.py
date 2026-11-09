import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import root_scout

HERE = pathlib.Path(__file__).resolve().parent
REPO = next((p for p in (HERE, *HERE.parents) if (p / "validation/bash-full40-v1").is_dir()), HERE)
BASELINE_FILE = REPO / "validation/context-compaction-v1/baseline/root_scout.py"
if not BASELINE_FILE.is_file():
    raise unittest.SkipTest(f"Historical context-compaction baseline not installed: {BASELINE_FILE}")
spec = importlib.util.spec_from_file_location("root_scout_baseline", BASELINE_FILE)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def sample_action(action_id="A0001", turn=1, command=None, status="accepted", result=None):
    return {"id": action_id, "turn": turn, "kind": "run_bash", "status": status,
            "proposal": {"ref": "bash-check", "command": command or "echo short", "input": "stdin"},
            "result": result or {"exit_code": 0, "stdout": "kept output", "stderr": "", "status": "ok"}}


class ContextCompactionTests(unittest.TestCase):
    def setUp(self):
        self.req = {"statement": "task statement", "source": "int main(){return 0;}\n", "language": "cpp17", "environment": {}}

    def test_prompt_compresses_only_old_long_command_and_expands_exactly(self):
        long_command = "cat <<'EOF' | python3 -c 'print(1)'\r\nλ\r\n" + ("# code\n" * 180)
        old = sample_action(command=long_command, result={"exit_code": 1, "stdout": "partial output", "stderr": "compile failed", "status": "error"})
        recent = sample_action("A0002", turn=2, command="echo recent " + "x" * 700)
        evidence = {"id": "T0001", "version_id": "original", "source_kind": "sample", "input": "1\n", "trusted_expected": True,
                    "expected": "42\n", "result": {"status": "ok", "matched": True, "stdout": "42\n", "stderr": "", "returncode": 0}}
        candidate = {"id": "C0001", "proposal_ref": "c1", "candidate_kind": "defect_candidate", "current_claim": "possible off by one",
                     "trigger_claim": "n is one", "source_location_claims": ["line 1"], "lifecycle": "open",
                     "current_evidence_ids": ["T0001"], "evidence_reference_events": [], "evidence_links": []}
        state = {"tool_actions": [old, recent], "evidence": [evidence], "candidates": [candidate], "protocol_issues": [],
                 "versions": {"original": {"parent_version_id": None, "source_sha256": "abc", "source": self.req["source"], "edits": []}},
                 "model_turns": [], "invariants": ["cursor starts at 1"]}

        for turn in (1, 2):
            # Cache layout can reorder object keys; compaction must preserve values.
            self.assertEqual(json.loads(root_scout.v2_prompt(self.req, state, turn)),
                             json.loads(baseline.v2_prompt(self.req, state, turn)))
        captured_prompt = json.loads(baseline.v2_prompt(self.req, state, 3))
        before = copy.deepcopy(captured_prompt)
        direct_compact = root_scout.compact_v2_prompt(captured_prompt, state, 3)
        compact_raw = root_scout.v2_prompt(self.req, state, 3)
        compact = json.loads(compact_raw)
        self.assertEqual(direct_compact, compact)
        mismatched_state = copy.deepcopy(state)
        mismatched_state["tool_actions"][0]["proposal"]["command"] = "different historical command"
        self.assertEqual(root_scout.compact_v2_prompt(captured_prompt, mismatched_state, 3), captured_prompt)
        self.assertIn("command_artifact", compact["tool_action_history"][0])
        self.assertNotIn("command", compact["tool_action_history"][0]["proposal"])
        self.assertEqual(compact["tool_action_history"][0]["command_artifact"]["sha256"], hashlib.sha256(long_command.encode()).hexdigest())
        self.assertEqual(compact["tool_action_history"][0]["command_artifact"]["bytes"], len(long_command.encode()))
        self.assertIn("command", compact["tool_action_history"][1]["proposal"])
        self.assertEqual(compact["tool_action_history"][0]["result"], before["tool_action_history"][0]["result"])
        self.assertEqual(compact["observations"], before["observations"])
        self.assertEqual(compact["versions"], before["versions"])
        self.assertEqual(compact["candidates"], before["candidates"])
        self.assertEqual(compact["established_invariants"], before["established_invariants"])
        expanded = json.loads(root_scout.expand_v2_prompt(compact_raw, state))
        self.assertEqual(expanded, before)
        self.assertGreater(len(baseline.v2_prompt(self.req, state, 3).encode()) - len(compact_raw.encode()), 128)

    def test_short_command_and_low_net_savings_keep_old_shape(self):
        short_state = {"tool_actions": [sample_action()], "evidence": [], "candidates": [],
                       "protocol_issues": [], "model_turns": [], "invariants": []}
        raw = root_scout.v2_prompt(self.req, short_state, 3)
        self.assertEqual(json.loads(raw), json.loads(baseline.v2_prompt(self.req, short_state, 3)))
        self.assertNotIn("command_artifact", raw)

        # A command can meet the length floor and still fall back when the note/ref overhead
        # is not a clear net saving against an unusually large prompt.
        almost_short = sample_action(command="x" * 520)
        huge_req = dict(self.req, statement="context " * 10000)
        state = {"tool_actions": [almost_short], "evidence": [], "candidates": [],
                 "protocol_issues": [], "model_turns": [], "invariants": []}
        self.assertEqual(json.loads(root_scout.v2_prompt(huge_req, state, 3)),
                         json.loads(baseline.v2_prompt(huge_req, state, 3)))

    def test_archive_preserves_bytes_filters_future_and_rejects_bad_ids(self):
        command = "printf 'one|two' | cat <<'EOF'\r\nλ雪\r\nEOF\r\n" + ("# x\r\n" * 120)
        action = sample_action(command=command)
        future = sample_action("A0002", turn=4, command="z" * 800)
        recent = sample_action("A0003", turn=3, command="r" * 800)
        with tempfile.TemporaryDirectory() as td:
            artifacts = root_scout.write_bash_history_artifacts(td, [action, future, recent], current_turn=4)
            path = pathlib.Path(td) / "history" / "A0001"
            self.assertEqual((path / "command.sh").read_bytes(), command.encode("utf-8"))
            self.assertEqual((path / "stdin.txt").read_bytes(), b"stdin")
            self.assertEqual(artifacts["A0001"], {"path": "/work/history/A0001/command.sh",
                "sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(), "bytes": len(command.encode("utf-8"))})
            self.assertFalse((pathlib.Path(td) / "history" / "A0002").exists())
            self.assertFalse((pathlib.Path(td) / "history" / "A0003").exists())
        bad = sample_action("../../escape", turn=1, command="x" * 600)
        with tempfile.TemporaryDirectory() as td, self.assertRaisesRegex(ValueError, "invalid prior run_bash action ID"):
            root_scout.write_bash_history_artifacts(td, [bad], current_turn=4)

    def test_docker_runner_mounts_prior_artifacts_without_docker(self):
        command = "printf x | cat <<'EOF'\r\n" + ("λ\n" * 300)
        prior = sample_action(command=command)
        seen = {}
        class FakeProcess:
            def __init__(self, argv, **kwargs):
                mount = argv[argv.index("-v") + 1].split(":/work:ro", 1)[0]
                folder = pathlib.Path(mount) / "history" / "A0001"
                seen["command"] = (folder / "command.sh").read_bytes()
                seen["stdin"] = (folder / "stdin.txt").read_bytes()
                self.stdin = None
                self.stdout, self.stderr, self.returncode = io.BytesIO(b"ok\n"), io.BytesIO(b""), 0
            def poll(self): return 0
        with patch.object(root_scout.subprocess, "Popen", FakeProcess), patch.object(root_scout.subprocess, "run"):
            result = root_scout.docker_bash_run("int main(){}", "cpp17", "true", prior_actions=[prior], current_turn=4)
        self.assertEqual(result["stdout"], "ok\n")
        self.assertEqual(seen["command"], command.encode("utf-8"))
        self.assertEqual(seen["stdin"], b"stdin")

    def test_execute_action_supplies_only_prior_tool_actions(self):
        old = sample_action(command="echo old")
        state = {"tool_actions": [old], "candidate_aliases": {}}
        mocked = {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "status": "ok"}
        action = {"turn": 3, "item": {"kind": "run_bash", "ref": "new", "command": "echo new"}}
        with patch.object(root_scout, "docker_bash_run", return_value=mocked) as run:
            root_scout.v2_execute_action(self.req, state, action, {}, {}, {}, None, None)
        self.assertEqual(run.call_args.kwargs["prior_actions"], [old])
        self.assertEqual(run.call_args.kwargs["current_turn"], 3)

    def test_real_saved_state_remains_verifiable_and_unmodified(self):
        path = REPO / "validation" / "bash-full40-v1" / "batch" / "results" / "cc-test-row-004-control" / "diagnose" / "case_state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(root_scout.verify_saved_case_state(state), state)
        before = copy.deepcopy(state)
        req = state["request_snapshot"]
        compact = root_scout.v2_prompt(req, state, 9)
        restored = json.loads(root_scout.expand_v2_prompt(compact, state))
        self.assertEqual(restored, json.loads(baseline.v2_prompt(req, state, 9)))
        self.assertEqual(state, before)
        self.assertEqual(root_scout.verify_saved_case_state(state), state)


if __name__ == "__main__": unittest.main()
