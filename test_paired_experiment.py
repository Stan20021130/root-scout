import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


RUNNER = Path(__file__).with_name("experiments") / "2026-11-10-paired-v2" / "run.py"
spec = importlib.util.spec_from_file_location("paired_experiment_runner_test", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PairedExperimentGuardTests(unittest.TestCase):
    def failure_state(self):
        return {"stop_reason": "model_protocol_incomplete", "analysis_errors": [{"error_class": "transport_failed"}],
                "budget": {"tokens_total": 1200, "confirmed_usage_tokens": 0, "reserved_unknown_tokens": 1200,
                           "calls": [{"status": "failed"}]}}

    def test_new_transport_failure_stops_before_the_other_arm(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(runner, "OUTPUT", Path(directory)), \
             patch.object(runner, "read_finished", return_value=self.failure_state()), \
             patch.object(runner.subprocess, "run", return_value=SimpleNamespace(stdout="", stderr="", returncode=1)) as run, \
             patch.object(sys, "argv", [str(RUNNER), "--pairs", "1"]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "transport or environment failed"):
                runner.main()
            self.assertEqual(run.call_count, 1)
            self.assertFalse((Path(directory) / "one_shot" / "max-init-python3").exists())

    def test_existing_transport_failure_prevents_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "causal" / "max-init-python3").mkdir(parents=True)
            with patch.object(runner, "OUTPUT", Path(directory)), \
                 patch.object(runner, "read_finished", return_value=self.failure_state()), \
                 patch.object(runner.subprocess, "run") as run, \
                 patch.object(sys, "argv", [str(RUNNER), "--pairs", "1"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "previous model transport"):
                    runner.main()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
