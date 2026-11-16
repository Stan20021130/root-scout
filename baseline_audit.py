"""Audit saved v2 diagnoses without calling a model or treating candidate labels as accuracy."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics

import root_scout


def audit(manifest_path, results_dir, weights=(1.0, 0.02, 4.0)):
    manifest_path=Path(manifest_path).resolve()
    dataset_root=manifest_path.parent
    results_dir=Path(results_dir).resolve()
    manifest_bytes=manifest_path.read_bytes()
    manifest=json.loads(manifest_bytes)
    programs=manifest.get("programs")
    if not isinstance(programs,list): raise ValueError("manifest must contain a programs array")
    if len(weights)!=3 or any(not isinstance(x,(int,float)) or not math.isfinite(x) or x<0 for x in weights):
        raise ValueError("cost weights must be three nonnegative finite numbers")
    roles=defaultdict(Counter); statuses=Counter(); rows=[]; seen=set()
    durations=[]; known_cost=0.0; incomplete_cost=0; calls_total=0; confirmed_tokens=0; missing_confirmed=0
    for entry in programs:
        case_id=entry.get("id"); role=entry.get("role_for_evaluation_only","unknown")
        if not isinstance(case_id,str) or not case_id or Path(case_id).name!=case_id or case_id in (".","..") or case_id in seen:
            raise ValueError("manifest case IDs must be unique single path components")
        seen.add(case_id); roles[role]["expected"]+=1
        row={"id":case_id,"role":role,"tier":entry.get("tier_for_evaluation_only","unknown")}
        state_path=results_dir/case_id/"diagnose"/"case_state.json"
        if not state_path.is_file():
            row["state"]="missing"; incomplete_cost+=1; rows.append(row); continue
        try:
            state_bytes=state_path.read_bytes()
            row["state_sha256"]=hashlib.sha256(state_bytes).hexdigest()
            request_path=(dataset_root/entry["request_path"]).resolve()
            if not request_path.is_relative_to(dataset_root): raise ValueError("request path leaves the dataset")
            request_bytes=request_path.read_bytes()
            if hashlib.sha256(request_bytes).hexdigest()!=entry.get("request_file_sha256"):
                raise ValueError("request file hash does not match the manifest")
            request=json.loads(request_bytes)
            state=json.loads(state_bytes)
            root_scout.verify_saved_case_state(state)
            if root_scout.canonical_sha(state["request_snapshot"])!=root_scout.canonical_sha(request):
                raise ValueError("saved request does not match the dataset")
            if state.get("diagnostic_core_version")!=2: raise ValueError("result is not a v2 diagnosis")
        except (OSError,ValueError,TypeError,KeyError) as exc:
            row.update(state="invalid",reason=str(exc)[:240]); incomplete_cost+=1; rows.append(row); continue
        row["state"]="verified"; roles[role]["verified"]+=1
        probe=root_scout.probe_result(state); statuses[probe["status"]]+=1
        active=[c for c in state.get("candidates",[]) if c.get("lifecycle") not in ("withdrawn","merged")]
        strict=any(c.get("candidate_kind")=="defect_candidate" for c in active)
        broad=any(c.get("candidate_kind") in ("defect_candidate","unclassified") for c in active)
        localization=root_scout.v2_localization(state)
        sites=localization.get("sites",[])
        anchored=bool(sites and sites[0].get("site_check")=="ok")
        mismatch=probe["status"]=="counterexample_found"
        contrast=any(f.get("causal_check",{}).get("status")=="isolated_contrast_observed" for f in probe["findings"])
        for key,value in (("active_defect_candidate",strict),("active_defect_or_unclassified",broad),
                          ("primary_location_syntactically_usable",anchored),("trusted_counterexample",mismatch),
                          ("isolated_contrast_observed",contrast)):
            roles[role][key]+=int(value); row[key]=value
        row["probe_status"]=probe["status"]; row["stop_reason"]=state.get("stop_reason")
        budget=state.get("budget",{}); calls=budget.get("calls",[])
        attempted=[c for c in calls if c.get("status") not in ("skipped","rejected")]
        calls_total+=len(attempted)
        complete=budget.get("usage_complete") is True
        case_cost=0.0
        for call in attempted:
            numbers=[call.get("cache_miss_tokens"),call.get("cache_hit_tokens"),call.get("output_tokens")]
            if call.get("usage_complete") is not True or any(type(x) is not int or x<0 for x in numbers):
                complete=False; continue
            case_cost+=sum(w*x for w,x in zip(weights,numbers))
        known_cost+=case_cost
        confirmed=budget.get("confirmed_usage_tokens")
        if type(confirmed) is int and confirmed>=0: confirmed_tokens+=confirmed
        else: complete=False; missing_confirmed+=1
        if not complete: incomplete_cost+=1
        wall=budget.get("wall_seconds")
        if isinstance(wall,(int,float)) and not isinstance(wall,bool) and math.isfinite(wall) and wall>=0: durations.append(wall)
        row.update(model_calls=len(attempted),confirmed_tokens=confirmed,usage_complete=complete,
                   known_cost_index=round(case_cost,2),wall_seconds=wall)
        rows.append(row)
    durations.sort()
    counts=Counter(row["state"] for row in rows)
    return {"schema_version":"rootscout.baseline_audit.v1","dataset_id":manifest.get("dataset_id"),
            "manifest_sha256":hashlib.sha256(manifest_bytes).hexdigest(),
            "results_sha256":root_scout.canonical_sha(sorted((r["id"],r.get("state_sha256")) for r in rows)),
            "auditor_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "delivery_engine_sha256":hashlib.sha256(Path(root_scout.__file__).read_bytes()).hexdigest(),
            "case_counts":{"expected":len(programs),
            "verified":counts["verified"],"missing":counts["missing"],"invalid":counts["invalid"]},
            "role_counts":{role:dict(values) for role,values in sorted(roles.items())},"probe_statuses":dict(statuses),
            "accuracy":{"defect_accuracy":None,"location_accuracy":None,
                        "reason":"This audit has no independently reviewed defect and source-location labels."},
            "usage":{"attempted_model_calls":calls_total,"known_confirmed_tokens":confirmed_tokens,
                     "confirmed_tokens":confirmed_tokens if not missing_confirmed and not counts["missing"] and not counts["invalid"] else None,
                     "incomplete_or_unavailable_cases":incomplete_cost,"known_weighted_cost_index":round(known_cost,2),
                     "weighted_cost_index":round(known_cost,2) if not incomplete_cost else None,
                     "weights":{"input_miss":weights[0],"input_hit":weights[1],"output":weights[2]}},
            "wall_seconds":{"observed":len(durations),"median":round(statistics.median(durations),2) if durations else None,
                            "p90":round(durations[math.ceil(len(durations)*0.9)-1],2) if durations else None},
            "limitations":["Candidate counts are not defect or root-cause accuracy.",
                           "A usable source anchor does not establish a correct location or cause.",
                           "Control labels are evaluation labels and may contain real defects.",
                           "The weighted cost index is not a provider invoice; incomplete usage is unknown.",
                           "Saved results describe the engine and model used for those runs, not a new live evaluation."],
            "cases":rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",required=True); parser.add_argument("--results",required=True)
    parser.add_argument("--weights",nargs=3,type=float,metavar=("MISS","HIT","OUTPUT"),default=(1.0,0.02,4.0))
    parser.add_argument("--out")
    args=parser.parse_args(); report=audit(args.manifest,args.results,args.weights)
    output=json.dumps(report,ensure_ascii=False,indent=2)+"\n"
    if args.out: Path(args.out).write_text(output,encoding="utf-8")
    else: print(output,end="")
    return int(bool(report["case_counts"]["missing"] or report["case_counts"]["invalid"]))


if __name__=="__main__":
    raise SystemExit(main())
