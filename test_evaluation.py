import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import evaluation
import root_scout


class EvaluationTests(unittest.TestCase):
    def test_score_separates_diagnostic_failure_from_functional_and_resource_scope(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET)
        case=next(x for x in dataset if x["case_id"]=="range-double-cpp17")
        roots=[]
        for i,label in enumerate(case["expected_roots"],1):
            roots.append({"id":f"R{i}","evidence_ids":[f"e{i}"],"original_location":{"line":label["source_line"]},
                          "intervention":{"status":"supported"}})
        state={"request_snapshot":case["request"],"stop_reason":"completed_with_evidence","roots":roots,
               "evidence":[{"id":f"e{i}","input":label["legal_trigger_input"],"trusted_expected":True}
                           for i,label in enumerate(case["expected_roots"],1)],
               "budget":{"tokens_total":24800,"wall_seconds":60},
               "coverage":{"residual_status":"complete","known_explained_evidence_ids":["e1","e2"],
                           "execution_failures":[{"evidence_id":"e1","profile":"diagnostic","status":"program_failed"}]}}
        report=evaluation.score(dataset,{case["case_id"]:state})
        row=next(x for x in report["cases"] if x["case_id"]==case["case_id"])
        self.assertEqual(row["stop_reason"],"completed_with_evidence")
        self.assertEqual(row["functional_scope_status"],"verified_finite_scope_with_findings")
        self.assertEqual(row["diagnostic_failure_count"],1)
        self.assertEqual(row["resource_status"],"unverified")
        self.assertEqual(report["category_summary"]["double"]["tokens"],24800)
        self.assertIn("No judge time limit",report["resource_reason"])

    def test_trusted_test_facts_are_independent_of_raw_stop_and_protocol_classification(self):
        state={"stop_reason":"oracle_unresolved","evidence":[
            {"id":f"t{i}","trusted_expected":True,"result":{"status":"ok","matched":True}} for i in range(4)],
            "budget":{"calls":[{"phase":"INTERVENE","status":"failed","error":"ValueError","error_class":"schema_validation_failed"}]},
            "coverage":{"trusted_tests_available":4,"residual_status":"unresolved","execution_failures":[]},
            "run_configuration":{"runtime":{"available":True}}}
        scope=evaluation.classify_scope(state,"clean")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertEqual(scope["trusted_test_status"],"no_failure_observed")
        self.assertEqual(scope["trusted_test_count"],4)
        self.assertEqual(scope["trusted_test_executed"],4)
        self.assertEqual(scope["functional_scope_status"],"unresolved")
        self.assertEqual(scope["protocol_status"],"schema_validation_failed")
        self.assertEqual(scope["resource_status"],"unverified")

    def test_protocol_failure_counter_counts_classified_errors(self):
        dataset=[x for x in evaluation.read_jsonl(evaluation.DATASET) if x["case_id"]=="clean-max-cpp17"]
        state={"request_snapshot":dataset[0]["request"],"stop_reason":"no_bug_found_within_scope","roots":[],
               "evidence":[{"id":"t1","trusted_expected":True,"result":{"status":"ok","matched":True}}],
               "budget":{"tokens_total":1000,"wall_seconds":10,
                         "calls":[{"phase":"INTERVENE","status":"failed","error":"ValueError","error_class":"schema_validation_failed"}]},
               "coverage":{"trusted_tests_available":1,"residual_status":"complete","execution_failures":[]},
               "run_configuration":{"runtime":{"available":True}}}
        report=evaluation.score(dataset,{"clean-max-cpp17":state})
        self.assertEqual(report["cases"][0]["protocol_status"],"schema_validation_failed")
        self.assertEqual(report["category_summary"]["clean"]["protocol_failures"],1)

    def test_timeout_wall_and_admission_have_distinct_protocol_status_and_token_split(self):
        dataset=[x for x in evaluation.read_jsonl(evaluation.DATASET) if x["case_id"]=="clean-max-cpp17"]
        statuses=[]
        for error in ("model_call_timeout","wall_limit","token_budget_exhausted_before_call_reserved_report","wall_budget_exhausted_before_call"):
            call={"phase":"RESIDUAL","status":"failed" if "before_call" not in error else "rejected","error_class":error if error in ("model_call_timeout","wall_limit") else None,"error":error}
            state={"request_snapshot":dataset[0]["request"],"stop_reason":"budget_exhausted","roots":[],"evidence":[],
                   "budget":{"tokens_total":17,"confirmed_usage_tokens":11,"reserved_unknown_tokens":6,"usage_complete":False,"calls":[call]},
                   "coverage":{"trusted_tests_available":0,"residual_status":"budget_rejected","execution_failures":[]},
                   "run_configuration":{"runtime":{"available":True}}}
            report=evaluation.score(dataset,{dataset[0]["case_id"]:state})
            statuses.append(report["cases"][0]["protocol_status"])
            self.assertEqual(report["confirmed_usage_tokens"],11)
            self.assertEqual(report["reserved_unknown_tokens"],6)
            self.assertEqual(report["total_tokens_basis"],"confirmed usage plus reserved unknown; legacy field retained")
            expected_protocol_failures=1 if error in ("model_call_timeout","wall_limit") else 0
            self.assertEqual(report["category_summary"]["clean"]["protocol_failures"],expected_protocol_failures)
        self.assertEqual(statuses,["model_call_timeout","wall_limit","token_admission_rejected","wall_admission_rejected"])

    def test_frozen_corpus_has_required_variety_and_no_duplicate_requests(self):
        rows=evaluation.read_jsonl(evaluation.DATASET)
        self.assertEqual(len(rows),32)
        self.assertTrue({"clean","single","double","masked","triple","quad","missing_spec"}.issubset({r["category"] for r in rows}))
        kinds={r["request"].get("oracle_tool_contract",{}).get("kind") for r in rows}-{None}
        self.assertEqual(kinds,{"range_sum.v1","range_min.v1","max.v1"})
        self.assertEqual(len({(r["request"]["language"],r["request"]["statement"],r["request"]["source"]) for r in rows}),len(rows))
        self.assertTrue(all("expected_roots" not in r["request"] for r in rows))

    def test_score_matches_exact_line_and_trigger_once_and_keeps_failures_in_denominator(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET)
        case=next(x for x in dataset if x["case_id"]=="range-double-cpp17")
        labels=case["expected_roots"]
        state={"request_snapshot":case["request"],"stop_reason":"completed_with_evidence",
               "roots":[{"id":"R1","evidence_ids":["e1"],"original_location":{"line":labels[0]["source_line"]}},
                        {"id":"R2","evidence_ids":["e2"],"original_location":{"line":labels[1]["source_line"]}},
                        {"id":"R3","evidence_ids":["e1"],"original_location":{"line":labels[0]["source_line"]}}],
               "evidence":[{"id":"e1","input":labels[0]["legal_trigger_input"]},{"id":"e2","input":labels[1]["legal_trigger_input"]}],
               "budget":{"tokens_total":123,"wall_seconds":2},
               "coverage":{"residual_status":"unresolved","execution_failures":[]}}
        report=evaluation.score(dataset,{case["case_id"]:state})
        result=next(x for x in report["cases"] if x["case_id"]==case["case_id"])
        self.assertEqual((result["TP"],result["FP"],result["FN"]),(2,1,0))
        self.assertEqual(result["match_method"],"rule_proxy_line_and_trigger")
        self.assertEqual(report["all_root_exact_denominator"],18)
        self.assertEqual(report["unresolved_count"],32)
        self.assertEqual(report["total_tokens"],123)
        self.assertEqual(report["missing_results"],31)

    def test_empty_manual_review_is_authoritative_and_never_falls_back_to_proxy(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET)
        case=next(x for x in dataset if x["case_id"]=="range-double-cpp17")
        label=case["expected_roots"][0]
        state={"request_snapshot":case["request"],"stop_reason":"failed","roots":[{"id":"R1","evidence_ids":["e1"],"original_location":{"line":label["source_line"]}}],
               "evidence":[{"id":"e1","input":label["legal_trigger_input"]}],"budget":{},"coverage":{"residual_status":"failed"}}
        reviews={case["case_id"]:{"case_id":case["case_id"],"reviewer":"reviewer-1","matches":[]}}
        report=evaluation.score(dataset,{case["case_id"]:state},reviews)
        result=next(x for x in report["cases"] if x["case_id"]==case["case_id"])
        self.assertEqual((result["TP"],result["FP"],result["FN"]),(0,1,2))
        self.assertEqual(result["match_method"],"human_review")
        self.assertEqual(result["reviewer"],"reviewer-1")

    def test_score_rejects_same_case_id_with_a_different_frozen_request(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET); case=dataset[0]
        state={"request_snapshot":dict(case["request"],source="different"),"stop_reason":"no_bug_found_within_scope",
               "roots":[],"evidence":[],"budget":{"tokens_total":1,"wall_seconds":1},"coverage":{"residual_status":"complete"}}
        report=evaluation.score(dataset,{case["case_id"]:state})
        self.assertEqual(report["results_available"],0)
        self.assertEqual(report["request_mismatches"],[case["case_id"]])
        self.assertEqual(report["cases"][0]["stop_reason"],"result_request_mismatch")

    def test_evaluation_runner_preserves_checkpointed_usage_on_failure(self):
        case=evaluation.read_jsonl(evaluation.DATASET)[0]
        def fail_after_checkpoint(_req,_provider,path):
            checkpoint={"id":"x","request_snapshot":case["request"],"roots":[],"evidence":[],
                        "budget":{"tokens_total":321,"wall_seconds":4.5,"usage_complete":False,"calls":[{"status":"started"}]},
                        "coverage":{"residual_status":"started","execution_failures":[],"untested":[]},"contract":[],"assumptions":[]}
            Path(path).write_text(json.dumps(checkpoint),encoding="utf-8")
            raise RuntimeError("fixture failure")
        with tempfile.TemporaryDirectory() as td, patch.object(root_scout,"diagnose",side_effect=fail_after_checkpoint):
            out=evaluation.run_dataset([case],td,"replay")
            saved=json.loads((Path(out)/case["case_id"]/"case_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["stop_reason"],"failed")
        self.assertEqual(saved["budget"]["tokens_total"],321)
        self.assertEqual(saved["budget"]["wall_seconds"],4.5)


if __name__=="__main__":
    unittest.main()
