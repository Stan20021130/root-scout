import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import baseline_audit
import root_scout
from test_evidence_probe import fixture_state


class BaselineAuditTests(unittest.TestCase):
    def prepare(self, directory, state, include_missing=False):
        root=Path(directory); dataset=root/"dataset"; results=root/"results"
        (dataset/"inputs").mkdir(parents=True)
        request_bytes=json.dumps(state["request_snapshot"],ensure_ascii=False).encode("utf-8")
        (dataset/"inputs"/"bug.json").write_bytes(request_bytes)
        programs=[{"id":"bug","role_for_evaluation_only":"bug","tier_for_evaluation_only":"easy",
                   "request_path":"inputs/bug.json","request_file_sha256":hashlib.sha256(request_bytes).hexdigest()}]
        if include_missing:
            programs.append(dict(programs[0],id="missing",role_for_evaluation_only="control"))
        manifest=dataset/"manifest.json"
        manifest.write_text(json.dumps({"dataset_id":"fixture","programs":programs}),encoding="utf-8")
        out=results/"bug"/"diagnose"; out.mkdir(parents=True)
        (out/"case_state.json").write_text(json.dumps(state,ensure_ascii=False),encoding="utf-8")
        return manifest,results,out/"case_state.json"

    def state(self):
        state=fixture_state()
        state["budget"].update(confirmed_usage_tokens=115,usage_complete=True,calls=[
            {"phase":"EXPLORE_V2","status":"ok","usage_complete":True,"cache_miss_tokens":10,
             "cache_hit_tokens":100,"output_tokens":5,"confirmed_usage_tokens":115}])
        state["integrity"]=root_scout.v2_integrity(state)
        return state

    def test_missing_cases_remain_in_denominator_and_cost_is_not_treated_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest,results,_=self.prepare(directory,self.state(),include_missing=True)
            report=baseline_audit.audit(manifest,results)
        self.assertEqual(report["case_counts"],{"expected":2,"verified":1,"missing":1,"invalid":0})
        self.assertEqual(report["role_counts"]["bug"]["trusted_counterexample"],1)
        self.assertEqual(report["role_counts"]["control"]["expected"],1)
        self.assertIsNone(report["accuracy"]["location_accuracy"])
        self.assertEqual(report["usage"]["known_weighted_cost_index"],32.0)
        self.assertIsNone(report["usage"]["weighted_cost_index"])
        self.assertIsNone(report["usage"]["confirmed_tokens"])

    def test_invalid_ledger_cannot_be_counted_as_a_successful_case(self):
        state=self.state()
        state["evidence"][0]["result"]["stdout"]="forged"
        with tempfile.TemporaryDirectory() as directory:
            manifest,results,_=self.prepare(directory,state)
            report=baseline_audit.audit(manifest,results)
        self.assertEqual(report["case_counts"]["invalid"],1)
        self.assertEqual(report["case_counts"]["verified"],0)
        self.assertIsNone(report["usage"]["weighted_cost_index"])

    def test_incomplete_failed_call_is_included_but_its_cost_stays_unknown(self):
        state=self.state()
        state["budget"]["calls"].append({"phase":"EXPLORE_V2","status":"failed","usage_complete":False})
        state["budget"]["usage_complete"]=False
        state["integrity"]=root_scout.v2_integrity(state)
        with tempfile.TemporaryDirectory() as directory:
            manifest,results,_=self.prepare(directory,state)
            report=baseline_audit.audit(manifest,results)
        self.assertEqual(report["usage"]["attempted_model_calls"],2)
        self.assertEqual(report["usage"]["incomplete_or_unavailable_cases"],1)
        self.assertEqual(report["usage"]["known_weighted_cost_index"],32.0)
        self.assertIsNone(report["usage"]["weighted_cost_index"])


if __name__=="__main__":
    unittest.main()
