"""Run a frozen, budgeted local paired experiment; never retry existing runs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import evaluation
import root_scout

MANIFEST = Path(__file__).with_name("manifest.json")
OUTPUT = ROOT / "validation" / "paired-v2-2026-11-10"
FINGERPRINTED_FILES = ("root_scout.py", "native_adapter.py", "native_transport.py", "probe_hard.py",
                       "state_feedback.py", "oracle_tools.py", "evaluation.py", "evaluation_samples.jsonl")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def configured_environment(config):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ROOT_SCOUT_", "OPENAI_"))}
    env.update(OPENAI_BASE_URL=config["base_url"], OPENAI_MODEL=config["model"],
               OPENAI_API_KEY="root-scout-local", OPENAI_REASONING_EFFORT=config["reasoning_effort"],
               ROOT_SCOUT_REASONING_POLICY=config["reasoning_policy"], ROOT_SCOUT_OUTPUT_POLICY=config["output_policy"],
               ROOT_SCOUT_PROMPT_PROFILE=config["prompt_profile"], ROOT_SCOUT_SCHEMA_CLARIFY="1",
               ROOT_SCOUT_MAX_TOKENS=str(config["per_diagnosis_token_admission_limit"]),
               ROOT_SCOUT_MAX_WALL_SECONDS=str(config["wall_limit_seconds"]),
               ROOT_SCOUT_EXEC_WORKERS=str(config["execution_workers"]), ROOT_SCOUT_IMAGE=config["image_id"],
               ROOT_SCOUT_MODEL_OUTPUT_LIMIT="128000", ROOT_SCOUT_TRANSPORT="native_delta")
    return env


def read_finished(directory, case, arm):
    meta_path = directory / "run.json"
    if not meta_path.exists():
        raise ValueError(f"Existing run directory has no ledger; inspect it before proceeding: {directory}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("status") != "finished":
        raise ValueError(f"Run was interrupted or did not finish; inspect it before proceeding: {directory}")
    state = json.loads((directory / "diagnose" / "case_state.json").read_text(encoding="utf-8"))
    root_scout.verify_saved_case_state(state)
    if state.get("request_snapshot") != case["request"] or state.get("strategy") != arm:
        raise ValueError(f"Run does not match its request and arm: {directory}")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=2, help="run the first N frozen pairs, with no automatic retries")
    args = parser.parse_args()
    config = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not 1 <= args.pairs <= len(config["case_ids"]):
        raise ValueError("pairs must be within the frozen manifest")
    if sha(evaluation.DATASET) != config["dataset_sha256"]:
        raise ValueError("frozen dataset hash differs from the manifest")
    dataset = evaluation.read_jsonl(evaluation.DATASET)
    by_id = {case["case_id"]: case for case in dataset}
    cases = [by_id[case_id] for case_id in config["case_ids"]]
    if len(set(config["case_ids"])) != len(cases):
        raise ValueError("duplicate case in experiment manifest")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fingerprints = {"manifest_sha256": sha(MANIFEST), "runner_sha256": sha(Path(__file__)),
                    "files": {name: sha(ROOT / name) for name in FINGERPRINTED_FILES}}
    fingerprint_path = OUTPUT / "fingerprints.json"
    if fingerprint_path.exists():
        if json.loads(fingerprint_path.read_text(encoding="utf-8")) != fingerprints:
            raise ValueError("experiment inputs changed; do not mix results")
    else:
        save(fingerprint_path, fingerprints)
    evaluation.export_requests(cases, OUTPUT / "requests")
    env = configured_environment(config)
    accounted = 0
    for i, case in enumerate(cases[:args.pairs]):
        arms = config["arms"] if i % 2 == 0 else list(reversed(config["arms"]))
        directories = [OUTPUT / arm / case["case_id"] for arm in arms]
        existing = [directory.exists() for directory in directories]
        for arm, directory, exists in zip(arms, directories, existing):
            if exists:
                state = read_finished(directory, case, arm)
                accounted += state["budget"]["tokens_total"]
        remaining = existing.count(False)
        if accounted + remaining * config["per_diagnosis_token_admission_limit"] > config["total_accounted_token_stop"]:
            print(f"STOP before {case['case_id']}: reserve would exceed the experiment accounting stop", flush=True)
            break
        for arm, directory, exists in zip(arms, directories, existing):
            if exists:
                continue
            directory.mkdir(parents=True)
            meta_path = directory / "run.json"
            save(meta_path, {"status": "started", "case_id": case["case_id"], "strategy": arm})
            print(f"START {case['case_id']} {arm}; accounted_before={accounted}", flush=True)
            started = time.monotonic()
            command = [sys.executable, str(ROOT / "root_scout.py"), "diagnose",
                       str(OUTPUT / "requests" / (case["case_id"] + ".json")), "--out", str(directory / "diagnose"),
                       "--provider", "model", "--strategy", arm, "--engine", "v2"]
            try:
                completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace", timeout=config["wall_limit_seconds"] + 60)
            except subprocess.TimeoutExpired:
                save(meta_path, {"status": "timed_out", "case_id": case["case_id"], "strategy": arm})
                raise RuntimeError(f"diagnosis timed out; inspect {directory} before any retry")
            (directory / "cli.log").write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
            save(meta_path, {"status": "finished", "case_id": case["case_id"], "strategy": arm,
                             "exit_code": completed.returncode, "elapsed_seconds": round(time.monotonic() - started, 2)})
            state = read_finished(directory, case, arm)
            budget = state["budget"]
            accounted += budget["tokens_total"]
            print(f"DONE {case['case_id']} {arm}; stop={state['stop_reason']}; "
                  f"confirmed={budget['confirmed_usage_tokens']}; reserved_unknown={budget['reserved_unknown_tokens']}; "
                  f"calls={len(budget['calls'])}; accounted_total={accounted}", flush=True)
            if state["stop_reason"] in ("failed", "environment_blocked"):
                raise RuntimeError(f"controller or environment failed; inspect {directory} before proceeding")
    print(f"FINISHED; accounted={accounted}; no automatic reruns", flush=True)


if __name__ == "__main__":
    main()
