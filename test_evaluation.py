import unittest
import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

import evaluation
import root_scout
from test_evidence_probe import fixture_state


class EvaluationTests(unittest.TestCase):
    def test_v2_location_scoring_uses_verified_source_and_gold_witness_without_claiming_semantics(self):
        state=fixture_state()
        case={"case_id":"bug","category":"single","request":state["request_snapshot"],
              "expected_roots":[{"frozen_id":"init","source_line":3,"legal_trigger_input":"2\n-4 -2\n"}]}
        missing={"case_id":"clean","category":"clean","request":{},"expected_roots":[]}
        report=evaluation.score_v2_locations([case,missing],{"bug":state})
        self.assertEqual(report["cases_expected"],2)
        self.assertEqual(report["cases_verified"],1)
        self.assertEqual(report["cases_missing"],1)
        self.assertEqual(report["bug_cases"]["primary_line_overlap"],1)
        self.assertEqual(report["bug_cases"]["line_and_exact_witness_association"],1)
        self.assertIsNone(report["semantic_accuracy"])
        self.assertIsNone(report["usage"]["confirmed_tokens"])
        wrong=dict(case,expected_roots=[{"frozen_id":"other","source_line":2,"legal_trigger_input":"2\n-4 -2\n"}])
        self.assertEqual(evaluation.score_v2_locations([wrong],{"bug":state})["bug_cases"]["primary_line_overlap"],0)

    def test_v2_location_scoring_reads_frozen_development_fixture_results(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET)
        case=next(x for x in dataset if x["case_id"]=="max-init-python3")
        response={"candidates":[{"kind":"defect_candidate","ref":"init","claim":"Zero initialization misses negative maxima.",
                                 "locations":["line 3"],"evidence_ids":["T0002"]}],
                  "updates":[],"actions":[],"next_question":"","stop":True}
        def runner(_src,_lang,case_input,_env,profile="formal",build_cache=None):
            numbers=list(map(int,case_input["input"].split())); result=max([0]+numbers[1:1+numbers[0]])
            return {"status":"ok","returncode":0,"stdout":f"{result}\n","stderr":"","wall_seconds":0}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"fixture"}), \
             patch.object(root_scout,"run_case",side_effect=runner), \
             patch.object(root_scout.OpenAICompatible,"ask",return_value=response):
            state=root_scout.diagnose_v2(case["request"],strategy="one_shot")
        with tempfile.TemporaryDirectory() as directory:
            root_scout.write_probe_artifacts(state,Path(directory)/case["case_id"])
            states=evaluation.load_states([directory],reject_duplicates=True)
            report=evaluation.score_v2_locations(dataset,states)
        self.assertEqual(report["cases_verified"],1)
        self.assertEqual(report["cases_missing"],31)
        self.assertEqual(report["bug_cases"]["primary_line_overlap"],1)
        self.assertEqual(report["bug_cases"]["line_and_exact_witness_association"],1)
        self.assertNotIn("expected_roots",state["request_snapshot"])

    def test_exported_v2_requests_exclude_gold_and_do_not_overwrite_different_files(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET)
        with tempfile.TemporaryDirectory() as directory:
            paths=evaluation.export_requests(dataset,directory)
            self.assertEqual(len(paths),len(dataset))
            for case in dataset:
                request=json.loads((Path(directory)/(case["case_id"]+".json")).read_text(encoding="utf-8"))
                self.assertEqual(request,case["request"])
                self.assertNotIn("expected_roots",request)
            first=paths[0]; first.write_text("different",encoding="utf-8")
            with self.assertRaises(ValueError): evaluation.export_requests(dataset,directory)
            self.assertEqual(first.read_text(encoding="utf-8"),"different")
        with tempfile.TemporaryDirectory() as directory:
            second=Path(directory)/(dataset[1]["case_id"]+".json")
            second.write_text("different",encoding="utf-8")
            with self.assertRaises(ValueError): evaluation.export_requests(dataset,directory)
            self.assertFalse((Path(directory)/(dataset[0]["case_id"]+".json")).exists())
        unsafe=dict(dataset[0],case_id="../escape",request=dict(dataset[0]["request"],evaluation_case_id="../escape"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): evaluation.export_requests([unsafe],directory)

    def test_frozen_dataset_rejects_duplicate_case_ids(self):
        dataset=evaluation.read_jsonl(evaluation.DATASET)
        dataset[1]=dict(dataset[1],case_id=dataset[0]["case_id"],
                        request=dict(dataset[1]["request"],evaluation_case_id=dataset[0]["case_id"]))
        with self.assertRaises(ValueError): evaluation.validate_dataset(dataset)

    def test_v2_location_scoring_rejects_tampered_states_and_duplicate_results(self):
        state=fixture_state()
        case={"case_id":"bug","category":"single","request":state["request_snapshot"],
              "expected_roots":[{"frozen_id":"init","source_line":3,"legal_trigger_input":"2\n-4 -2\n"}]}
        state["evidence"][0]["result"]["stdout"]="forged"
        report=evaluation.score_v2_locations([case],{"bug":state})
        self.assertEqual(report["cases_invalid"],1)
        self.assertEqual(report["bug_cases"]["assessed"],0)
        with tempfile.TemporaryDirectory() as directory:
            for name in ("one","two"):
                path=Path(directory)/name/"case_state.json"; path.parent.mkdir()
                path.write_text(json.dumps({"request_snapshot":{"evaluation_case_id":"duplicate"}}),encoding="utf-8")
            with self.assertRaises(ValueError): evaluation.load_states([directory],reject_duplicates=True)

    def test_transport_failure_is_not_scored_as_a_successful_abstention(self):
        case=next(c for c in evaluation.read_jsonl(evaluation.DATASET) if c["case_id"]=="missing-spec-1-python3")
        error=urllib.error.HTTPError("http://example.invalid",503,"unavailable",{},None)
        with patch.dict(os.environ,{},clear=True), \
             patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"fixture"}), \
             patch.object(root_scout.OpenAICompatible,"ask",side_effect=error):
            state=root_scout.diagnose_v2(case["request"],strategy="one_shot")
        root_scout.verify_saved_case_state(state)
        report=evaluation.score_v2_locations([case],{case["case_id"]:state})
        self.assertEqual(report["cases_verified"],1)
        self.assertEqual(report["cases_unscorable"],1)
        self.assertEqual(report["cases_missing"],0)
        self.assertEqual(report["missing_spec_cases"]["assessed"],0)
        self.assertEqual(report["missing_spec_cases"]["no_defect_or_unclassified_candidate"],0)
        self.assertEqual(report["cases"][0]["state"],"unscorable_no_completed_response")

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
