"""Run the remaining frozen fixtures using the original paired runner."""
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PILOT = ROOT / "experiments" / "2026-11-10-paired-v2"
PILOT_OUTPUT = ROOT / "validation" / "paired-v2-2026-11-10"
MANIFEST = HERE / "manifest.json"
OUTPUT = ROOT / "validation" / "paired-v2-2026-11-10-extension"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    config = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if sha(Path(__file__)) != config["extension_runner_sha256"]:
        raise ValueError("extension wrapper changed")
    if sha(PILOT / "manifest.json") != config["pilot_manifest_sha256"]:
        raise ValueError("pilot manifest changed")
    spec = importlib.util.spec_from_file_location("paired_pilot_runner", PILOT / "run.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    fingerprints = json.loads((PILOT_OUTPUT / "fingerprints.json").read_text(encoding="utf-8"))
    if sha(PILOT / "run.py") != fingerprints["runner_sha256"]:
        raise ValueError("pilot runner changed")
    for name, expected in fingerprints["files"].items():
        if sha(ROOT / name) != expected:
            raise ValueError(f"pilot input changed: {name}")
    pilot = json.loads((PILOT / "manifest.json").read_text(encoding="utf-8"))
    by_id = {case["case_id"]: case for case in runner.evaluation.read_jsonl(runner.evaluation.DATASET)}
    remainder = [case_id for case_id in by_id if case_id not in set(pilot["case_ids"])]
    if config["case_ids"] != remainder:
        raise ValueError("extension must contain every remaining fixture in frozen order")
    for key in ("dataset_sha256", "arms", "order", "model", "base_url", "prompt_profile", "reasoning_effort",
                "reasoning_policy", "output_policy", "per_diagnosis_token_admission_limit", "wall_limit_seconds",
                "execution_workers", "schema_clarify", "image_id"):
        if pilot[key] != config[key]:
            raise ValueError(f"extension changed paired configuration: {key}")
    total = 0
    for case_id in pilot["case_ids"]:
        for arm in pilot["arms"]:
            state = runner.read_finished(PILOT_OUTPUT / arm / case_id, by_id[case_id], arm)
            total += state["budget"]["tokens_total"]
    if total != config["pilot_accounted_tokens"] or total + config["total_accounted_token_stop"] != pilot["total_accounted_token_stop"]:
        raise ValueError("pilot accounting no longer matches the shared experiment stop")
    runner.MANIFEST = MANIFEST
    runner.OUTPUT = OUTPUT
    runner.main()


if __name__ == "__main__":
    main()
