"""Combine the pilot and its complete-set extension without model calls."""
import contextlib
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import analyze
import evaluation


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(here, output):
    analyze.HERE = here
    analyze.OUTPUT = output
    text = io.StringIO()
    with contextlib.redirect_stdout(text):
        analyze.main()
    return json.loads(text.getvalue())


def add_groups(a, b):
    if set(a) != set(b):
        raise ValueError("group metrics differ between experiment batches")
    return {key: a[key] + b[key] for key in a}


def unpack_known_evidence(archive, temporary, batches):
    with zipfile.ZipFile(archive) as evidence:
        names = evidence.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive paths")
        available = set(names)
        for batch, manifest in batches:
            paths = ["fingerprints.json"]
            for case_id in manifest["case_ids"]:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", case_id):
                    raise ValueError("unsafe case ID")
                for arm in manifest["arms"]:
                    if arm not in ("causal", "one_shot"):
                        raise ValueError("unknown arm")
                    pair_paths = (f"{arm}/{case_id}/run.json", f"{arm}/{case_id}/diagnose/case_state.json")
                    present = [f"{batch}/{name}" in available for name in pair_paths]
                    if any(present) and not all(present):
                        raise ValueError(f"incomplete archived run: {batch}/{arm}/{case_id}")
                    if all(present):
                        paths.extend(pair_paths)
            for name in paths:
                target = temporary / batch / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(evidence.read(f"{batch}/{name}"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", help="read the packaged evidence instead of the local validation directory")
    args = parser.parse_args()
    extension = HERE.parent / "2026-11-10-paired-v2-extension"
    pilot_manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    ext_manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    if args.archive:
        with tempfile.TemporaryDirectory(prefix="rootscout-paired-evidence-") as directory:
            temporary = Path(directory)
            unpack_known_evidence(args.archive, temporary, [("pilot", pilot_manifest), ("extension", ext_manifest)])
            first = collect(HERE, temporary / "pilot")
            second = collect(extension, temporary / "extension")
    else:
        first = collect(HERE, ROOT / "validation" / "paired-v2-2026-11-10")
        second = collect(extension, ROOT / "validation" / "paired-v2-2026-11-10-extension")
    if ext_manifest["pilot_manifest_sha256"] != sha(HERE / "manifest.json"):
        raise ValueError("extension is not tied to this pilot")
    ids = pilot_manifest["case_ids"] + ext_manifest["case_ids"]
    expected = [case["case_id"] for case in evaluation.read_jsonl(evaluation.DATASET)]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("paired manifests do not cover the development set exactly once")
    arms = {}
    for arm in pilot_manifest["arms"]:
        a, b = first["arms"][arm], second["arms"][arm]
        usage_a, usage_b = a["usage"], b["usage"]
        confirmed = (usage_a["confirmed_tokens"] + usage_b["confirmed_tokens"]
                     if usage_a["confirmed_tokens"] is not None and usage_b["confirmed_tokens"] is not None else None)
        index = a["weighted_index"] + b["weighted_index"] if a["weighted_index"] is not None and b["weighted_index"] is not None else None
        arms[arm] = {"cases_expected": a["cases_expected"] + b["cases_expected"],
                     "cases_verified": a["cases_verified"] + b["cases_verified"],
                     "cases_unscorable": a["cases_unscorable"] + b["cases_unscorable"],
                     "cases_missing": a["cases_missing"] + b["cases_missing"],
                     "cases_invalid": a["cases_invalid"] + b["cases_invalid"],
                     "bug_cases": add_groups(a["bug_cases"], b["bug_cases"]),
                     "clean_cases": add_groups(a["clean_cases"], b["clean_cases"]),
                     "missing_spec_cases": add_groups(a["missing_spec_cases"], b["missing_spec_cases"]),
                     "semantic_accuracy": None,
                     "usage": {"known_confirmed_tokens": usage_a["known_confirmed_tokens"] + usage_b["known_confirmed_tokens"],
                               "known_reserved_unknown_tokens": usage_a["known_reserved_unknown_tokens"] + usage_b["known_reserved_unknown_tokens"],
                               "incomplete_or_unavailable_cases": usage_a["incomplete_or_unavailable_cases"] + usage_b["incomplete_or_unavailable_cases"],
                               "confirmed_tokens": confirmed},
                     "attempted_model_calls": a["attempted_model_calls"] + b["attempted_model_calls"],
                     "weighted_index": round(index, 2) if index is not None else None,
                     "cases": a["cases"] + b["cases"]}
    pairs = first["pairs"] + second["pairs"]
    matched = [pair for pair in pairs if pair["both_scorable"]]
    case_rows = {arm: {row["case_id"]: row for row in arms[arm]["cases"]} for arm in pilot_manifest["arms"]}
    complete_pairs = [pair for pair in matched if all(case_rows[arm][pair["case_id"]]["usage_complete"] is True
                                                       for arm in pilot_manifest["arms"])]
    paired_usage = {"pair_count": len(complete_pairs)}
    for arm in pilot_manifest["arms"]:
        rows = [case_rows[arm][pair["case_id"]] for pair in complete_pairs]
        indices = [row["weighted_index"] for row in rows]
        paired_usage[arm] = {"confirmed_tokens": sum(row["confirmed_tokens"] for row in rows),
                             "attempted_model_calls": sum(row["attempted_model_calls"] for row in rows),
                             "weighted_index": round(sum(indices), 2) if all(i is not None for i in indices) else None}
    result = {"experiment": "2026-11-10-paired-v2-full-development-set", "analyzer_sha256": sha(Path(__file__)),
              "batch_analyzer_sha256": sha(HERE / "analyze.py"),
              "batch_manifests_sha256": [first["manifest_sha256"], second["manifest_sha256"]],
              "input_fingerprints": [first["input_fingerprints"], second["input_fingerprints"]],
              "state_hashes": first["state_hashes"] + second["state_hashes"],
              "arms": arms, "scorable_pair_count": len(matched), "paired_complete_usage": paired_usage, "pairs": pairs,
              "weighted_index_weights": first["weighted_index_weights"],
              "limitations": ext_manifest["limitations"] + ["Cases without a completed model response are unscorable, not successful abstentions.",
                  "Failed requests may have unreported provider usage; their reservation is not an actual charge.",
                  "The weighted index is not a provider invoice.",
                  "Line and exact-witness matches are proxies; semantic accuracy remains unmeasured."]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
