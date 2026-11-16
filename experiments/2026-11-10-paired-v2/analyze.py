"""Summarize the frozen experiment without making model calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import evaluation
import root_scout

HERE = Path(__file__).resolve().parent
OUTPUT = ROOT / "validation" / "paired-v2-2026-11-10"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprints = json.loads((OUTPUT / "fingerprints.json").read_text(encoding="utf-8"))
    if fingerprints["manifest_sha256"] != sha(manifest_path):
        raise ValueError("experiment manifest changed")
    pilot_dir = HERE.parent / "2026-11-10-paired-v2" if "pilot_manifest_sha256" in manifest else HERE
    candidates = [pilot_dir / "run.py", pilot_dir / "run-original.py"]
    if not any(path.exists() and fingerprints["runner_sha256"] == sha(path) for path in candidates):
        raise ValueError("experiment runner changed")
    if "extension_runner_sha256" in manifest and manifest["extension_runner_sha256"] != sha(HERE / "run.py"):
        raise ValueError("extension wrapper changed")
    scorer = evaluation
    for name, expected in fingerprints["files"].items():
        if name == "evaluation.py" and sha(ROOT / name) != expected:
            frozen = pilot_dir / "evaluation-original.py"
            if frozen.exists() and sha(frozen) == expected:
                spec = importlib.util.spec_from_file_location("paired_frozen_evaluation", frozen)
                scorer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(scorer)
                scorer.DATASET = ROOT / "evaluation_samples.jsonl"
                continue
        if sha(ROOT / name) != expected:
            raise ValueError(f"experiment input changed: {name}")
    by_id = {c["case_id"]: c for c in scorer.read_jsonl(scorer.DATASET)}
    cases = [by_id[case_id] for case_id in manifest["case_ids"]]
    arms = {}; row_maps = {}; state_hashes = []
    for arm in manifest["arms"]:
        states = {}; scorable_states = {}; details = {}
        for case in cases:
            case_id = case["case_id"]
            directory = OUTPUT / arm / case_id
            path = directory / "diagnose" / "case_state.json"
            if not path.exists():
                continue
            meta = json.loads((directory / "run.json").read_text(encoding="utf-8"))
            if meta.get("status") != "finished":
                continue
            state = json.loads(path.read_text(encoding="utf-8"))
            root_scout.verify_saved_case_state(state)
            if state.get("request_snapshot") != case["request"] or state.get("strategy") != arm:
                raise ValueError(f"result does not match {arm}/{case_id}")
            states[case_id] = state
            response_complete = any(t.get("response_complete") is True for t in state.get("model_turns", []))
            if response_complete:
                scorable_states[case_id] = state
            state_hashes.append({"arm": arm, "case_id": case_id, "sha256": sha(path)})
            budget = state["budget"]
            calls = [c for c in budget["calls"] if c.get("status") not in ("skipped", "rejected")]
            weighted = 0.0; split_complete = budget.get("usage_complete") is True
            for call in calls:
                nums = [call.get("cache_miss_tokens"), call.get("cache_hit_tokens"), call.get("output_tokens")]
                if call.get("usage_complete") is not True or any(type(n) is not int or n < 0 for n in nums):
                    split_complete = False
                else:
                    weighted += nums[0] + 0.02 * nums[1] + 4 * nums[2]
            details[case_id] = {"stop_reason": state.get("stop_reason"),
                                "probe_status": root_scout.probe_result(state).get("status"),
                                "attempted_model_calls": len(calls),
                                "confirmed_tokens": budget.get("confirmed_usage_tokens"),
                                "reserved_unknown_tokens": budget.get("reserved_unknown_tokens"),
                                "usage_complete": budget.get("usage_complete"),
                                "model_response_complete": response_complete,
                                "error_classes": sorted({e.get("error_class") or e.get("type") for e in state.get("analysis_errors", []) if e.get("error_class") or e.get("type")}),
                                "weighted_index": round(weighted, 2) if split_complete else None,
                                "wall_seconds": budget.get("wall_seconds"),
                                "final_review_status": state.get("final_review", {}).get("status")}
        scored = scorer.score_v2_locations(cases, scorable_states)
        row_map = {row["case_id"]: {**row, **details.get(row["case_id"], {})} for row in scored.pop("cases")}
        for case_id in states.keys() - scorable_states.keys():
            row_map[case_id]["state"] = "unscorable_no_completed_response"
        scored["cases_verified"] = len(states)
        scored["cases_unscorable"] = len(states) - len(scorable_states)
        scored["cases_missing"] = len(cases) - len(states)
        incomplete = scored["cases_missing"] + sum(details[x]["usage_complete"] is not True for x in states)
        confirmed = sum(details[x]["confirmed_tokens"] for x in states)
        scored["usage"] = {"known_confirmed_tokens": confirmed,
                           "known_reserved_unknown_tokens": sum(details[x]["reserved_unknown_tokens"] for x in states),
                           "incomplete_or_unavailable_cases": incomplete,
                           "confirmed_tokens": confirmed if not incomplete else None}
        row_maps[arm] = row_map
        complete = len(states) == len(cases) and scored["cases_invalid"] == 0 and incomplete == 0
        indices = [details[x]["weighted_index"] for x in states]
        arms[arm] = {**scored,
                     "attempted_model_calls": sum(details[x]["attempted_model_calls"] for x in states),
                     "weighted_index": round(sum(indices), 2) if complete and all(x is not None for x in indices) else None,
                     "cases": list(row_map.values())}
    pairs = []
    for case in cases:
        case_id = case["case_id"]
        a, b = (row_maps[arm][case_id] for arm in manifest["arms"])
        pair = {"case_id": case_id, "group": a["group"], "both_scorable": a["state"] == b["state"] == "verified"}
        if pair["both_scorable"]:
            pair["confirmed_token_difference_one_shot_minus_causal"] = (b["confirmed_tokens"] - a["confirmed_tokens"]
                if a["usage_complete"] is True and b["usage_complete"] is True else None)
            if a["group"] == "bug_cases":
                for metric in ("primary_line_overlap", "line_and_exact_witness_association", "line_witness_and_isolated_contrast"):
                    pair[metric] = {"causal": a.get(metric, False), "one_shot": b.get(metric, False)}
            else:
                pair["primary_candidate_present"] = {"causal": bool(a.get("primary_candidate_id")),
                                                      "one_shot": bool(b.get("primary_candidate_id"))}
        pairs.append(pair)
    result = {"experiment": manifest["experiment"], "manifest_sha256": sha(manifest_path),
              "analyzer_sha256": sha(Path(__file__)), "input_fingerprints": fingerprints,
              "state_hashes": state_hashes, "arms": arms, "pairs": pairs,
              "weighted_index_weights": {"input_miss": 1, "input_hit": 0.02, "output": 4},
              "limitations": manifest["limitations"] + ["Cases without a completed model response are unscorable, not successful abstentions.",
                  "The weighted index is not a provider invoice.",
                  "Line and exact-witness matches are proxies; semantic accuracy remains unmeasured."]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
