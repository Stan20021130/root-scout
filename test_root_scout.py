import unittest
import json
import os
import copy
import argparse
import sys
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import root_scout
import oracle_tools

class ValidationTests(unittest.TestCase):
    def test_phase_protocol_is_explicit_and_real_intervene_drift_is_rejected(self):
        paths=["validation/ten-case-2026-10-27/runs/max-double-python3/case_state.json",
               "validation/ten-case-2026-10-27/runs/range-endpoint-cpp17/case_state.json"]
        for path in paths:
            with open(path,encoding="utf-8") as f: state=json.load(f)
            call=next(c for c in state["budget"]["calls"] if c.get("phase")=="INTERVENE")
            raw=json.loads(call["raw_content"])
            self.assertIn("contract",raw)
            with self.assertRaisesRegex(ValueError,"only patches"):
                root_scout.validate_model_result(raw,"INTERVENE")
        prompt=root_scout.system_for_phase("INTERVENE")
        self.assertIn("Output exactly {patches",prompt)
        self.assertNotIn("Return keys contract",prompt)
        root_scout.validate_model_result({"patches":[]},"INTERVENE")

    def test_root_merge_is_exact_span_and_repair_based_not_line_based(self):
        original={"id":"R1","source_line":8,"source_excerpt":"cout<<p[r]-p[l];","contract_quote":"inclusive sum",
                  "repair_direction":"replace p[l] with p[l-1]","claim":"omit endpoint"}
        duplicate=dict(original,id="candidate",claim="same defect in other words")
        different_same_line=dict(duplicate,source_excerpt="p[r]-p[l]")
        different_repair=dict(duplicate,repair_direction="subtract a[l] after prefix query")
        ambiguous=[original,dict(original,id="R2")]
        self.assertIs(root_scout.root_merge_target(duplicate,[original])[0],original)
        self.assertEqual(root_scout.root_merge_target(different_same_line,[original]),(None,None))
        self.assertEqual(root_scout.root_merge_target(different_repair,[original]),(None,None))
        self.assertEqual(root_scout.root_merge_target(dict(duplicate,existing_root_id="R9"),[original])[1],"existing_root_id_does_not_match_exact_span_and_repair")
        self.assertEqual(root_scout.root_merge_target(duplicate,ambiguous),(None,None))
        target=copy.deepcopy(original)
        candidate=dict(duplicate,evidence_ids=["trusted-2"])
        self.assertEqual(root_scout.attach_root_evidence(target,candidate,"exact_signature"),["trusted-2"])
        self.assertEqual(target["evidence_ids"],["trusted-2"])
        self.assertEqual(root_scout.attach_root_evidence(target,candidate,"exact_signature"),[])

    def test_shared_exact_anchor_with_different_repair_needs_disambiguation(self):
        statement="Inclusive range sum."
        req={"statement":statement,"source":"print(0)","language":"python3","known_failing_tests":[
            {"input":"a","expected":"1","trusted":True,"expected_source":"checked","legal_input":True,"legal_input_source":"valid"},
            {"input":"b","expected":"2","trusted":True,"expected_source":"checked","legal_input":True,"legal_input_source":"valid"}]}
        plan={"contract":[{"clause":"inclusive sum","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        first={"claim":"first root","contract_quote":statement,"source_line":1,"source_excerpt":"print(0)","trigger":"a","causal_explanation":"first explanation","repair_direction":"subtract prefix before left endpoint","evidence_ids":["trusted-1"]}
        second=dict(first,claim="possibly same issue, maybe another mechanism",causal_explanation="different explanation",repair_direction="widen the accumulator",evidence_ids=["trusted-2"])
        answers=[plan,{"roots":[first,second]},{"patches":[]},{"residual_audit":[],"next_checks":[],"assumptions":[]}]
        def runner(_source,_lang,case,_env,profile="formal",build_cache=None):
            out="0\n"
            return {"status":"ok","stdout":out,"stderr":"","matched":out==case["expected"],"wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner), \
             patch.object(root_scout.OpenAICompatible,"ask",side_effect=answers):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(len(state["roots"]),1)
        self.assertEqual(state["root_decisions"][1]["status"],"needs_disambiguation")
        self.assertEqual(state["ambiguous_root_candidates"][0]["possible_existing_root_ids"],["R1"])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        legacy=copy.deepcopy(state); legacy["roots"][0].pop("intervention",None)
        self.assertEqual(root_scout.verify_saved_case_state(legacy),req)
        tampered=copy.deepcopy(legacy); tampered["coverage"]["intervention_status"]="complete"
        with self.assertRaises(ValueError): root_scout.verify_saved_case_state(tampered)

    def test_diagnose_merges_compact_existing_root_evidence_after_e2_gate(self):
        statement="Output 9 exactly."
        req={"statement":statement,"source":"print(0)","language":"python3","known_failing_tests":[
            {"input":"a","expected":"9","trusted":True,"expected_source":"checked","legal_input":True,"legal_input_source":"valid input"},
            {"input":"b","expected":"9","trusted":True,"expected_source":"checked","legal_input":True,"legal_input_source":"valid input"}]}
        plan={"contract":[{"clause":"output 9","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        root={"claim":"constant output is wrong","contract_quote":statement,"source_line":1,"source_excerpt":"print(0)","trigger":"always","causal_explanation":"Prints zero instead of nine.","repair_direction":"print 9","evidence_ids":["trusted-1"]}
        answers=[plan,{"roots":[root,{"existing_root_id":"R1","evidence_ids":["trusted-2"]}]},
                 {"patches":[{"root_id":"R1","source_line":1,"old_text":"print(0)","new_text":"print(9)"}]},
                 {"residual_audit":[],"next_checks":[],"assumptions":[]}]
        def runner(source,_lang,_case,_env,profile="formal",build_cache=None):
            out="9\n" if "print(9)" in source else "0\n"
            return {"status":"ok","stdout":out,"stderr":"","matched":out=="9\n","wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner), \
             patch.object(root_scout.OpenAICompatible,"ask",side_effect=answers):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(len(state["roots"]),1)
        self.assertEqual(state["roots"][0]["evidence_ids"],["trusted-1","trusted-2"])
        self.assertEqual([x["status"] for x in state["root_decisions"]], ["accepted","merged"])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_requires_provenance_for_trusted_expected(self):
        with self.assertRaises(ValueError):
            root_scout.validate_request({"statement":"s","source":"x","language":"python3","known_failing_tests":[{"input":"","expected":"x","trusted":True,"legal_input":True,"legal_input_source":"by hand"}]})

    def test_untrusted_test_rejected(self):
        with self.assertRaises(ValueError):
            root_scout.validate_request({"statement":"s","source":"x","language":"python3","known_failing_tests":[{"input":"","expected":"x","expected_source":"model","legal_input":True,"legal_input_source":"by hand"}]})

    def test_original_hash_stable(self):
        self.assertEqual(root_scout.sha("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_mismatch_is_only_suspected_without_semantic_adjudication(self):
        req={"statement":"要求输出 9","source":"print(0)","language":"python3","known_failing_tests":[{"input":"","expected":"9","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"hand checked"}]}
        with patch.object(root_scout,"docker_run",return_value={"status":"ok","stdout":"0","stderr":"","matched":False}):
            state=root_scout._diagnose_legacy(req,provider="replay")
        self.assertEqual(state["evidence"][0]["result"]["evidence_level"],"E1")
        self.assertEqual(state["roots"],[])
        self.assertEqual(state["suspected_failures"][0]["status"],"suspected")

    def test_cpp_sample_source_uses_a_newline_character_not_multichar_literal(self):
        import json
        with open("examples/prefix_overflow.json",encoding="utf-8") as f: source=json.load(f)["source"]
        self.assertIn("<<'\\n';", source)
        self.assertNotIn("<<'\\\\n';", source)

    def test_extracts_exact_statement_sample_pairs(self):
        statement="样例输入 1:\n```text\n2\n```\n样例输出 1:\n```text\n3\n```"
        cases=root_scout.extract_samples(statement)
        self.assertEqual((cases[0]["input"],cases[0]["expected"]),("2","3"))
        self.assertIn("```text\n2\n```",cases[0]["input_statement_quote"])
        self.assertIn("```text\n3\n```",cases[0]["expected_statement_quote"])

    def test_sample_heading_without_immediate_fence_is_not_trusted(self):
        statement="样例输入 1:\n缺少代码块\n样例输出 1:\n```text\nnot an input-output pair\n```"
        self.assertEqual(root_scout.extract_samples(statement),[])

    def test_saved_case_state_detects_source_tampering(self):
        state={"request_snapshot":{"statement":"s","source":"x","language":"python3"},"originals":{"statement_sha256":root_scout.sha("s"),"source_sha256":"wrong","environment_sha256":root_scout.sha("{}")}}
        with self.assertRaises(ValueError): root_scout.verify_saved_case_state(state)

    def test_saved_case_state_binds_request_tests_config_and_diagnostic_logs(self):
        req={"statement":"要求 9","source":"print(0)","language":"python3","known_failing_tests":[{"input":"","expected":"9","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"valid"}]}
        with patch.object(root_scout,"docker_preflight",return_value={"available":False,"reason":"fixture"}):
            state=root_scout._diagnose_legacy(req,provider="replay")
        self.assertEqual(state["run_configuration"]["sandbox"]["stdout_bytes_max"],root_scout.SANDBOX_STDOUT_LIMIT_BYTES)
        self.assertEqual(state["run_configuration"]["sandbox"]["stderr_bytes_max"],root_scout.SANDBOX_STDERR_LIMIT_BYTES)
        self.assertEqual(state["run_configuration"]["sandbox"]["output_bytes_max"],root_scout.SANDBOX_OUTPUT_LIMIT_BYTES)
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        mutated=copy.deepcopy(state); mutated["request_snapshot"]["known_failing_tests"][0]["expected"]="8"
        with self.assertRaises(ValueError): root_scout.verify_saved_case_state(mutated)
        mutated=copy.deepcopy(state); mutated["evidence"][0]["result"]["diagnostic_build"]["stderr"]="tampered"
        with self.assertRaises(ValueError): root_scout.verify_saved_case_state(mutated)
        mutated=copy.deepcopy(state); mutated["run_configuration"]["sandbox"]["memory"]="1g"
        with self.assertRaises(ValueError): root_scout.verify_saved_case_state(mutated)
        mutated=copy.deepcopy(state); mutated["evidence"][0]["expected"]="8"; mutated["evidence"][0]["expected_sha256"]=root_scout.sha("8")
        with self.assertRaises(ValueError): root_scout.verify_saved_case_state(mutated)
        partial=copy.deepcopy(state); partial["evidence"]=[]; partial["integrity"]["evidence_sha256"]=root_scout.canonical_sha([])
        self.assertEqual(root_scout.verify_saved_case_state(partial),req)

    def test_legacy_snapshot_is_explicitly_refused(self):
        old={"request_snapshot":{"statement":"s","source":"x","language":"python3"},"originals":{"statement_sha256":root_scout.sha("s"),"source_sha256":root_scout.sha("x"),"environment_sha256":root_scout.sha("{}")},"evidence":[]}
        with self.assertRaisesRegex(ValueError,"legacy CaseState lacks integrity metadata"):
            root_scout.verify_saved_case_state(old)

    def test_bad_root_reference_does_not_promote(self):
        req={"statement":"要求输出 9","source":"print(0)","language":"python3","known_failing_tests":[{"input":"","expected":"9","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"valid"}]}
        responses=[{"contract":[{"clause":"output","status":"stated","statement_quote":"要求输出 9"}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]},
                   {"roots":[{"claim":"bad","contract_quote":"要求输出 9","source_line":1,"trigger":"always","causal_explanation":"wrong","repair_direction":"fix","evidence_ids":["not-an-evidence-id"]}]},
                   {"residual_audit":[],"next_checks":[],"assumptions":[]}]
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), patch.object(root_scout,"docker_run",side_effect=lambda *_args,**_kwargs:{"status":"ok","stdout":"0","stderr":"","matched":False}), patch.object(root_scout.OpenAICompatible,"ask",side_effect=responses):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["roots"],[])
        self.assertTrue(state["coverage"]["untested"])

    def test_malformed_root_schema_and_report_are_safe(self):
        with self.assertRaises(ValueError): root_scout.validate_model_result({"roots":[{"contract_quote":"q"}]},"ADJUDICATE")
        self.assertIn("R1",root_scout.markdown({"stop_reason":"oracle_unresolved","id":"x","roots":[{"id":"R1"}],"budget":{},"coverage":{"untested":[]},"contract":[],"assumptions":[]}))

    def test_failed_model_response_keeps_reported_usage(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":"not json"}}],"usage":{"prompt_tokens":123,"completion_tokens":456}}).encode()
        budget=root_scout.Budget()
        with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}), patch.object(root_scout.urllib.request,"urlopen",return_value=Response()) as urlopen:
            with self.assertRaises(json.JSONDecodeError): root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",budget)
        request_body=json.loads(urlopen.call_args.args[0].data)
        self.assertIn("Output exactly {residual_audit",request_body["messages"][0]["content"])
        self.assertNotIn("Return keys contract",request_body["messages"][0]["content"])
        self.assertEqual(budget.total(),579)
        self.assertEqual(budget.calls[0]["input_tokens"],123)
        self.assertEqual(budget.calls[0]["output_tokens"],456)
        self.assertEqual(budget.calls[0]["raw_content"],"not json")
        self.assertEqual(budget.calls[0]["raw_sha256"],root_scout.sha("not json"))
        self.assertEqual(budget.calls[0]["finish_reason"],"stop")
        self.assertFalse(budget.calls[0]["raw_content_truncated"])

    def test_null_usage_is_unknown_reservation_not_confirmed_zero(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                content=json.dumps({"residual_audit":[],"next_checks":[],"assumptions":[]})
                usage={"prompt_tokens":None,"completion_tokens":None,"prompt_cache_hit_tokens":None,"prompt_cache_miss_tokens":None}
                return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":content}}],"usage":usage}).encode()
        budget=root_scout.Budget()
        settings={"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}
        with patch.dict(os.environ,settings),patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",budget)
        call=budget.calls[0]
        self.assertEqual(call["confirmed_usage_tokens"],0)
        self.assertEqual(call["reserved_unknown_tokens"],call["input_reservation"]+call["max_output_reserved"])
        self.assertFalse(call["usage_complete"])
        self.assertNotIn("cache_hit_tokens",call)

    def test_length_finish_with_valid_json_is_accepted_and_flagged(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                content=json.dumps({"residual_audit":[],"next_checks":[],"assumptions":[]})
                return json.dumps({"choices":[{"finish_reason":"length","message":{"content":content}}],"usage":{"prompt_tokens":10,"completion_tokens":6000}}).encode()
        budget=root_scout.Budget()
        with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}), patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            answer=root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",budget)
        self.assertEqual(answer["next_checks"],[])
        self.assertEqual(budget.calls[0]["finish_reason"],"length")
        self.assertTrue(budget.calls[0]["finish_reason_incomplete"])

    def test_intervene_length_json_truncation_gets_one_same_cap_recovery(self):
        class Response:
            def __init__(self,content,finish,completion): self.content=content; self.finish=finish; self.completion=completion
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":self.finish,"message":{"content":self.content}}],"usage":{"prompt_tokens":10,"completion_tokens":self.completion}}).encode()
        first=Response('{"patches":[',"length",6000)
        second=Response('{"patches":[]}',"stop",20)
        requests=[]
        def mock_urlopen(request,**_kwargs): requests.append(json.loads(request.data)); return [first,second][len(requests)-1]
        budget=root_scout.Budget(max_tokens=16000)
        prompt=json.dumps({"roots":[{"id":"R1"}],"hypotheses":["long detail "*100]})
        env={"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_MAX_OUTPUT_TOKENS_INTERVENE":"6000"}
        with patch.dict(os.environ,env),patch.object(root_scout.urllib.request,"urlopen",side_effect=mock_urlopen):
            self.assertEqual(root_scout.OpenAICompatible({}).ask("INTERVENE",prompt,budget),{"patches":[]})
        self.assertEqual(len(requests),2)
        self.assertEqual([r["max_completion_tokens"] for r in requests],[6000,6000])
        self.assertLess(len(requests[1]["messages"][1]["content"].encode()),len(requests[0]["messages"][1]["content"].encode()))
        self.assertEqual(budget.calls[0]["status"],"failed")
        self.assertEqual(budget.calls[0]["raw_content"],'{"patches":[')
        self.assertTrue(budget.calls[0]["bounded_length_recovery_scheduled"])
        self.assertEqual(budget.calls[1]["retry_of_call_index"],0)
        self.assertEqual(budget.confirmed_total(),6040)

    def test_intervene_length_recovery_timeout_keeps_unknown_separate(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":"length","message":{"content":"{"}}],"usage":{"prompt_tokens":10,"completion_tokens":6000}}).encode()
        budget=root_scout.Budget(max_tokens=16000)
        env={"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_MAX_OUTPUT_TOKENS_INTERVENE":"6000"}
        with patch.dict(os.environ,env),patch.object(root_scout.urllib.request,"urlopen",side_effect=[Response(),TimeoutError("mock timeout")]) as call:
            with self.assertRaises(TimeoutError): root_scout.OpenAICompatible({}).ask("INTERVENE","{}",budget)
        self.assertEqual(call.call_count,2)
        self.assertEqual(budget.calls[0]["confirmed_usage_tokens"],6010)
        self.assertEqual(budget.calls[1]["confirmed_usage_tokens"],0)
        self.assertEqual(budget.calls[1]["reserved_unknown_tokens"],budget.calls[1]["input_reservation"]+6000)
        self.assertFalse(budget.calls[1]["usage_complete"])

    def test_intervene_default_phase_cap_is_twelve_thousand(self):
        self.assertEqual(root_scout.PHASE_OUTPUT_DEFAULT["INTERVENE"],12000)

    def test_parse_model_json_repairs_common_syntax_errors(self):
        cases=[
            ('{"residual_audit":[{"x":1},],"next_checks":[]}',"remove_trailing_commas"),
            ('```json\n{"residual_audit":[],"next_checks":[]}\n```',"strip_code_fence"),
            ('说明\n{"residual_audit":[],"next_checks":[]}\n结束',"extract_object"),
            ('{"residual_audit":[{"detail":"a\nb"}],"next_checks":[]}',"escape_control_chars"),
            ('{"residual_audit":[{"detail":"say "hi" now"}],"next_checks":[]}',"escape_inner_quotes"),
            ('{"residual_audit":[]，"next_checks":[]}',"normalize_fullwidth_punctuation"),
            ('{"residual_audit":[] "next_checks":[]}',"insert_missing_comma"),
        ]
        for content,repair in cases:
            with self.subTest(repair=repair):
                data,repairs=root_scout.parse_model_json(content)
                self.assertEqual(data["next_checks"],[])
                self.assertIn(repair,repairs)
        with self.assertRaises(json.JSONDecodeError):
            root_scout.parse_model_json('{"residual_audit":[1,2')

    def test_extract_object_with_only_leading_comment_is_recorded_as_repair(self):
        data,repairs=root_scout.parse_model_json('说明文本\n{"residual_audit":[],"next_checks":[]}')
        self.assertEqual(data["next_checks"],[])
        self.assertEqual(repairs,["extract_object"])

    def test_build_cache_is_closed_when_diagnosis_is_interrupted(self):
        cache=unittest.mock.Mock()
        req={"statement":"do x","source":"int main(){}","language":"cpp17"}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), patch.object(root_scout,"BuildCache",return_value=cache), patch.object(root_scout.OpenAICompatible,"ask",side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): root_scout._diagnose_legacy(req)
        cache.close.assert_called_once()

    def _oracle_request(self, kind="range_sum.v1", source=None):
        if kind=="range_sum.v1":
            anchor="Given an integer array, answer each 1-based closed-interval sum query [l,r]."
            semantics="sum array elements at 1-based indices l through r inclusive"
            input_format="n q; n integer values; then q pairs l r"
            domain_quote="1<=n,q<=200000; -1000000000<=a[i]<=1000000000"
            stmt=anchor+" Constraints: "+domain_quote
            source=source or "n,q=map(int,input().split())\na=list(map(int,input().split()))\nfor _ in range(q):\n l,r=map(int,input().split())\n print(sum(a[l:r]))"
            domain={"n_min":1,"n_max":200000,"q_min":1,"q_max":200000,"value_min":-1000000000,"value_max":1000000000}
        elif kind=="range_min.v1":
            anchor="Given an integer array, answer each 1-based closed-interval minimum query [l,r]."
            semantics="minimum array element at 1-based indices l through r inclusive"
            input_format="n q; n integer values; then q pairs l r"
            domain_quote="1<=n,q<=200000; -1000000000<=a[i]<=1000000000"
            stmt=anchor+" Constraints: "+domain_quote
            source=source or "n,q=map(int,input().split())\na=list(map(int,input().split()))\nfor _ in range(q):\n l,r=map(int,input().split())\n print(min(a[l-1:r]))"
            domain={"n_min":1,"n_max":200000,"q_min":1,"q_max":200000,"value_min":-1000000000,"value_max":1000000000}
        else:
            anchor="Given n integers, output their maximum."
            semantics="output the maximum of the n input integers"
            input_format="n; n integer values"
            domain_quote="1<=n<=200000; -1000000000<=a[i]<=1000000000"
            stmt=anchor+" Constraints: "+domain_quote
            source=source or "n=int(input())\na=list(map(int,input().split()))\nbest=0\nfor x in a:\n if x>best: best=x\nprint(best)"
            domain={"n_min":1,"n_max":200000,"value_min":-1000000000,"value_max":1000000000}
        import hashlib
        return {"statement":stmt,"source":source,"language":"python3","oracle_tool_contract":{"kind":kind,"version":1,"caller_attested":True,"reviewed_by":"unit-test caller review","statement_sha256":hashlib.sha256(stmt.encode()).hexdigest(),"statement_quote":anchor,"semantics":semantics,"input_format":input_format,"domain":domain,"domain_quote":domain_quote}}

    def test_oracle_requires_exact_caller_binding_and_returns_unknown_outside_domain(self):
        req=self._oracle_request()
        contract,error=root_scout.make_oracle_plan(req)[0],None
        self.assertIsNotNone(contract)
        self.assertEqual(root_scout.oracle_tools.evaluate("range_sum.v1","9 1\n0 0 0 0 0 0 0 0 0\n1 1\n",contract["domain"])["status"],"unknown")
        bad=copy.deepcopy(req); bad["oracle_tool_contract"]["statement_sha256"]="0"*64
        self.assertEqual(root_scout.make_oracle_plan(bad)[2],"statement_hash_binding_mismatch")
        bad=copy.deepcopy(req); bad["oracle_tool_contract"]["caller_attested"]=False
        self.assertEqual(root_scout.make_oracle_plan(bad)[2],"caller_review_attestation_required")
        bad=copy.deepcopy(req); bad["oracle_tool_contract"]["domain"]["q_min"]=-1
        self.assertEqual(root_scout.make_oracle_plan(bad)[2],"query_domain_bounds_malformed")
        with self.assertRaisesRegex(ValueError,"structured residual action"):
            root_scout.validate_model_result({"residual_audit":[],"assumptions":[],"next_checks":[{"action":"oracle_batch","kind":"max.v1","inputs":["1\\n0\\n"]}]},"RESIDUAL")

    def test_max_oracle_generator_includes_all_negative_and_range_extremes(self):
        contract,tests,error=root_scout.make_oracle_plan(self._oracle_request("max.v1"))
        self.assertIsNone(error)
        self.assertTrue(root_scout.oracle_tools.self_check("max.v1"))
        self.assertTrue(any(all(int(x)<0 for x in test["input"].splitlines()[1].split()) for test in tests))
        range_contract,range_tests,_=root_scout.make_oracle_plan(self._oracle_request())
        self.assertTrue(any("1000000000" in x["input"] for x in range_tests))
        self.assertEqual(contract["kind"],"max.v1")

    def test_range_min_oracle_is_independently_checked_and_bounded(self):
        self.assertTrue(root_scout.oracle_tools.self_check("range_min.v1"))
        contract,tests,error=root_scout.make_oracle_plan(self._oracle_request("range_min.v1"))
        self.assertIsNone(error)
        self.assertEqual(contract["kind"],"range_min.v1")
        self.assertTrue(any(t["input"].startswith("1 ") and t["expected"].split()==["1"]*4 for t in tests))
        self.assertTrue(any("-1000000000" in t["input"] and "1000000000" in t["input"] for t in tests))
        self.assertEqual(root_scout.oracle_tools.evaluate("range_min.v1","9 1\n0 0 0 0 0 0 0 0 0\n1 1\n",contract["domain"])["status"],"unknown")
        self.assertEqual(root_scout.oracle_tools.evaluate("range_min.v1","2 1\n5 -3\n1 2\n",contract["domain"])["expected"],"-3\n")

    def test_no_known_tests_uses_oracle_for_gated_E2_and_replay(self):
        req=self._oracle_request()
        anchor=req["oracle_tool_contract"]["statement_quote"]
        plan={"contract":[{"clause":"range sum","status":"stated","statement_quote":anchor}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        root={"claim":"closed interval starts one item too late","contract_quote":anchor,"source_line":5,"source_excerpt":"sum(a[l:r])","trigger":"nonzero first item on singleton range","causal_explanation":"The zero-based slice omits the 1-based left endpoint.","repair_direction":"Use a[l-1:r]","evidence_ids":["trusted-2"]}
        audit={"residual_audit":[],"next_checks":[]}
        def runner(_source,_language,case,_env,profile="formal",build_cache=None):
            lines=case["input"].splitlines(); n,q=map(int,lines[0].split()); values=list(map(int,lines[1].split())); out=[]
            for line in lines[2:]:
                l,r=map(int,line.split()); out.append(str(sum(values[l:r])))
            stdout="\n".join(out)+"\n"
            return {"status":"ok","stdout":stdout,"stderr":"","matched":stdout.strip()==case["expected"].strip(),"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,{"roots":[root]},audit]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["coverage"]["caller_tests"],0)
        self.assertGreater(state["coverage"]["oracle_tests"],0)
        self.assertEqual(state["roots"][0]["evidence_level"],"E2")
        self.assertTrue(state["evidence"][1]["source_kind"].startswith("builtin_oracle:"))
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_residual_action_executes_one_oracle_batch_and_is_replayable(self):
        req=self._oracle_request()
        anchor=req["oracle_tool_contract"]["statement_quote"]
        plan={"contract":[{"clause":"range sum","status":"stated","statement_quote":anchor}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        audit={"residual_audit":["Check unseen array shapes."],"assumptions":[],"next_checks":[{"action":"oracle_batch","kind":"range_sum.v1"}]}
        def runner(_source,_language,case,_env,profile="formal",build_cache=None):
            expected=root_scout.oracle_tools.evaluate("range_sum.v1",case["input"],req["oracle_tool_contract"]["domain"])["expected"]
            return {"status":"ok","stdout":expected,"stderr":"","matched":True,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,audit]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["coverage"]["residual_batch_status"],"completed")
        self.assertEqual(state["residual_actions"][0]["status"],"completed")
        self.assertEqual(len(state["residual_oracle_test_plan"]),2)
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        replayed_inputs=[]
        def replay_runner(_source,_language,case,_env,profile="formal",build_cache=None):
            replayed_inputs.append(case["input"])
            expected=root_scout.oracle_tools.evaluate("range_sum.v1",case["input"],req["oracle_tool_contract"]["domain"])["expected"]
            return {"status":"ok","stdout":expected,"stderr":"","matched":True,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=replay_runner),patch.object(root_scout,"docker_run",side_effect=replay_runner):
            replayed=root_scout._diagnose_legacy(req,provider="replay",replay_state=state)
        self.assertTrue(all(case["input"] in replayed_inputs for case in state["residual_oracle_test_plan"]))
        self.assertEqual(root_scout.verify_saved_case_state(replayed),req)

    def test_residual_batch_with_no_new_legal_cases_does_not_duplicate_evidence(self):
        req=self._oracle_request("max.v1")
        contract=req["oracle_tool_contract"]; anchor=contract["statement_quote"]
        quote="1<=n<=1; 0<=a[i]<=0"
        req["statement"]=anchor+" Constraints: "+quote
        req["oracle_tool_contract"].update(statement_sha256=root_scout.sha(req["statement"]),domain_quote=quote,
            domain={"n_min":1,"n_max":1,"value_min":0,"value_max":0})
        plan={"contract":[{"clause":"maximum","status":"stated","statement_quote":anchor}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        audit={"residual_audit":[],"assumptions":[],"next_checks":[{"action":"oracle_batch","kind":"max.v1"}]}
        def runner(_source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":"0\n","stderr":"","matched":True,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,audit]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["coverage"]["residual_batch_status"],"no_new_cases")
        self.assertEqual(len(state["evidence"]),1)
        self.assertEqual(state["coverage"]["residual_status"],"complete")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_residual_plan_deduplicates_against_caller_trusted_cases(self):
        req=self._oracle_request()
        anchor=req["oracle_tool_contract"]["statement_quote"]
        contract,planned,error=root_scout.make_oracle_plan(req,"residual")
        self.assertIsNone(error)
        duplicate=planned[0]
        req["known_failing_tests"]=[{"input":duplicate["input"],"expected":duplicate["expected"],"trusted":True,
            "expected_source":"independently checked expected","legal_input":True,"legal_input_source":"caller validated against题意"}]
        plan={"contract":[{"clause":"range sum","status":"stated","statement_quote":anchor}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        audit={"residual_audit":[],"assumptions":[],"next_checks":[{"action":"oracle_batch","kind":"range_sum.v1"}]}
        def runner(_source,_language,case,_env,profile="formal",build_cache=None):
            result=root_scout.oracle_tools.evaluate("range_sum.v1",case["input"],contract["domain"])
            return {"status":"ok","stdout":result["expected"],"stderr":"","matched":True,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,audit]):
            state=root_scout._diagnose_legacy(req)
        pairs=[(e["input"],e["expected"]) for e in state["evidence"] if e.get("trusted_expected")]
        self.assertEqual(len(pairs),len(set(pairs)))
        self.assertEqual(sum(e.get("source_kind")=="caller_trusted" for e in state["evidence"]),1)
        self.assertEqual(state["coverage"]["residual_batch_status"],"completed")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_repaired_model_json_is_recorded_in_call_ledger(self):
        content='{"residual_audit":[],"next_checks":[],}'
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":content}}],"usage":{"prompt_tokens":10,"completion_tokens":20}}).encode()
        budget=root_scout.Budget()
        with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}), patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            answer=root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",budget)
        self.assertEqual(answer["next_checks"],[])
        self.assertEqual(budget.calls[0]["json_repairs"],["remove_trailing_commas"])
        self.assertEqual(budget.calls[0]["raw_sha256"],root_scout.sha(content))

    def test_repaired_residual_json_completes_diagnosis(self):
        statement="输出输入整数加一。"
        req={"statement":statement,"source":"print(int(input()) - 1)","language":"python3",
             "known_failing_tests":[{"input":"41\n","expected":"42\n","trusted":True,
                                     "expected_source":"手算41+1","legal_input":True,
                                     "legal_input_source":"整数41符合题面输入域"}]}
        plan={"contract":[{"clause":"输出输入整数加一","status":"stated","statement_quote":statement}],
              "assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        root={"claim":"加一被写成减一","contract_quote":statement,"source_line":1,
              "source_excerpt":"int(input()) - 1","trigger":"输入41","causal_explanation":"41减一得到40。",
              "repair_direction":"改成加一","evidence_ids":["trusted-1"]}
        edit_proposal={"root_id":"R1","source_line":1,"old_text":"- 1","new_text":"+ 1"}
        payloads=[json.dumps(plan),json.dumps({"roots":[root]}),json.dumps({"patches":[edit_proposal]}),'{"residual_audit":[],"next_checks":[],"assumptions":[],}']
        class Response:
            def __init__(self,payload): self.payload=payload
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit): return self.payload
        responses=iter(payloads)
        def fake_urlopen(*_args,**_kwargs):
            content=next(responses)
            return Response(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":content}}],
                                        "usage":{"prompt_tokens":10,"completion_tokens":10}}).encode())
        def failing_run(source,*_args,**_kwargs):
            stdout="42\n" if " + 1" in source else "40\n"
            return {"status":"ok","stdout":stdout,"stderr":"","matched":stdout=="42\n","wall_seconds":0}
        with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}), \
             patch.object(root_scout.urllib.request,"urlopen",side_effect=fake_urlopen), \
             patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"test"}), \
             patch.object(root_scout,"run_case",side_effect=failing_run), \
             patch.object(root_scout,"docker_run",side_effect=failing_run):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(len(state["roots"]),1)
        self.assertEqual(state["stop_reason"],"completed_with_evidence")
        self.assertEqual(state["budget"]["calls"][3]["json_repairs"],["remove_trailing_commas"])
        self.assertIn("响应修复（RESIDUAL）",root_scout.markdown(state))

    def test_prompt_prefix_is_shared_across_phases(self):
        statement="输出输入整数加一。"
        req={"statement":statement,"source":"print(int(input()) - 1)","language":"python3",
             "known_failing_tests":[{"input":"41\n","expected":"42\n","trusted":True,
                                     "expected_source":"手算41+1","legal_input":True,
                                     "legal_input_source":"整数41符合题面输入域"}]}
        root={"claim":"加一被写成减一","contract_quote":statement,"source_line":1,
              "source_excerpt":"int(input()) - 1","trigger":"输入41","causal_explanation":"41减一得到40。",
              "repair_direction":"改成加一","evidence_ids":["trusted-1"]}
        prompts=[]
        def fake_ask(phase,prompt,budget,max_output=2500):
            prompts.append(prompt)
            if phase=="CONTRACT+PLAN":
                return {"contract":[{"clause":"输出输入整数加一","status":"stated","statement_quote":statement}],
                        "assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
            if phase=="ADJUDICATE": return {"roots":[root]}
            if phase=="INTERVENE": return {"patches":[{"root_id":"R1","source_line":1,"old_text":"- 1","new_text":"+ 1"}]}
            return {"residual_audit":[],"next_checks":[],"assumptions":[]}
        def failing_run(*_args,**_kwargs):
            return {"status":"ok","stdout":"40\n","stderr":"","matched":False,"wall_seconds":0}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"test"}), \
             patch.object(root_scout,"run_case",side_effect=failing_run), \
             patch.object(root_scout,"docker_run",side_effect=failing_run), \
             patch.object(root_scout.OpenAICompatible,"ask",side_effect=fake_ask):
            root_scout._diagnose_legacy(req)
        self.assertEqual(len(prompts),4)
        bodies=[json.loads(p) for p in prompts]
        prefix=["statement","source","numbered_source","language","environment"]
        for body in bodies: self.assertEqual(list(body.keys())[:5],prefix)
        for body in (bodies[1],bodies[3]):
            self.assertEqual(list(body.keys())[:8],prefix+["contract","hypotheses","evidence"])
        common=os.path.commonprefix([prompts[1],prompts[2],prompts[3]])
        self.assertGreater(len(common),1000)

    def test_cache_usage_is_recorded_and_reported(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"residual_audit":[],"next_checks":[],"assumptions":[]})}}],
                                   "usage":{"prompt_tokens":1000,"completion_tokens":10,
                                            "prompt_cache_hit_tokens":800,"prompt_cache_miss_tokens":200}}).encode()
        budget=root_scout.Budget()
        with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}), patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",budget)
        call=budget.calls[0]
        self.assertEqual((call["cache_hit_tokens"],call["cache_miss_tokens"]),(800,200))
        self.assertEqual(call["cache_hit_rate"],0.8)
        state={"stop_reason":"completed_with_evidence","id":"x","roots":[],"budget":{"calls":budget.calls},
               "coverage":{"untested":[],"residual_status":"complete"},"contract":[],"assumptions":[]}
        report=root_scout.markdown(state)
        self.assertIn("缓存命中：800 / 1000（80.0%）",report)
        self.assertIn("缓存（RESIDUAL）：800 / 1000（80.0%）",report)

    def test_run_tasks_preserves_order_and_parallelizes(self):
        import time as _time
        def fn(task):
            _time.sleep(task["delay"]); return task["id"]
        tasks=[{"id":f"t{i}","delay":0.15} for i in range(4)]
        started=_time.monotonic(); out=root_scout.run_tasks(tasks,fn,4); elapsed=_time.monotonic()-started
        self.assertEqual(out,["t0","t1","t2","t3"])
        self.assertLess(elapsed,0.5)

    def test_execution_workers_are_clamped(self):
        for raw,expected in (("1",1),("0",1),("99",16),("bad",8),("4",4)):
            with self.subTest(raw=raw), patch.dict(os.environ,{"ROOT_SCOUT_EXEC_WORKERS":raw}):
                self.assertEqual(root_scout.execution_workers(),expected)

    def test_unverified_model_probes_keep_structured_unresolved_status(self):
        req={"statement":"输入 n，合法范围为0或1","source":"print(0)","language":"python3","known_failing_tests":[{"input":"0\n","expected":"0\n","trusted":True,"expected_source":"n=0","legal_input":True,"legal_input_source":"n=0符合题面"}]}
        plan={"contract":[{"clause":"range","status":"stated","statement_quote":"合法范围为0或1"}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[{"input":"1\n","purpose":"boundary"}]}
        audit={"residual_audit":[],"next_checks":[],"assumptions":[]}
        calls=[]
        def fake_run(_source,_language,case,_env,profile="formal",build_cache=None,build_only=False):
            calls.append((case["input"],profile))
            if case["input"]=="1\n" and profile=="formal": return {"status":"program_failed","stdout":"","stderr":"UB","returncode":1,"wall_seconds":0.01}
            return {"status":"ok","stdout":"0\n","stderr":"","returncode":0,"matched":True,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), patch.object(root_scout,"run_case",side_effect=fake_run), patch.object(root_scout,"docker_run",side_effect=fake_run), patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,audit]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["roots"],[])
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertTrue(state["coverage"]["execution_failures"])
        self.assertNotIn(("1\n","diagnostic"),calls)
        probe=next(e for e in state["evidence"] if e["id"]=="targeted-1")
        self.assertEqual(probe["result"]["diagnostic_build"]["status"],"skipped_untrusted_probe")
        self.assertIn("仅正式配置",root_scout.markdown(state))

    def test_passed_trusted_tests_with_new_residual_direction_remain_unresolved(self):
        statement="输出输入整数加一。"
        req={"statement":statement,"source":"print(int(input()) + 1)","language":"python3",
             "known_failing_tests":[{"input":"4\\n","expected":"5\\n","trusted":True,
                                     "expected_source":"手算4+1","legal_input":True,
                                     "legal_input_source":"整数4符合题面输入域"}]}
        plan={"contract":[{"clause":"输出输入整数加一","status":"stated","statement_quote":statement}],
              "assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        audit={"residual_audit":["输入边界仍需确认"],"assumptions":[],"next_checks":["验证整数输入范围边界"]}
        def successful_run(*_args,**_kwargs):
            return {"status":"ok","stdout":"5\\n","stderr":"","returncode":0,
                    "matched":True,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), patch.object(root_scout,"run_case",side_effect=successful_run), patch.object(root_scout,"docker_run",side_effect=successful_run), patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,audit]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["roots"],[])
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertIn("验证整数输入范围边界",state["coverage"]["untested"])

    def test_failed_residual_audit_does_not_report_plan_phase_draft(self):
        statement="输出输入整数加一。"
        req={"statement":statement,"source":"print(int(input()) - 1)","language":"python3",
             "known_failing_tests":[{"input":"41\n","expected":"42\n","trusted":True,
                                     "expected_source":"手算41+1","legal_input":True,
                                     "legal_input_source":"整数41符合题面输入域"}]}
        plan={"contract":[{"clause":"输出输入整数加一","status":"stated","statement_quote":statement}],
              "assumptions":[],"hypotheses":[],"residual_audit":["PLAN_PHASE_DRAFT"],"targeted_tests":[]}
        root={"claim":"加一被写成减一","contract_quote":statement,"source_line":1,
              "source_excerpt":"int(input()) - 1","trigger":"输入41","causal_explanation":"41减一得到40。",
              "repair_direction":"改成加一","evidence_ids":["trusted-1"]}
        def failing_run(*_args,**_kwargs):
            return {"status":"ok","stdout":"40\n","stderr":"","returncode":0,
                    "matched":False,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=failing_run), \
             patch.object(root_scout,"docker_run",side_effect=failing_run), \
             patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,{"roots":[root]},json.JSONDecodeError("bad json","",0)]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["residual_audit"],[])
        self.assertEqual(state["initial_residual_audit"],["PLAN_PHASE_DRAFT"])
        self.assertEqual(len(state["roots"]),1)
        self.assertEqual(state["roots"][0]["evidence_level"],"E2")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        report=root_scout.markdown(state)
        self.assertNotIn("PLAN_PHASE_DRAFT",report)
        self.assertIn("残余审计未完成",report)

    def test_real_budget_reservation_failure_is_budget_exhausted(self):
        requests=[]; statement="输出整数加一。"
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_args): pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                answer={"contract":[{"clause":"output","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
                payload=json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(answer)}}],"usage":{"prompt_tokens":47000,"completion_tokens":0}}).encode()
                self.send_response(200); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        req={"statement":statement,"source":"print(0)","language":"python3","known_failing_tests":[{"input":"1\n","expected":"2\n","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"valid"}]}
        try:
            with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":f"http://127.0.0.1:{server.server_port}/v1","ROOT_SCOUT_MAX_TOKENS":"48000"}),patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",return_value={"status":"ok","stdout":"0\n","stderr":"","matched":False,"wall_seconds":0}),patch.object(root_scout,"docker_run",return_value={"status":"ok","stdout":"0\n","stderr":"","matched":False,"wall_seconds":0}):
                state=root_scout._diagnose_legacy(req)
        finally: server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(len(requests),1)
        self.assertEqual(state["budget"]["tokens_total"],47000)
        self.assertEqual(state["stop_reason"],"budget_exhausted")
        self.assertEqual([x["status"] for x in state["budget"]["calls"]], ["ok","rejected","rejected"])

    def test_model_evidence_keeps_counterexample_and_diagnostics_without_mutating_ledger(self):
        evidence=[{"id":"trusted-1","input":"1\\n","expected":"7\\n","trusted_expected":True,
                   "legal_input_verified":True,"source_sha256":"source-hash","expected_source":"hand checked",
                   "result":{"status":"ok","stdout":"0\\n","stderr":"","evidence_level":"E1",
                             "stages":[{"container_state":{"irrelevant_metadata":"x"*10000}}],
                             "diagnostic_build":{"status":"program_failed","stderr":"signed integer overflow",
                                                 "stdout":"","stages":[{"container_state":{"metadata":"x"*10000}}]}}}]
        before=json.dumps(evidence,sort_keys=True)
        summary=root_scout.model_evidence(evidence)
        self.assertEqual(json.dumps(evidence,sort_keys=True),before)
        self.assertEqual(summary[0]["input"],evidence[0]["input"])
        self.assertEqual(summary[0]["expected"],evidence[0]["expected"])
        self.assertEqual(summary[0]["result"]["evidence_level"],"E1")
        self.assertEqual(summary[0]["result"]["diagnostic_build"]["stderr"],"signed integer overflow")
        self.assertTrue(summary[0]["trusted_expected"])
        self.assertTrue(summary[0]["legal_input_verified"])
        self.assertLess(len(json.dumps(summary)),len(before)//10)

    def test_configured_reasoning_and_output_limit_are_sent_and_reserved(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"residual_audit":[],"next_checks":[]})}}],
                                   "usage":{"prompt_tokens":100,"completion_tokens":200}}).encode()
        budget=root_scout.Budget()
        settings={"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1",
                  "OPENAI_REASONING_EFFORT":"low","ROOT_SCOUT_MAX_OUTPUT_TOKENS":"6000"}
        with patch.dict(os.environ,settings), patch.object(root_scout.urllib.request,"urlopen",return_value=Response()) as http:
            root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",budget,1200)
        body=json.loads(http.call_args.args[0].data)
        self.assertEqual(body["max_completion_tokens"],1200)
        self.assertEqual(body["reasoning_effort"],"low")
        self.assertEqual(budget.calls[0]["max_output_reserved"],1200)
        self.assertEqual(budget.calls[0]["input_reservation"],len(http.call_args.args[0].data))
        self.assertEqual(budget.total(),300)

    def test_phase_output_cap_is_used_without_legacy_callsite_limit(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit):
                return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"roots":[]})}}],"usage":{"prompt_tokens":10,"completion_tokens":2}}).encode()
        budget=root_scout.Budget()
        settings={"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1",
                  "ROOT_SCOUT_MAX_TOKENS":"48000","ROOT_SCOUT_MAX_OUTPUT_TOKENS":"10000",
                  "ROOT_SCOUT_MAX_OUTPUT_TOKENS_ADJUDICATE":"8000"}
        with patch.dict(os.environ,settings,clear=True),patch.object(root_scout.urllib.request,"urlopen",return_value=Response()) as http:
            root_scout.OpenAICompatible({}).ask("ADJUDICATE","short prompt",budget)
        body=json.loads(http.call_args.args[0].data)
        self.assertEqual(body["max_completion_tokens"],8000)
        self.assertEqual(budget.calls[0]["max_output_reserved"],8000)

    def test_invalid_model_settings_are_rejected_before_http(self):
        base={"OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_REASONING_EFFORT":"low",
              "ROOT_SCOUT_MAX_OUTPUT_TOKENS":"6000","ROOT_SCOUT_MAX_TOKENS":"48000"}
        for invalid in ({"ROOT_SCOUT_MAX_OUTPUT_TOKENS":"0"},{"ROOT_SCOUT_MAX_OUTPUT_TOKENS":"48001"},
                        {"ROOT_SCOUT_MAX_OUTPUT_TOKENS":"invalid"},{"OPENAI_REASONING_EFFORT":"invalid"}):
            with self.subTest(settings=invalid), patch.dict(os.environ,dict(base,**invalid)), \
                 patch.object(root_scout.urllib.request,"urlopen") as http, self.assertRaises(ValueError):
                root_scout.OpenAICompatible({}).ask("RESIDUAL","{}",root_scout.Budget())
            http.assert_not_called()

    def test_model_input_lines_become_physical_stdin_without_unescaping_literals(self):
        plan={"contract":[],"targeted_tests":[{"input_lines":["1 1","7","1 1"],"purpose":"singleton"},
                                               {"input_lines":[r"literal\ntext"],"purpose":"literal characters"}]}
        parsed=root_scout.validate_model_result(plan,"CONTRACT+PLAN")
        self.assertEqual(parsed["targeted_tests"][0]["input"],"1 1\n7\n1 1\n")
        self.assertEqual(parsed["targeted_tests"][1]["input"],"literal\\ntext\n")
        for test in ({"input_lines":["one\ntwo"]},{"input_lines":["one\rtwo"]},
                     {"input_lines":["one"],"input":"different"},{"input_lines":[42]}):
            with self.subTest(test=test),self.assertRaises(ValueError):
                root_scout.validate_model_result({"contract":[],"targeted_tests":[test]},"CONTRACT+PLAN")

    def _root_gate_fixture(self):
        req=root_scout.load_json("examples/prefix_overflow.json")
        req["statement"]="给定 n 个非负整数，回答闭区间 [l,r] 的元素和。1<=n,q<=200000，0<=a[i]<=1000000000。"
        contract=[{"clause":"闭区间和","status":"stated","statement_quote":"给定 n 个非负整数，回答闭区间 [l,r] 的元素和"},
                  {"clause":"数值范围","status":"stated","statement_quote":"0<=a[i]<=1000000000"}]
        evidence={f"trusted-{i}":{"source_kind":"caller_trusted","trusted_expected":True,"legal_input_verified":True,
                  "source_sha256":root_scout.sha(req["source"]),"result":{"status":"ok","matched":False,"evidence_level":"E1"}} for i in (1,2)}
        root={"claim":"闭区间遗漏左端点","contract_quote":"回答闭区间 [l,r] 的元素和","source_line":4,
              "source_excerpt":"p[r]-p[l]","trigger":"单点查询","causal_explanation":"减去p[l]遗漏a[l]",
              "repair_direction":"改为p[r]-p[l-1]","evidence_ids":["trusted-1"]}
        return req,contract,evidence,root

    def test_local_patch_validation_supports_same_line_nonoverlap_and_bounded_variants(self):
        source="int main(){int p=0; long long q=0;}\r\n"
        roots=[{"id":"R1","status":"confirmed","evidence_level":"E2","original_location":{"line":1},"source_excerpt":"p=0"},
               {"id":"R2","status":"confirmed","evidence_level":"E2","original_location":{"line":1},"source_excerpt":"q=0"}]
        proposals=[{"root_id":"R1","source_line":1,"old_text":"p=0","new_text":"p=1"},
                   {"root_id":"R2","source_line":1,"old_text":"q=0","new_text":"q=1"}]
        patches=root_scout.local_patch_plan(source,roots,proposals)
        self.assertEqual(len(root_scout.intervention_variant_sets(patches)),3)
        combined=root_scout.apply_local_patches(source,patches,["R1","R2"])
        self.assertIn("p=1; long long q=1",combined)
        self.assertTrue(combined.endswith("\r\n"))
        bad=[dict(proposals[0],old_text="int p=0; long long q=0;",new_text="rewritten")]
        with self.assertRaisesRegex(ValueError,"overlapping"):
            root_scout.local_patch_plan(source,roots,bad+[{"root_id":"R2","source_line":1,"old_text":"q=0","new_text":"q=1"}])
        with self.assertRaisesRegex(ValueError,"replacement"):
            root_scout.local_patch_plan(source,roots,[{"root_id":"R1","source_line":1,"old_text":"p=0","new_text":"p=0"}])
        self.assertEqual([len(root_scout.intervention_variant_sets([{"root_id":str(i)} for i in range(n)])) for n in (1,2,3)],[1,3,4])

    def test_intervention_matrix_supports_patch_and_replays_variants(self):
        statement="Output 9 exactly."
        req={"statement":statement,"source":"print(0)","language":"python3","known_failing_tests":[{"input":"","expected":"9","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"hand checked"}]}
        plan={"contract":[{"clause":"output","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        root={"claim":"constant is wrong","contract_quote":statement,"source_line":1,"source_excerpt":"print(0)","trigger":"always","causal_explanation":"Prints zero rather than nine.","repair_direction":"print 9","evidence_ids":["trusted-1"]}
        edit_proposal={"root_id":"R1","source_line":1,"old_text":"print(0)","new_text":"print(9)"}
        audit={"residual_audit":[],"next_checks":[],"assumptions":[]}
        calls=[]
        def runner(source,_language,case,_env,profile="formal",build_cache=None):
            calls.append((source,case["input"]))
            stdout="9" if "print(9)" in source else "0"
            return {"status":"ok","stdout":stdout,"stderr":"","matched":stdout==case["expected"],"wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,{"roots":[root]},{"patches":[edit_proposal]},audit]):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["coverage"]["intervention_status"],"complete")
        self.assertEqual(state["roots"][0]["intervention"]["status"],"supported")
        self.assertIn("trusted-1",state["coverage"]["known_explained_evidence_ids"])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        calls.clear()
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner):
            replay=root_scout._diagnose_legacy(req,provider="replay",replay_state=state)
        self.assertTrue(any("print(9)" in source for source,_ in calls))
        self.assertEqual(replay["roots"][0]["intervention"]["status"],"supported")
        self.assertEqual(root_scout.verify_saved_case_state(replay),req)
        forged=copy.deepcopy(state); forged["intervention_variants"][0]["matrix"][0]["variant"]["matched"]=False
        forged["integrity"]["intervention_matrix_sha256"]=root_scout.canonical_sha(forged["intervention_variants"])
        with self.assertRaisesRegex(ValueError,"match flag does not recompute"):
            root_scout.verify_saved_case_state(forged)
        def fixed_runner(_source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":"9","stderr":"","matched":True,"wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=fixed_runner),patch.object(root_scout,"docker_run",side_effect=fixed_runner):
            not_reproduced=root_scout._diagnose_legacy(req,provider="replay",replay_state=state)
        self.assertEqual(not_reproduced["roots"],[])
        self.assertEqual(not_reproduced["replay_prior_roots"][0]["status"],"not_reproduced")
        self.assertEqual(not_reproduced["intervention_variants"],[])

    def test_failed_or_regressing_variant_never_counts_as_intervention_support(self):
        statement="Output 9 exactly."
        req={"statement":statement,"source":"print(0)","language":"python3","known_failing_tests":[
            {"input":"a","expected":"9","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"hand checked"},
            {"input":"b","expected":"0","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"hand checked"}]}
        root={"id":"R1","status":"confirmed","evidence_level":"E2","source_line":1,"source_excerpt":"print(0)","original_location":{"line":1,"excerpt":"print(0)"},"claim":"wrong","contract_quote":statement,"trigger":"a","causal_explanation":"wrong output","repair_direction":"print 9","evidence_ids":["trusted-1"]}
        evidence=[{"id":"trusted-1","trusted_expected":True,"input":"a","expected":"9","input_sha256":root_scout.sha("a"),"expected_sha256":root_scout.sha("9"),"source_sha256":root_scout.sha(req["source"]),"result":{"status":"ok","stdout":"0","matched":False,"evidence_level":"E1"}},
                  {"id":"trusted-2","trusted_expected":True,"input":"b","expected":"0","input_sha256":root_scout.sha("b"),"expected_sha256":root_scout.sha("0"),"source_sha256":root_scout.sha(req["source"]),"result":{"status":"ok","stdout":"0","matched":True}}]
        patches=root_scout.local_patch_plan(req["source"],[root],[{"root_id":"R1","source_line":1,"old_text":"print(0)","new_text":"print(9)"}])
        variants=[]
        for spec in root_scout.intervention_variant_sets(patches):
            variants.append({**spec,"source_sha256":root_scout.sha(root_scout.apply_local_patches(req["source"],patches,spec["root_ids"])),"status":"regressed","matrix":[
                {"evidence_id":"trusted-1","input_sha256":root_scout.sha("a"),"expected_sha256":root_scout.sha("9"),"original":{"status":"ok","matched":False},"variant":{"status":"ok","stdout":"9","stdout_sha256":root_scout.sha("9"),"matched":True},"classification":"supported"},
                {"evidence_id":"trusted-2","input_sha256":root_scout.sha("b"),"expected_sha256":root_scout.sha("0"),"original":{"status":"ok","matched":True},"variant":{"status":"ok","stdout":"9","stdout_sha256":root_scout.sha("9"),"matched":False},"classification":"variant_risk"}]})
        state={"request_snapshot":req,"originals":{"statement_sha256":root_scout.sha(statement),"source_sha256":root_scout.sha(req["source"]),"environment_sha256":root_scout.sha("{}")},"integrity":{"request_sha256":root_scout.canonical_sha(req),"test_plan_sha256":"x","run_configuration_sha256":"x","evidence_sha256":"x","intervention_plan_sha256":"x","intervention_matrix_sha256":"x"}}
        self.assertTrue(all(v["status"]=="regressed" for v in variants))
        self.assertFalse(all(row["classification"]!="variant_risk" for v in variants for row in v["matrix"]))
        self.assertNotEqual(root_scout.intervention_root_result(root,patches,variants,evidence)["status"],"supported")

    def test_compile_failed_variant_is_blocked_and_not_replay_support(self):
        statement="Output 9 exactly."
        req={"statement":statement,"source":"print(0)","language":"python3","known_failing_tests":[{"input":"","expected":"9","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"hand checked"}]}
        plan={"contract":[{"clause":"output","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        root={"claim":"constant is wrong","contract_quote":statement,"source_line":1,"source_excerpt":"print(0)","trigger":"always","causal_explanation":"Prints zero.","repair_direction":"print nine","evidence_ids":["trusted-1"]}
        proposal={"root_id":"R1","source_line":1,"old_text":"print(0)","new_text":"print(9)"}
        def runner(source,_language,_case,_env,profile="formal",build_cache=None):
            if "print(9)" in source: return {"status":"compile_failed","stdout":"","stderr":"compiler error","wall_seconds":.01}
            return {"status":"ok","stdout":"0","stderr":"","matched":False,"wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=[plan,{"roots":[root]},{"patches":[proposal]},{"residual_audit":[],"next_checks":[],"assumptions":[]}]) as ask:
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(state["intervention_variants"][0]["status"],"blocked")
        self.assertEqual(state["roots"][0]["intervention"]["status"],"blocked")
        self.assertEqual(state["coverage"]["known_explained_evidence_ids"],[])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_healthy_intervention_adjudicates_unexplained_masked_original_e1_once(self):
        statement="Given n integers, output their maximum."
        req={"statement":statement,"source":"n=int(input())\na=list(map(int,input().split()))\nbest=0\nfor x in a[:-1]:\n if x>best: best=x\nprint(best)","language":"python3","known_failing_tests":[
            {"input":"2\n-2 -4\n","expected":"-2\n","trusted":True,"expected_source":"hand checked max","legal_input":True,"legal_input_source":"valid integer array"},
            {"input":"3\n-5 -6 10\n","expected":"10\n","trusted":True,"expected_source":"hand checked max","legal_input":True,"legal_input_source":"valid integer array"}]}
        plan={"contract":[{"clause":"output maximum","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        root1={"claim":"best initialized to zero rejects all-negative arrays","contract_quote":statement,"source_line":3,"source_excerpt":"best=0","trigger":"all values are negative","causal_explanation":"Zero is larger than every legal negative value.","repair_direction":"initialize from first value","evidence_ids":["trusted-1"]}
        root2={"claim":"loop skips the final element","contract_quote":statement,"source_line":4,"source_excerpt":"a[:-1]","trigger":"unique maximum is last","causal_explanation":"The slice excludes the final element.","repair_direction":"iterate over all values","evidence_ids":["trusted-2"]}
        outputs={"2\n-2 -4\n":"-2\n","3\n-5 -6 10\n":"-5\n"}
        phases=[]
        def ask(phase,_prompt,_budget,max_output=2500):
            phases.append(phase)
            if phase=="CONTRACT+PLAN": return plan
            if phase=="ADJUDICATE": return {"roots":[root1]} if phases.count("ADJUDICATE")==1 else {"roots":[root2]}
            if phase=="INTERVENE": return {"patches":[{"root_id":"R1","source_line":3,"old_text":"best=0","new_text":"best=a[0]"}]}
            if phase=="RESIDUAL": return {"residual_audit":[],"next_checks":[],"assumptions":[]}
            raise AssertionError(phase)
        def runner(source,_language,case,_env,profile="formal",build_cache=None):
            inp=case["input"]
            if "best=a[0]" in source: stdout=outputs[inp]
            else: stdout="0\n"
            return {"status":"ok","stdout":stdout,"stderr":"","matched":stdout==case["expected"],"wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=ask):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(phases.count("ADJUDICATE"),2)
        self.assertEqual([r["id"] for r in state["roots"]],["R1","R2"])
        self.assertEqual(state["coverage"]["known_explained_evidence_ids"],["trusted-1"])
        self.assertEqual(state["roots"][1]["intervention"]["status"],"blocked")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_multiple_confirmed_roots_trigger_one_completeness_audit_before_patching(self):
        statement="Print one for input x and two for input y."
        source="a=0\nb=0\nc=0\nprint(0)"
        req={"statement":statement,"source":source,"language":"python3","known_failing_tests":[
            {"input":"x\n","expected":"1\n","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"valid input x"},
            {"input":"y\n","expected":"2\n","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"valid input y"}]}
        plan={"contract":[{"clause":"output","status":"stated","statement_quote":statement}],"assumptions":[],"hypotheses":[],"residual_audit":[],"targeted_tests":[]}
        def candidate(claim,line,excerpt):
            return {"claim":claim,"contract_quote":statement,"source_line":line,"source_excerpt":excerpt,"trigger":"x","causal_explanation":claim,"repair_direction":"change this expression","evidence_ids":["trusted-1"]}
        roots=[candidate("first cause",1,"a=0"),candidate("second cause",2,"b=0"),candidate("third cause",3,"c=0")]
        roots[0]["evidence_ids"]=["trusted-1"]; roots[1]["evidence_ids"]=["trusted-2"]; roots[2]["evidence_ids"]=["trusted-2"]
        phases=[]; prompts=[]
        def ask(phase,prompt,_budget,max_output=None):
            phases.append(phase); prompts.append(prompt)
            if phase=="CONTRACT+PLAN": return plan
            if phase=="ADJUDICATE": return {"roots":roots[:2]} if phases.count("ADJUDICATE")==1 else {"roots":[roots[2]]}
            if phase=="INTERVENE": return {"patches":[]}
            return {"residual_audit":[],"next_checks":[],"assumptions":[]}
        def runner(_source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":"0\n","stderr":"","matched":False,"wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout,"docker_run",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",side_effect=ask):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(phases.count("ADJUDICATE"),2)
        self.assertEqual([r["id"] for r in state["roots"]],["R1","R2","R3"])
        audit=state["coverage"]["adjudication_completeness_audit"]
        self.assertEqual(audit["status"],"completed_with_candidates")
        self.assertEqual(audit["shared_evidence"],{})
        self.assertEqual(audit["root_ids"],["R1","R2"])
        self.assertTrue(any("initialization, comparison/update, indexing/bounds, counts/termination, and output" in prompt for prompt in prompts))
        audit_payload=json.loads(prompts[2])
        self.assertEqual({e["id"] for e in audit_payload["evidence"]},{"trusted-1","trusted-2"})
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_root_gate_accepts_verbatim_subclause_and_exact_source_expression(self):
        req,contract,evidence,root=self._root_gate_fixture()
        self.assertNotIn(root["contract_quote"],[c["statement_quote"] for c in contract])
        self.assertEqual(root_scout.root_rejection_reasons(root,req,contract,evidence,set()),[])
        root_scout.validate_model_result({"roots":[root]},"ADJUDICATE")

    def test_root_gate_still_rejects_fabricated_quotes_wrong_lines_and_untrusted_evidence(self):
        req,contract,evidence,root=self._root_gate_fixture()
        cases=[(dict(root,contract_quote="闭区间不含左端点"),evidence,"contract_quote_not_verbatim"),
               (dict(root,contract_quote="1<=n,q<=200000"),evidence,"contract_quote_not_in_stated_clause"),
               (dict(root,source_line=1),evidence,"source_excerpt_mismatch"),
               (dict(root,source_excerpt="p[r]-p[l-1]"),evidence,"source_excerpt_mismatch"),
               (dict(root,evidence_ids=["missing"]),evidence,"unknown_evidence")]
        untrusted=copy.deepcopy(evidence); untrusted["trusted-1"]["trusted_expected"]=False
        cases.append((root,untrusted,"untrusted_input_or_expected"))
        wrong_source=copy.deepcopy(evidence); wrong_source["trusted-1"]["source_sha256"]="different-source"
        cases.append((root,wrong_source,"source_hash_mismatch"))
        for candidate,ledger,reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(reason,root_scout.root_rejection_reasons(candidate,req,contract,ledger,set()))
        missing_excerpt=dict(root); del missing_excerpt["source_excerpt"]
        with self.assertRaises(ValueError): root_scout.validate_model_result({"roots":[missing_excerpt]},"ADJUDICATE")

    def test_two_roots_promote_and_rejected_candidate_keeps_specific_reason(self):
        req,contract,_evidence,endpoint=self._root_gate_fixture()
        overflow=dict(endpoint,claim="前缀和溢出",contract_quote="0<=a[i]<=1000000000",
                      source_excerpt="vector<int> p(n+1)",evidence_ids=["trusted-2"])
        wrong_location=dict(endpoint,claim="错误行号候选",source_line=1)
        proposed=[endpoint,overflow,wrong_location]
        responses=[{"contract":contract,"hypotheses":[],"targeted_tests":[]},
                   {"roots":copy.deepcopy(proposed)},{"residual_audit":[],"next_checks":[]}]
        def run(*_args,**_kwargs):
            return {"status":"ok","matched":False,"stdout":"0\n","stderr":"","wall_seconds":0}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"test"}), \
             patch.object(root_scout,"docker_run",side_effect=run), \
             patch.object(root_scout.OpenAICompatible,"ask",side_effect=responses):
            state=root_scout._diagnose_legacy(req)
        self.assertEqual(len(state["roots"]),2)
        self.assertTrue(all(r["original_location"]["line"]==4 for r in state["roots"]))
        self.assertEqual([d["status"] for d in state["root_decisions"]],["accepted","accepted","rejected"])
        self.assertEqual(state["root_decisions"][2]["candidate"],wrong_location)
        self.assertIn("source_excerpt_mismatch",state["root_decisions"][2]["reasons"])
        self.assertIn("source_excerpt_mismatch",root_scout.markdown(state))

    def test_contract_quote_can_link_complete_stated_clause_union_but_not_assumed_tail(self):
        req={"statement":"1<=n<=2; -5<=a[i]<=5; if optional, use a slower method.","source":"print(0)","language":"python3"}
        clauses=[{"clause":"size bound","status":"stated","statement_quote":"1<=n<=2","clause_id":"C1"},
                 {"clause":"value bound","status":"stated","statement_quote":"-5<=a[i]<=5","clause_id":"C2"},
                 {"clause":"optional clause","status":"assumed","statement_quote":"if optional, use a slower method."}]
        self.assertEqual(root_scout.contract_quote_links("1<=n<=2; -5<=a[i]<=5",req,clauses),["C1","C2"])
        self.assertEqual(root_scout.contract_quote_links("1<=n<=2; if optional",req,clauses),[])
        self.assertEqual(root_scout.contract_quote_links("!(1<=n<=2)",{"statement":"!(1<=n<=2)","source":"x","language":"python3"},clauses[:1]),[])
        self.assertEqual(root_scout.contract_quote_links("1<=n<=2 => -5<=a[i]<=5",{"statement":"1<=n<=2 => -5<=a[i]<=5","source":"x","language":"python3"},clauses[:2]),[])
        tool={"statement_quote":"1-based closed interval queries","domain_quote":"1<=n<=100"}
        tool_req={"statement":"For 1-based closed interval queries, 1<=n<=100.","source":"print(0)","language":"python3"}
        bound=root_scout.bind_contract_evidence([],tool_req,tool)
        self.assertEqual(root_scout.contract_quote_links(tool["statement_quote"],tool_req,bound),["TOOL-semantics"])
        self.assertEqual(root_scout.contract_quote_links(tool["domain_quote"],tool_req,bound),["TOOL-domain"])

    def test_four_root_plan_keeps_full_cumulative_then_four_leave_one_out(self):
        source="a=0\nb=0\nc=0\nd=0\n"
        roots=[{"id":f"R{i}","status":"confirmed","evidence_level":"E2","original_location":{"line":i},"source_excerpt":f"{chr(96+i)}=0"} for i in range(1,5)]
        proposals=[{"root_id":f"R{i}","source_line":i,"old_text":f"{chr(96+i)}=0","new_text":f"{chr(96+i)}=1"} for i in range(1,5)]
        patches=root_scout.local_patch_plan(source,roots,proposals)
        self.assertEqual(len(patches),4)
        variants=root_scout.intervention_variant_sets(patches)
        self.assertEqual([v["root_ids"] for v in variants],[["R1","R2","R3","R4"],["R2","R3","R4"],["R1","R3","R4"],["R1","R2","R4"],["R1","R2","R3"]])
        root_scout.validate_model_result({"patches":proposals},"INTERVENE")

    def test_interaction_confounded_keeps_single_patch_regression_and_requires_safe_cumulative(self):
        source="best=0\nfor x in a[:-1]: pass\n"
        roots=[{"id":"R1","status":"confirmed","evidence_level":"E2","original_location":{"line":1},"source_excerpt":"best=0"},
               {"id":"R2","status":"confirmed","evidence_level":"E2","original_location":{"line":2},"source_excerpt":"a[:-1]"}]
        proposals=[{"root_id":"R1","source_line":1,"old_text":"best=0","new_text":"best=-1000000001"},
                   {"root_id":"R2","source_line":2,"old_text":"a[:-1]","new_text":"a"}]
        patches=root_scout.local_patch_plan(source,roots,proposals)
        cases=[("e1","2\n-2 -4\n","-2\n"),("control","1\n0\n","0\n"),("e2","2\n0 3\n","3\n")]
        evidence=[]
        baseline={"e1":"0\n","control":"0\n","e2":"0\n"}
        for eid,inp,expected in cases:
            stdout=baseline[eid]
            evidence.append({"id":eid,"trusted_expected":True,"input":inp,"expected":expected,
                             "result":{"status":"ok","stdout":stdout,"matched":stdout==expected,"evidence_level":"E1" if eid!="control" else None}})
        variant_outputs={"variant-R1":{"e1":"-2\n","control":"-1000000001\n","e2":"0\n"},
                         "variant-R2":{"e1":"0\n","control":"0\n","e2":"3\n"},
                         "variant-R1R2":{"e1":"-2\n","control":"0\n","e2":"3\n"}}
        variants=[]
        for spec in root_scout.intervention_variant_sets(patches):
            vid=spec["id"]; matrix=[]
            for eid,inp,expected in cases:
                stdout=variant_outputs[vid][eid]; result={"status":"ok","stdout":stdout,"matched":stdout==expected}
                row=root_scout.intervention_row_result(next(e for e in evidence if e["id"]==eid),result)
                matrix.append({"evidence_id":eid,"original":row["original"],"variant":result,"classification":row["classification"]})
            variant_status="regressed" if any(m["classification"]=="variant_risk" for m in matrix) else "completed"
            variants.append({**spec,"status":variant_status,"matrix":matrix})
        first=root_scout.intervention_root_result({"id":"R1","evidence_ids":["e1"]},patches,variants,evidence)
        self.assertEqual(first["status"],"interaction_confounded")
        self.assertEqual(first["depends_on_root_ids"],["R2"])
        self.assertEqual(first["evidence_ids"],["e1"])
        self.assertEqual(first["variant_risk_evidence_ids"],["control"])
        self.assertIn(("variant-R1","control"),root_scout.conditionally_accounted_variant_risks([{"intervention":first}],variants))
        second=root_scout.intervention_root_result({"id":"R2","evidence_ids":["e2"]},patches,variants,evidence)
        self.assertEqual(second["status"],"supported")
        bad=copy.deepcopy(variants)
        full=next(v for v in bad if v["id"]=="variant-R1R2")
        full["matrix"][-1]["variant"].update(stdout="0\n",matched=False)
        full["matrix"][-1]["classification"]="unchanged"
        bad_result=root_scout.intervention_root_result({"id":"R1","evidence_ids":["e1"]},patches,bad,evidence)
        self.assertNotEqual(bad_result["status"],"interaction_confounded")
        self.assertIn(bad_result["status"],("blocked","unresolved"))

    def test_shared_e1_cannot_claim_independent_patch_support_when_other_patch_also_fixes_it(self):
        patches=[{"root_id":"R1"},{"root_id":"R2"}]
        evidence=[
            {"id":"e1","trusted_expected":True,"input":"x","expected":"1","result":{"status":"ok","stdout":"0","matched":False}},
            {"id":"control","trusted_expected":True,"input":"y","expected":"0","result":{"status":"ok","stdout":"0","matched":True}},
        ]
        specs=[{"id":"variant-R1","root_ids":["R1"]},{"id":"variant-R2","root_ids":["R2"]},{"id":"variant-R1R2","root_ids":["R1","R2"]}]
        outputs={"variant-R1":{"e1":"0","control":"0"},"variant-R2":{"e1":"1","control":"0"},"variant-R1R2":{"e1":"1","control":"0"}}
        variants=[]
        for spec in specs:
            matrix=[]
            for ev in evidence:
                result={"status":"ok","stdout":outputs[spec["id"]][ev["id"]]}
                row=root_scout.intervention_row_result(ev,result); result["matched"]=row["variant_match"]
                matrix.append({"evidence_id":ev["id"],"original":row["original"],"variant":result,"classification":row["classification"]})
            variants.append({**spec,"status":"completed","matrix":matrix})
        first=root_scout.intervention_root_result({"id":"R1","evidence_ids":["e1"]},patches,variants,evidence)
        second=root_scout.intervention_root_result({"id":"R2","evidence_ids":["e1"]},patches,variants,evidence)
        self.assertEqual(first["status"],"cross_root_confounded")
        self.assertEqual(first["evidence_ids"],["e1"])
        self.assertEqual(second["status"],"supported")
        self.assertEqual(second["evidence_ids"],["e1"])

    def test_legacy_snapshot_replay_rebinds_links_but_downgrades_nonreproduced_e1(self):
        path="validation/holdout-live-serial-96k/max-masked-cpp20/case_state.json"
        old=root_scout.load_json(path); request=root_scout.verify_saved_case_state(old)
        self.assertTrue(any("contract_evidence_links" not in root for root in old["roots"]))
        def passing_runner(_source,_language,case,_env,profile="formal",build_cache=None):
            expected=case.get("expected","")
            return {"status":"ok","returncode":0,"stdout":expected,"stderr":"","matched":True,"wall_seconds":0}
        runtime=old.get("run_configuration",{}).get("runtime",{})
        with patch.object(root_scout,"docker_preflight",return_value=runtime),patch.object(root_scout,"run_case",side_effect=passing_runner),patch.object(root_scout,"docker_run",side_effect=passing_runner):
            fresh=root_scout._diagnose_legacy(request,"replay",replay_state=old)
        self.assertEqual(fresh["roots"],[])
        self.assertTrue(all(x["status"]=="not_reproduced" for x in fresh["replay_prior_roots"]))
        self.assertTrue(all(c["oracle_implementation_sha256"]==root_scout.oracle_tools.ORACLE_IMPLEMENTATION_SHA256 for c in fresh["oracle_test_plan"]))
        self.assertTrue(all(root_scout.oracle_tools.implementation_sha256_supported(c["oracle_implementation_sha256"],"max.v1") for c in fresh["residual_oracle_test_plan"]))
        self.assertEqual(root_scout.verify_saved_case_state(fresh),request)

class CausalLoopTests(unittest.TestCase):
    def request(self):
        return {"statement":"Print the input value.","source":"x=0\nprint(x)","language":"python3",
                "known_failing_tests":[{"input":"one\n","expected":"1\n","trusted":True,"expected_source":"hand checked","legal_input":True,"legal_input_source":"valid singleton"}]}

    def response(self, edit=True):
        return {"contract":[{"ref":"c1","clause":"output value","status":"stated","statement_quote":"Print the input value."}],"assumptions":[],
                "hypotheses":[{"kind":"defect_candidate","ref":"h1","clause_refs":["c1"],"source_spans":[{"line":1,"excerpt":"x=0"}],"claim":"wrong initial constant","mechanism":"initial state","granularity":"single","falsifier":"a trusted input where changing x does not change mismatch"}],
                "experiments":[{"ref":"e1","target_refs":["h1"],"background_refs":[],"edits":[{"hypothesis_refs":["h1"],"line":1,"old_text":"x=0","new_text":"x=1" if edit else "x=2"}],"inputs":[],"non_target_clause_refs":[],"distinguishes":"whether the initial value causes the mismatch","rationale":"change only the anchored initial value"}]}

    @staticmethod
    def runner(source,_language,case,_env,profile="formal",build_cache=None):
        output="1\n" if "x=1" in source else "0\n"
        return {"status":"ok","stdout":output,"stderr":"","returncode":0,"matched":output==case.get("expected",""),"wall_seconds":0.01}

    def test_causal_experiment_derives_finding_and_replay_certificate(self):
        req=self.request(); reply=self.response()
        def ask(_model,phase,_prompt,_budget,max_output=None):
            self.assertEqual(phase,"HYPOTHESIS_EXPERIMENT"); return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req)
        self.assertEqual(state["stop_reason"],"completed_with_evidence")
        self.assertEqual([h["status"] for h in state["hypotheses"]],["supported_finite"])
        self.assertEqual(state["roots"][0]["intervention"]["status"],"supported")
        self.assertEqual(state["roots"][0]["evidence_ids"],["T0001"])
        self.assertEqual(state["coverage"]["residual_status"],"complete")
        self.assertEqual(state["coverage"]["known_explained_evidence_ids"],["T0001"])
        self.assertIn("no text residual audit",state["coverage"]["residual_status_basis"])
        sandbox=state["run_configuration"]["sandbox"]
        self.assertEqual(sandbox["stdout_bytes_max"],root_scout.SANDBOX_STDOUT_LIMIT_BYTES)
        self.assertEqual(sandbox["stderr_bytes_max"],root_scout.SANDBOX_STDERR_LIMIT_BYTES)
        self.assertEqual(sandbox["output_bytes_max"],root_scout.SANDBOX_OUTPUT_LIMIT_BYTES)
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        forged_scope=copy.deepcopy(state); forged_scope["coverage"]["residual_status"]="unresolved"
        with self.assertRaisesRegex(ValueError,"finite-scope compatibility fields do not recompute"):
            root_scout.verify_saved_case_state(forged_scope)
        changed=copy.deepcopy(state); changed["experiments"][0]["matrix"][0]["target_matched"]=False
        changed["integrity"]["causal_ledger_sha256"]=root_scout.canonical_sha(root_scout.causal_ledger_payload(changed))
        with self.assertRaisesRegex(ValueError,"match flags do not recompute"):
            root_scout.verify_saved_case_state(changed)

    def test_causal_update_system_is_self_contained_and_separates_ledger_from_proposals(self):
        system=root_scout.system_for_phase("CAUSAL_UPDATE")
        for expected in ("updates,hypotheses,experiments,merge_proposals","kind=defect_candidate|correctness_coverage|resource_concern",
                         "granularity=single|group","source_spans is 1-8 exact {line,excerpt} objects",
                         "Canonical mechanism labels are initialization|comparison_update|indexing_bounds|iteration_count|numeric_width",
                         "disposition=retain|refine|merge|dismiss|blocked","merge_proposals are exactly {source_hypothesis_ids,target_hypothesis_id,basis_evidence_ids,rationale}",
                         "experiments are exactly {ref,target_refs,background_refs,edits,inputs,non_target_clause_refs,distinguishes,rationale}",
                         "granularity=single|group","Canonical mechanism labels are initialization|comparison_update|indexing_bounds|iteration_count|numeric_width",
                         "At most 12 new hypotheses and 6 experiments","edits is 1-4",
                         "background_result=background edits only","target_result=background+target",
                         "leave_one_out_results[H]=full set with H withdrawn"):
            self.assertIn(expected,system)
        self.assertIn("Statement-backed C IDs refer only to exact request substrings",system)
        self.assertIn("Caller tool authorization is separate",system)
        initial=root_scout.causal_prompt(self.request(),[],updates=False)["instruction"]
        feedback=root_scout.causal_prompt(self.request(),[],contract=[],hypotheses=[],experiments=[],evidence=[],updates=True)["instruction"]
        for instruction in (initial,feedback):
            self.assertIn("TOOL-semantics",instruction)
            self.assertIn("statement",instruction.lower())
        self.assertIn("do not poison other valid clauses",initial)
        self.assertIn("rejected",feedback)
        self.assertIn("values[0]",initial); self.assertIn("outside-domain sentinel",initial)
        self.assertIn("every declared target and background H must own at least one actual edit",system)
        rejected=[{"proposal":{"ref":"E1"},"status":"rejected","reason":"every declared target/background hypothesis must own at least one edit: H2"}]
        prompt=root_scout.causal_prompt(self.request(),[],contract=[],hypotheses=[],experiments=[],evidence=[],updates=True,experiment_decisions=rejected)
        self.assertEqual(prompt["prior_experiment_decisions"],[{"proposal_ref":"E1","status":"rejected","reason":"every declared target/background hypothesis must own at least one edit: H2"}])
        self.assertIn("Correct each prior experiment rejection",prompt["instruction"])
        self.assertIn("background=[H2]",prompt["instruction"])
        response={"updates":[{"hypothesis_id":"H1","disposition":"retain","evidence_ids":["T0001"],"reason":"still open"}],
                  "hypotheses":[{"kind":"defect_candidate","ref":"h2","clause_refs":["C1"],"source_spans":[{"line":2,"excerpt":"x=0"}],
                                 "claim":"possible initialization defect","mechanism":"initialization","granularity":"single","falsifier":"trusted case passes unchanged"}],
                  "experiments":[],
                  "merge_proposals":[{"source_hypothesis_ids":["H1"],"target_hypothesis_id":"H2","basis_evidence_ids":["T0001"],"rationale":"related evidence"}]}
        root_scout.validate_model_result(response,"CAUSAL_UPDATE")
        malformed=copy.deepcopy(response); malformed["merge_proposals"][0].pop("target_hypothesis_id")
        with self.assertRaisesRegex(ValueError,"merge proposal requires source, target, evidence basis"):
            root_scout.validate_model_result(malformed,"CAUSAL_UPDATE")
        update=root_scout.causal_prompt(self.request(),[],[],[],[],[],updates=True)
        self.assertNotIn("use the HYPOTHESIS_EXPERIMENT field schemas",update["instruction"])

    def test_experiment_cannot_claim_background_without_an_owned_edit(self):
        req=self.request(); response=self.response()
        response["hypotheses"].append({"kind":"defect_candidate","ref":"h2","clause_refs":["c1"],"source_spans":[{"line":1,"excerpt":"x=0"}],
            "claim":"second mechanism","mechanism":"comparison","granularity":"single","falsifier":"independent passing contrast"})
        response["experiments"][0]["background_refs"]=["h2"]
        # The only edit remains owned by h1; naming h2 as background must not gain causal credit.
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(response)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["experiments"],[])
        self.assertEqual(state["experiment_decisions"][0]["status"],"rejected")
        self.assertIn("must own at least one edit",state["experiment_decisions"][0]["reason"])
        self.assertEqual(state["findings"],[])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_initial_contract_refs_keep_proposal_namespace_and_never_promote_nonstated(self):
        req={**self.request(),"statement":"Given n and values, return the maximum."}
        response={"contract":[
            {"ref":"C1","clause":"derived observation","status":"derived","statement_quote":""},
            {"ref":"A1","clause":"assumption","status":"assumed","statement_quote":""},
            {"ref":"U1","clause":"unknown","status":"unknown","statement_quote":""},
            {"ref":"TOOL-domain","clause":"derived collision","status":"derived","statement_quote":""},
            {"ref":"max-output","clause":"output maximum","status":"stated","statement_quote":"return the maximum"},
            {"ref":"values-domain","clause":"integer input","status":"stated","statement_quote":"Given n and values"}]}
        oracle_contract={"statement_quote":"return the maximum","domain_quote":"Given n and values",
                         "semantics":"maximum","domain":"integer","reviewed_by":"caller"}
        contract,refs=root_scout.causal_initial_contract_check(response,req,oracle_contract)
        self.assertEqual(refs["C1"],None)  # Does not collide into the stated controller C1.
        self.assertEqual(refs["max-output"],"C1")
        self.assertEqual(refs["values-domain"],"C2")
        self.assertEqual(refs["TOOL-domain"],None)  # Raw proposal refs shadow implicit tool aliases.
        self.assertNotIn("C2",refs)  # Initial proposals cannot guess internal controller IDs.
        state={"contract":contract,"hypotheses":[],"hypothesis_decisions":[]}
        def hypothesis(ref,clause_ref):
            return {"ref":ref,"clause_refs":[clause_ref],"source_spans":[{"line":1,"excerpt":"x=0"}],
                    "claim":"candidate","mechanism":"initialization","granularity":"single","falsifier":"a discriminating trusted case"}
        proposals=[hypothesis("bad-derived","C1"),hypothesis("bad-assumed","A1"),hypothesis("bad-unknown","U1"),
                   hypothesis("bad-tool-collision","TOOL-domain"),hypothesis("bad-controller-id","C2"),
                   hypothesis("good","max-output")]
        aliases=root_scout.causal_add_hypotheses(state,proposals,refs,req)
        self.assertEqual(aliases,{"good":"H1"})
        self.assertEqual([d["status"] for d in state["hypothesis_decisions"]],
                         ["rejected"]*5+["accepted_open"])
        decisions=state["hypothesis_decisions"][0]["clause_reference_decisions"]
        self.assertEqual(decisions[0]["resolution"],"unverified_context")
        self.assertEqual(state["hypothesis_decisions"][4]["clause_reference_decisions"][0]["resolution"],"unknown_context")
        self.assertEqual([d["resolution"] for d in root_scout.contract_reference_decisions(contract) if d["ref"] in ("C1","A1","U1","TOOL-domain")],
                         ["rejected_nonstated"]*4)
        followup=root_scout.causal_add_hypotheses(state,[hypothesis("followup","C1")],
            {"C1":"C1","C2":"C2"},req,namespace="controller")
        self.assertEqual(followup,{"followup":"H2"})
        feedback_refs,feedback_ids=root_scout.resolve_contract_refs(contract,["C1","A1","not-present"],
            {"C1":"C1","C2":"C2"}, "controller")
        self.assertEqual(feedback_ids,["C1"])
        self.assertEqual(feedback_refs[1]["clause_status"],"assumed")
        self.assertEqual(feedback_refs[2]["resolution"],"unknown_context")

    def test_mixed_contract_statuses_do_not_crash_initial_diagnosis(self):
        req=self.request()
        reply={"contract":[
            {"ref":"C1","clause":"derived note","status":"derived","statement_quote":""},
            {"ref":"A","clause":"assumption","status":"assumed","statement_quote":""},
            {"ref":"U","clause":"unknown","status":"unknown","statement_quote":""},
            {"ref":"output","clause":"output requirement","status":"stated","statement_quote":"Print the input value."}],
            "assumptions":[],"hypotheses":[],"experiments":[]}
        def ask(_model,phase,_prompt,_budget,max_output=None):
            self.assertEqual(phase,"HYPOTHESIS_EXPERIMENT")
            return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["contract_binding_status"],"bound")
        self.assertEqual(len(state["contract_reference_decisions"]),4)
        self.assertEqual(state["stop_reason"],"oracle_unresolved")  # E1 remains unadjudicated.
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_clean_positive_claim_records_finite_coverage_without_opening_a_bug(self):
        req=self.request(); req["known_failing_tests"][0]["expected"]="0\n"
        reply={"contract":[{"ref":"c1","clause":"output value","status":"stated","statement_quote":"Print the input value."}],
               "assumptions":[],"hypotheses":[
                   {"kind":"correctness_coverage","ref":"q","clause_refs":["c1"],"source_spans":[{"line":2,"excerpt":"print(x)"}],
                    "claim":"the original output matches the supplied example","mechanism":"output behavior","granularity":"single","falsifier":"a trusted original output mismatch"},
                   {"kind":"resource_concern","ref":"r","clause_refs":["c1"],"source_spans":[{"line":2,"excerpt":"print(x)"}],
                    "claim":"runtime may need benchmarking","mechanism":"runtime","granularity":"single","falsifier":"an explicit performance measurement"}],
               "experiments":[]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["hypotheses"],[])
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        claims={x["kind"]:x for x in state["non_bug_claims"]}
        self.assertEqual(claims["correctness_coverage"]["status"],"finite_tests_passed")
        self.assertEqual(claims["resource_concern"]["status"],"unverified")
        self.assertEqual(state["coverage"]["trusted_test_status"],"no_failure_observed")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        forged=copy.deepcopy(state); forged["non_bug_claims"][0]["status"]="globally_correct"
        forged["integrity"]["causal_ledger_sha256"]=root_scout.canonical_sha(root_scout.causal_ledger_payload(forged))
        with self.assertRaisesRegex(ValueError,"finite coverage/resource observations do not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_coverage_label_cannot_hide_an_original_mismatch(self):
        req=self.request()  # Expected 1, original emits 0.
        reply={"contract":[{"ref":"c1","clause":"output value","status":"stated","statement_quote":"Print the input value."}],
               "assumptions":[],"hypotheses":[
                   {"kind":"correctness_coverage","ref":"q","clause_refs":["c1"],"source_spans":[{"line":2,"excerpt":"print(x)"}],
                    "claim":"output is correct","mechanism":"output behavior","granularity":"single","falsifier":"a trusted mismatch"}],
               "experiments":[]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["non_bug_claims"][0]["status"],"finite_mismatch_observed")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertEqual(state["coverage"]["unresolved_e1_ids"],["T0001"])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_missing_kind_is_a_recorded_schema_error_not_implicit_root_classification(self):
        req=self.request(); reply=self.response(); reply["hypotheses"][0].pop("kind")
        with self.assertRaisesRegex(ValueError,"kind is required"):
            root_scout.validate_model_result(reply,"HYPOTHESIS_EXPERIMENT")
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["hypotheses"],[])
        self.assertEqual(state["analysis_errors"][0]["type"],"ValueError")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_trusted_defect_with_unverified_clause_context_can_run_but_context_is_not_exclusion(self):
        req=self.request()
        reply={"contract":[{"ref":"derived","clause":"derived note","status":"derived","statement_quote":""},
                           {"ref":"assumed","clause":"assumption","status":"assumed","statement_quote":""},
                           {"ref":"unknown","clause":"unknown detail","status":"unknown","statement_quote":""},
                           {"ref":"output","clause":"output","status":"stated","statement_quote":"Print the input value."}],
               "assumptions":[],"hypotheses":[
                   {"kind":"defect_candidate","ref":"h","clause_refs":["derived","assumed","unknown","output"],
                    "source_spans":[{"line":1,"excerpt":"x=0"}],"claim":"wrong initial value","mechanism":"initialization",
                    "granularity":"single","falsifier":"changing the initial value removes the trusted mismatch"}],
               "experiments":[{"ref":"e","target_refs":["h"],"background_refs":[],
                               "edits":[{"hypothesis_refs":["h"],"line":1,"old_text":"x=0","new_text":"x=1"}],
                               "inputs":[],"non_target_clause_refs":["derived","assumed","unknown","missing"],
                               "distinguishes":"test the initial value","rationale":"change only the anchored value"}]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["hypotheses"][0]["clause_ids"],["C1"])
        self.assertEqual([x["role"] for x in state["hypotheses"][0]["context_references"]],["context"]*3)
        self.assertEqual(state["experiments"][0]["non_target_clause_ids"],[])
        self.assertEqual([x["role"] for x in state["experiments"][0]["non_target_clause_reference_decisions"]],["context"]*4)
        self.assertEqual(len(state["experiments"][0]["non_target_clause_contexts"]),4)
        self.assertTrue(all(x["verified"] is False for x in state["experiments"][0]["non_target_clause_contexts"]))
        self.assertEqual(state["experiments"][0]["non_target_exclusion_status"],"declarations_not_independently_verified")
        self.assertEqual(state["roots"][0]["intervention"]["status"],"supported")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)
        forged=copy.deepcopy(state); forged["experiments"][0]["non_target_clause_contexts"][0]["verified"]=True
        forged["integrity"]["causal_ledger_sha256"]=root_scout.canonical_sha(root_scout.causal_ledger_payload(forged))
        with self.assertRaisesRegex(ValueError,"non-target authority/context does not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_edit_span_normalizes_only_surrounding_whitespace(self):
        source="    int sum=0;\r\n"
        hyp={"id":"H1","clause_ids":["C1"],"source_spans":[{"line":1,"excerpt":"int sum=0"}],"granularity":"single"}
        raw={"target_refs":["H1"],"background_refs":[],"edits":[
            {"hypothesis_refs":["H1"],"line":1,"old_text":"    int sum=0","new_text":"    long long sum=0"}],
            "non_target_clause_refs":[]}
        result=root_scout.normalize_causal_edits(source,raw,[hyp],{"C1"},{},enforce_single_mechanism=True)
        edit=result["edits"][0]
        self.assertEqual((edit["old_text"],edit["new_text"],edit["start"],edit["end"]),("int sum=0","long long sum=0",4,13))
        self.assertEqual(edit["span_normalization"]["removed_prefix_whitespace"],"    ")
        changed=root_scout.apply_causal_edit_set(source,result["edits"],["H1"])
        self.assertEqual(changed,"    long long sum=0;\r\n")
        bad_source="X int sum=0;"
        bad_hyp={**hyp,"source_spans":[{"line":1,"excerpt":"int sum=0"}]}
        bad={**raw,"edits":[{**raw["edits"][0],"old_text":"X int sum=0","new_text":"X long long sum=0"}]}
        with self.assertRaisesRegex(ValueError,"extend only through surrounding whitespace"):
            root_scout.normalize_causal_edits(bad_source,bad,[bad_hyp],{"C1"},{},enforce_single_mechanism=True)

    def test_normalized_span_is_bound_and_recomputed_in_saved_case(self):
        req=self.request()
        req["source"]="def f():\n    x=0\n    return x\nprint(f())"
        reply=self.response()
        reply["hypotheses"][0]["source_spans"]=[{"line":2,"excerpt":"x=0"}]
        reply["experiments"][0]["edits"]=[{"hypothesis_refs":["h1"],"line":2,
            "old_text":"    x=0","new_text":"    x=1"}]
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(reply)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        edit=state["experiments"][0]["edits"][0]
        self.assertEqual(edit["old_text"],"x=0")
        self.assertEqual(edit["span_normalization"]["raw_old_text"],"    x=0")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_bad_patch_stays_open_and_one_shot_and_causal_share_first_packet(self):
        req=self.request(); initial=self.response(edit=False); prompts={};
        def execute(strategy):
            def ask(_model,phase,prompt,_budget,max_output=None):
                if phase=="HYPOTHESIS_EXPERIMENT": prompts[strategy]=prompt; return copy.deepcopy(initial)
                self.assertEqual(phase,"CAUSAL_UPDATE")
                return {"updates":[],"hypotheses":[],"experiments":[],"merge_proposals":[]}
            with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
                return root_scout.diagnose_causal(req,strategy=strategy)
        single=execute("one_shot"); causal=execute("causal")
        self.assertEqual(prompts["one_shot"],prompts["causal"])
        self.assertEqual(single["coverage"]["feedback_calls"],0)
        self.assertEqual(causal["coverage"]["feedback_calls"],1)
        self.assertEqual(causal["hypotheses"][0]["status"],"open")
        self.assertEqual(causal["roots"],[])
        self.assertEqual(causal["stop_reason"],"oracle_unresolved")
        self.assertEqual(root_scout.verify_saved_case_state(causal),req)

    def test_v5_rejects_forged_completion_with_open_hypothesis_or_unresolved_e1(self):
        req=self.request(); initial=self.response(edit=False)
        def ask(_model,phase,_prompt,_budget,max_output=None): return copy.deepcopy(initial)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertTrue(state["coverage"]["open_hypothesis_ids"])
        self.assertTrue(state["coverage"]["unresolved_e1_ids"])
        forged=copy.deepcopy(state); forged["stop_reason"]="completed_with_evidence"
        forged["coverage"]["open_hypothesis_ids"]=[]; forged["coverage"]["unresolved_e1_ids"]=[]
        forged["integrity"]["causal_ledger_sha256"]=root_scout.canonical_sha(root_scout.causal_ledger_payload(forged))
        with self.assertRaisesRegex(ValueError,"open hypothesis IDs do not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_v5_rejects_no_bug_closure_after_formal_execution_failure(self):
        req=self.request(); answer={"contract":[{"ref":"c1","clause":"output value","status":"stated","statement_quote":"Print the input value."}],"assumptions":[],"hypotheses":[],"experiments":[]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(answer)
        def failed_runner(*_args,**_kwargs): return {"status":"compile_failed","stdout":"","stderr":"compile error","returncode":1,"wall_seconds":0.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=failed_runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        forged=copy.deepcopy(state); forged["stop_reason"]="no_bug_found_within_scope"
        forged["coverage"]["execution_failures"]=[]
        forged["integrity"]["causal_ledger_sha256"]=root_scout.canonical_sha(root_scout.causal_ledger_payload(forged))
        with self.assertRaisesRegex(ValueError,"formal execution failures do not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_unknown_feedback_target_is_rejected_without_losing_open_hypothesis(self):
        req=self.request(); initial=self.response(); initial["experiments"]=[]; update={"updates":[],"hypotheses":[],"merge_proposals":[],
            "experiments":[{"ref":"bad","target_refs":["H99"],"background_refs":[],"edits":[{"hypothesis_refs":["H99"],"line":1,"old_text":"x=0","new_text":"x=1"}],"inputs":[],"non_target_clause_refs":[],"distinguishes":"unknown target","rationale":"must be rejected"}]}
        def ask(_model,phase,_prompt,_budget,max_output=None): return copy.deepcopy(initial if phase=="HYPOTHESIS_EXPERIMENT" else update)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req)
        self.assertEqual(state["experiments"],[])
        self.assertEqual(state["experiment_decisions"][0]["status"],"rejected")
        self.assertEqual(state["hypotheses"][0]["status"],"open")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_oracle_recomputes_generated_expected_and_rejects_out_of_domain(self):
        req=root_scout.load_json("examples/max_oracle_demo.json")
        contract,_,error=root_scout.make_oracle_plan(req)
        self.assertIsNone(error)
        case,error=root_scout.causal_test_from_input(req,contract,"3\n-8 -2 -5\n")
        self.assertIsNone(error); self.assertEqual(case["expected"],"-2\n")
        invalid,error=root_scout.causal_test_from_input(req,contract,"0\n\n")
        self.assertIsNone(invalid); self.assertTrue(error)

    def test_correct_program_does_not_receive_invented_hypotheses(self):
        req=self.request(); req["source"]="x=1\nprint(x)"
        answer={"contract":[{"ref":"c1","clause":"output value","status":"stated","statement_quote":"Print the input value."}],"assumptions":[],"hypotheses":[],"experiments":[]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(answer)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req)
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(state["hypotheses"],[])
        self.assertEqual(state["coverage"]["obligation_ledger"][0]["status"],"not_assessed")
        self.assertEqual(state["coverage"]["resource_status"],"unverified")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_controller_generated_oracle_input_runs_and_enters_replay_plan(self):
        req=root_scout.load_json("examples/max_oracle_demo.json")
        answer={"contract":[{"ref":"c1","clause":"maximum output","status":"stated","statement_quote":"Given n integers, output their maximum."}],"assumptions":[],
                "hypotheses":[{"kind":"defect_candidate","ref":"h1","clause_refs":["c1"],"source_spans":[{"line":3,"excerpt":"best=0"}],"claim":"zero is not a valid initial maximum for negative values","mechanism":"initial state","granularity":"single","falsifier":"all-negative trusted input passes unchanged"}],
                "experiments":[{"ref":"e1","target_refs":["h1"],"background_refs":[],"edits":[{"hypothesis_refs":["h1"],"line":3,"old_text":"best=0","new_text":"best=-1000000001"}],"inputs":["3\n-7 -4 -5\n"],"non_target_clause_refs":[],"distinguishes":"negative-only arrays","rationale":"isolated sentinel change"}]}
        def runner(source,_lang,case,_env,profile="formal",build_cache=None):
            vals=list(map(int,case["input"].split())); values=vals[1:1+vals[0]]
            actual=max(values) if "best=-1000000001" in source else max(0,max(values))
            out=f"{actual}\n"
            return {"status":"ok","stdout":out,"stderr":"","returncode":0,"matched":out==case.get("expected",""),"wall_seconds":.01}
        def ask(_model,phase,_prompt,_budget,max_output=None):
            self.assertEqual(phase,"HYPOTHESIS_EXPERIMENT"); return copy.deepcopy(answer)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        generated=next(e for e in state["evidence"] if e["input"]=="3\n-7 -4 -5\n")
        self.assertEqual(generated["expected"],"-4\n"); self.assertTrue(generated["source_kind"].startswith("builtin_oracle:"))
        self.assertIn(generated["id"],state["experiments"][0]["test_ids"])
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_multi_hypothesis_compensation_remains_group_support(self):
        req={"statement":"Print the total of the two declared components.","source":"a=0\nb=0\nprint(a+b)","language":"python3",
             "known_failing_tests":[{"input":"two\n","expected":"2\n","trusted":True,"expected_source":"checked sum","legal_input":True,"legal_input_source":"two-component case"}]}
        answer={"contract":[{"ref":"c","clause":"sum","status":"stated","statement_quote":"Print the total of the two declared components."}],"assumptions":[],
                "hypotheses":[{"kind":"defect_candidate","ref":"a","clause_refs":["c"],"source_spans":[{"line":1,"excerpt":"a=0"}],"claim":"first component is lost","mechanism":"component assignment","granularity":"group","falsifier":"first component contributes correctly"},
                              {"kind":"defect_candidate","ref":"b","clause_refs":["c"],"source_spans":[{"line":2,"excerpt":"b=0"}],"claim":"second component is lost","mechanism":"component assignment","granularity":"group","falsifier":"second component contributes correctly"}],
                "experiments":[{"ref":"joint","target_refs":["a","b"],"background_refs":[],"edits":[{"hypothesis_refs":["a"],"line":1,"old_text":"a=0","new_text":"a=1"},{"hypothesis_refs":["b"],"line":2,"old_text":"b=0","new_text":"b=1"}],"inputs":[],"non_target_clause_refs":[],"distinguishes":"the combined assignment change","rationale":"only the joint variant has interpretable output"}]}
        def runner(source,_lang,case,_env,profile="formal",build_cache=None):
            out="2\n" if "a=1" in source and "b=1" in source else "0\n"
            return {"status":"ok","stdout":out,"stderr":"","returncode":0,"matched":out==case.get("expected",""),"wall_seconds":.01}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(answer)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["roots"][0]["intervention"]["status"],"jointly_supported")
        self.assertEqual(state["roots"][0]["intervention"]["support_kind"],"group")
        self.assertEqual(len(state["roots"]),1)
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_combined_certificate_does_not_duplicate_individual_roots(self):
        hypotheses=[{"id":"H1","claim":"init","clause_ids":["C1"],"source_spans":[{"line":1,"excerpt":"a=0"}]},
                    {"id":"H2","claim":"update","clause_ids":["C1"],"source_spans":[{"line":2,"excerpt":"b=0"}]}]
        contract=[{"clause_id":"C1","status":"stated","statement_quote":"maximize values"}]
        findings=[{"experiment_id":"X1","hypothesis_ids":["H1"],"evidence_ids":["e1"],"support_kind":"finite","background_hypothesis_ids":[],"status":"finite_causal_support"},
                  {"experiment_id":"X2","hypothesis_ids":["H2"],"evidence_ids":["e2"],"support_kind":"finite","background_hypothesis_ids":[],"status":"finite_causal_support"},
                  {"experiment_id":"X3","hypothesis_ids":["H1","H2"],"evidence_ids":["e3"],"support_kind":"group","background_hypothesis_ids":[],"status":"finite_causal_support"}]
        roots=root_scout.causal_findings_to_roots(findings,hypotheses,contract)
        self.assertEqual([r["claim"] for r in roots],["init","update"])
        self.assertEqual(len(roots[0]["related_group_certificates"]),1)
        self.assertEqual(roots[0]["related_group_certificates"][0]["evidence_ids"],["e3"])

    def test_causal_root_primary_location_comes_from_supported_target_edit(self):
        hypotheses=[{"id":"H1","claim":"comparison is reversed","clause_ids":["C1"],
                     "source_spans":[{"line":1,"excerpt":"best=0"},{"line":2,"excerpt":"for x in values:"},{"line":3,"excerpt":"if x>best: best=x"}]}]
        contract=[{"clause_id":"C1","status":"stated","statement_quote":"output the minimum"}]
        findings=[{"experiment_id":"X1","hypothesis_ids":["H1"],"evidence_ids":["T1"],"support_kind":"finite","background_hypothesis_ids":[],"status":"finite_causal_support",
                   "primary_location":{"line":3,"excerpt":"x>best","mechanism_class":"comparison_update"},
                   "target_edit_spans":[{"line":3,"excerpt":"x>best","mechanism_class":"comparison_update"}],
                   "mechanism_validation":{"H1":{"status":"validated","edit_mechanism_classes":["comparison_update"],"reason":"checked","limits":[]}}}]
        root=root_scout.causal_findings_to_roots(findings,hypotheses,contract)[0]
        self.assertEqual(root["source_line"],3)
        self.assertEqual(root["source_excerpt"],"x>best")
        self.assertEqual(root["primary_source_span"],{"line":3,"excerpt":"x>best"})
        self.assertIn({"line":1,"excerpt":"best=0"},root["context_source_spans"])

    def test_min_negative_sentinel_is_output_only_not_mechanism_support(self):
        source="best=0\nfor x in values:\n    if x>best: best=x"
        hypotheses=[{"id":"H1","granularity":"single","mechanism":"initial state"}]
        experiment={"target_hypothesis_ids":["H1"],"background_hypothesis_ids":[],"edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"best=0","new_text":"best=-1000000001"}]}
        oracle={"kind":"range_min.v1","domain":{"value_min":-1000000000,"value_max":1000000000}}
        validation=root_scout.causal_mechanism_validation(source,experiment,hypotheses,oracle)
        self.assertEqual(validation["H1"]["status"],"inconsistent")
        exp={**experiment,"status":"completed","test_ids":["T1"],"mechanism_validation":validation,
             "matrix":[{"evidence_id":"T1","original_matched":False,"background_matched":False,"target_matched":True,
                        "background_result":{"status":"ok","stdout":"0\n"},"target_result":{"status":"ok","stdout":"-2\n"},
                        "leave_one_out_results":{}}]}
        evidence={"T1":{"id":"T1","expected":"-2\n","result":{"status":"ok","stdout":"0\n"}}}
        result=root_scout.derive_causal_experiment_result(exp,evidence)
        self.assertEqual(result["status"],"tested_change_set_only")
        self.assertEqual(result["output_change_evidence_ids"],["T1"])
        self.assertEqual(result["support_by_hypothesis"],{})
        good_min=root_scout.causal_mechanism_validation(source,{**experiment,"edits":[{**experiment["edits"][0],"new_text":"best=1000000001"}]},hypotheses,oracle)
        self.assertEqual(good_min["H1"]["status"],"validated")
        good_max=root_scout.causal_mechanism_validation(source,{**experiment,"edits":[{**experiment["edits"][0],"new_text":"best=-1000000001"}]},hypotheses,{"kind":"max.v1","domain":{"value_min":-1000000000,"value_max":1000000000}})
        self.assertEqual(good_max["H1"]["status"],"validated")
        cpp_source="int best=0;\nfor (int x: values) best=std::min(best,x);"
        cpp_exp={**experiment,"edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"best=0","new_text":"best=1000000000000"}]}
        cpp_check=root_scout.causal_mechanism_validation(cpp_source,cpp_exp,hypotheses,oracle,"cpp17")
        self.assertEqual(cpp_check["H1"]["status"],"inconsistent")
        self.assertIn("target type",cpp_check["H1"]["reason"])

    def test_background_only_regression_is_risk_when_full_target_restores_pass(self):
        def result(stdout, status="ok"):
            return {"status":status,"stdout":stdout,"stdout_truncated":False}
        evidence={
            "PASS":{"id":"PASS","expected":"0\n","result":result("0\n")},
            "E1":{"id":"E1","expected":"7\n","result":result("0\n")},
        }
        rows=[]
        for eid, original, background, target in (
            ("PASS",True,"-1\n","0\n"),
            ("E1",False,"-1\n","7\n"),
        ):
            rows.append({"evidence_id":eid,"original_matched":original,
                         "background_matched":background==evidence[eid]["expected"],
                         "target_matched":target==evidence[eid]["expected"],
                         "background_result":result(background),"target_result":result(target),
                         "leave_one_out_results":{}})
        exp={"id":"X5","status":"completed","target_hypothesis_ids":["H3"],
             "background_hypothesis_ids":["H1","H2"],
             "edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"a=0"},{"hypothesis_ids":["H2"],"line":2,"old_text":"b=0"},{"hypothesis_ids":["H3"],"line":3,"old_text":"c=0"}],
             "matrix":rows,"mechanism_validation":{"H1":{"status":"validated"},"H2":{"status":"validated"},"H3":{"status":"validated"}}}
        result_v6=root_scout.derive_causal_experiment_result(exp,evidence,scope_version=6)
        self.assertEqual(result_v6["status"],"causal_support")
        self.assertEqual(result_v6["support_by_hypothesis"],{"H3":["E1"]})
        self.assertEqual(result_v6["support_evidence_ids"],["E1"])
        self.assertEqual(result_v6["background_risk_evidence_ids"],["PASS"])
        self.assertEqual(result_v6["support_kind"],"conditional")
        hypotheses=[{"id":hid,"claim":hid,"clause_ids":["C1"],"source_spans":[{"line":1,"excerpt":"x"}],"status":"open","support_records":[]} for hid in ("H1","H2","H3")]
        exp["causal_result"]=result_v6
        findings=root_scout.derive_causal_state(hypotheses,[exp],list(evidence.values()),scope_version=6)
        self.assertEqual(findings[0]["background_risk_evidence_ids"],["PASS"])
        self.assertEqual(findings[0]["claim_semantics_status"],"unverified_model_text")
        root=root_scout.causal_findings_to_roots(findings,hypotheses,[{"clause_id":"C1","status":"stated","statement_quote":"sum"}],6)[0]
        self.assertEqual(root["claim_semantics_status"],"unverified_model_text")
        self.assertIn("not semantically validate",root["claim_assessment"])
        root_scout.derive_causal_state(hypotheses,[exp],list(evidence.values()),validate_existing=True,scope_version=6)
        saved=copy.deepcopy(exp["causal_result"])
        exp["causal_result"]["background_risk_evidence_ids"]=[]
        with self.assertRaisesRegex(ValueError,"causal experiment result does not recompute"):
            root_scout.derive_causal_state(hypotheses,[exp],list(evidence.values()),validate_existing=True,scope_version=6)
        exp["causal_result"]=saved
        exp["causal_result"]["support_kind"]="finite"
        with self.assertRaisesRegex(ValueError,"causal experiment result does not recompute"):
            root_scout.derive_causal_state(hypotheses,[exp],list(evidence.values()),validate_existing=True,scope_version=6)
        exp["causal_result"]=saved
        legacy=root_scout.derive_causal_experiment_result(exp,evidence,scope_version=5)
        self.assertEqual(legacy["status"],"regressed")
        self.assertEqual(legacy["regression_evidence_ids"],["PASS"])

    def test_full_variant_regression_still_blocks_even_if_background_also_fails(self):
        evidence={"PASS":{"id":"PASS","expected":"0\n","result":{"status":"ok","stdout":"0\n"}}}
        exp={"status":"completed","target_hypothesis_ids":["H1"],"background_hypothesis_ids":["H2"],
             "edits":[{"hypothesis_ids":["H1"]},{"hypothesis_ids":["H2"]}],
             "matrix":[{"evidence_id":"PASS","original_matched":True,"background_matched":False,"target_matched":False,
                        "background_result":{"status":"ok","stdout":"-1\n"},"target_result":{"status":"ok","stdout":"-2\n"},"leave_one_out_results":{}}]}
        derived=root_scout.derive_causal_experiment_result(exp,evidence,scope_version=6)
        self.assertEqual(derived["status"],"regressed")
        self.assertEqual(derived["regression_evidence_ids"],["PASS"])

    def test_single_hypothesis_cannot_bundle_initialization_and_comparison(self):
        source="best=0\nif x>best: best=x"
        raw={"target_refs":["H1"],"background_refs":[],"edits":[
            {"hypothesis_refs":["H1"],"line":1,"old_text":"best=0","new_text":"best=999"},
            {"hypothesis_refs":["H1"],"line":2,"old_text":"x>best","new_text":"x<best"}],"non_target_clause_refs":[]}
        h={"id":"H1","clause_ids":["C1"],"source_spans":[{"line":1,"excerpt":"best=0"},{"line":2,"excerpt":"x>best"}],"granularity":"single","mechanism":"initialization and comparison"}
        with self.assertRaisesRegex(ValueError,"cannot combine distinct source edit mechanisms"):
            root_scout.normalize_causal_edits(source,raw,[h],{"C1"},{},enforce_single_mechanism=True)
        group=root_scout.normalize_causal_edits(source,raw,[{**h,"granularity":"group"}],{"C1"},{},enforce_single_mechanism=True)
        validation=root_scout.causal_mechanism_validation(source,group,[{**h,"granularity":"group"}],None)
        self.assertEqual(validation["H1"]["status"],"group")

    def test_two_width_edits_remain_one_mechanism(self):
        source="int pref=0;\nint sum=0;"
        hyp={"id":"H1","clause_ids":["C1"],"source_spans":[{"line":1,"excerpt":"int pref"},{"line":2,"excerpt":"int sum"}],"granularity":"single"}
        raw={"target_refs":["H1"],"background_refs":[],"edits":[
            {"hypothesis_refs":["H1"],"line":1,"old_text":"int","new_text":"long long"},
            {"hypothesis_refs":["H1"],"line":2,"old_text":"int","new_text":"long long"}],"non_target_clause_refs":[]}
        normalized=root_scout.normalize_causal_edits(source,raw,[hyp],{"C1"},{},enforce_single_mechanism=True)
        self.assertEqual({e["mechanism_class"] for e in normalized["edits"]},{"numeric_width"})

    def test_mechanism_parser_requires_exact_bounded_integer_expressions(self):
        hypotheses=[{"id":"H1","granularity":"single","mechanism":"initialization"}]
        oracle={"kind":"range_min.v1","domain":{"value_min":-1000000000,"value_max":1000000000}}
        def validate(source,new_text,language="python3"):
            exp={"target_hypothesis_ids":["H1"],"background_hypothesis_ids":[],"edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"best=0","new_text":new_text}]}
            return root_scout.causal_mechanism_validation(source,exp,hypotheses,oracle,language)["H1"]
        self.assertEqual(validate("best=0", "best=10**18")["status"],"validated")
        self.assertEqual(root_scout.parse_integer_expression("-1000000001LL"),-1000000001)
        self.assertEqual(root_scout.parse_integer_expression("0x3e8ULL"),1000)
        self.assertIsNone(root_scout.parse_integer_expression("1000000001LLLU"))
        arithmetic=validate("best=0", "best=1000000001-2000000000")
        self.assertEqual(arithmetic["status"],"inconsistent")
        unknown=validate("best=0", "best=1000000001-2000000000+domain_max")
        self.assertEqual(unknown["status"],"unverified")
        cpp=validate("long long best=0;", "best=1000000001", "cpp17")
        self.assertEqual(cpp["status"],"validated")
        mismatch_h=[{"id":"H1","granularity":"single","mechanism":"comparison_update"}]
        mismatch_exp={"target_hypothesis_ids":["H1"],"background_hypothesis_ids":[],"edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"best=0","new_text":"best=1000000001"}]}
        mismatch=root_scout.causal_mechanism_validation("best=0",mismatch_exp,mismatch_h,oracle)
        self.assertEqual(mismatch["H1"]["status"],"unverified")
        max_exp={"target_hypothesis_ids":["H1"],"background_hypothesis_ids":[],"edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"best=0","new_text":"best=-1000000001LL"}]}
        max_check=root_scout.causal_mechanism_validation("long long best=0;",max_exp,hypotheses,{"kind":"max.v1","domain":{"value_min":-1000000000,"value_max":1000000000}},"cpp17")
        self.assertEqual(max_check["H1"]["status"],"validated")

    def test_element_seed_is_initialization_with_compensation_limit(self):
        source="best=0\nfor x in values: best=max(best,x)"
        h=[{"id":"H1","granularity":"single","mechanism":"initialization"}]
        exp={"target_hypothesis_ids":["H1"],"background_hypothesis_ids":[],"edits":[{"hypothesis_ids":["H1"],"line":1,"old_text":"best=0","new_text":"best=a[0]"}]}
        self.assertEqual(root_scout.causal_edit_mechanism(source,1,"best=0","best=a[0]"),"initialization")
        result=root_scout.causal_mechanism_validation(source,exp,h,{"kind":"max.v1","domain":{"value_min":-10,"value_max":10}})
        self.assertEqual(result["H1"]["status"],"compensation_limited")

    def test_loop_line_context_does_not_override_modified_index_or_comparison(self):
        source="for (auto it=v.begin()+l; it!=v.end(); ++it) if (*it>best) best=*it;"
        self.assertEqual(root_scout.causal_edit_mechanism(source,1,"v.begin()+l","v.begin()+l-1"),"indexing_bounds")
        self.assertEqual(root_scout.causal_edit_mechanism(source,1,"*it>best","*it<best"),"comparison_update")
        loop="for(int i=0;i<n-1;++i) if(x>best) best=x;"
        self.assertEqual(root_scout.causal_edit_mechanism(loop,1,"i<n-1","i<n"),"iteration_count")
        self.assertEqual(root_scout.causal_edit_mechanism(loop,1,"x>best","x<best"),"comparison_update")

    def test_full_for_header_and_subspan_share_mechanism_classification(self):
        max_line="    for (int i = 1; i < n-1; ++i) {"
        max_fixed="    for (int i = 1; i < n; ++i) {"
        self.assertEqual(root_scout.causal_edit_mechanism(max_line,1,max_line,max_fixed),"iteration_count")
        sum_line="for (int qi = 1; qi < q; ++qi) {"
        sum_fixed="for (int qi = 0; qi < q; ++qi) {"
        self.assertEqual(root_scout.causal_edit_mechanism(sum_line,1,sum_line,sum_fixed),"iteration_count")
        self.assertEqual(root_scout.causal_edit_mechanism(sum_line,1,"qi = 1","qi = 0"),"iteration_count")
        mixed="for (int i = 1; i < n-1; ++i) {"
        mixed_fixed="for (int i = 0; i <= n-1; ++i) {"
        self.assertEqual(root_scout.causal_edit_mechanism(mixed,1,mixed,mixed_fixed),"unknown")

    def test_for_loop_index_mapping_is_not_count_or_comparison(self):
        source="for (int position = left; position < right; ++position) { total += values[position]; }"
        self.assertEqual(root_scout.causal_edit_mechanism(source,1,"left","left-1"),"indexing_bounds")
        self.assertEqual(root_scout.causal_edit_mechanism("a[l:r]",1,"a[l:r]","a[l-1:r]"),"indexing_bounds")
        body="for (int i=0; i<n; ++i) { if (values[i] > best) best=values[i]; }"
        self.assertEqual(root_scout.causal_edit_mechanism(body,1,"values[i] > best","values[i] < best"),"comparison_update")
        legacy="for (int i = 1; i < n-1; ++i) {"
        self.assertEqual(root_scout.causal_edit_mechanism(legacy,1,legacy,legacy.replace("n-1","n"),schema_version=4),"comparison_update")

    def test_invalid_stated_clause_is_retained_without_poisoning_valid_authority(self):
        req={"statement":"Given n values, output their maximum. 1<=n<=4.","source":"print(0)","language":"python3"}
        oracle={"statement_quote":"Given n values, output their maximum.","domain_quote":"1<=n<=4",
                "semantics":"maximum","domain":{"n_min":1,"n_max":4},"reviewed_by":"caller"}
        response={"contract":[
            {"ref":"good","clause":"maximum output","status":"stated","statement_quote":"Given n values, output their maximum."},
            {"ref":"bad","clause":"invented constraint","status":"stated","statement_quote":"n is prime"},
            {"ref":"derived","clause":"inferred loop property","status":"derived","statement_quote":""},
            {"ref":"TOOL-domain","clause":"collision","status":"stated","statement_quote":"invented domain"}]}
        contract,refs=root_scout.causal_initial_contract_check(response,req,oracle)
        self.assertEqual(refs["good"],"C1")
        self.assertIsNone(refs["bad"])
        self.assertIsNone(refs["TOOL-domain"])
        by_ref={c.get("ref"):c for c in contract if c.get("ref")}
        self.assertEqual(by_ref["bad"]["binding_rejection_reason"],"stated_quote_not_exact_request_statement_substring")
        decisions,ids=root_scout.resolve_contract_refs(contract,["good","bad","derived","unknown","TOOL-domain"],refs,"proposal")
        self.assertEqual(ids,["C1"])
        self.assertEqual([x["resolution"] for x in decisions],
                         ["accepted_stated","unverified_context","unverified_context","unknown_context","unverified_context"])
        self.assertIn("stated_quote_not_exact_request_statement_substring",decisions[1]["reason"])
        hstate={"contract":contract,"hypotheses":[],"hypothesis_decisions":[]}
        def proposal(refs):
            return {"ref":"h","clause_refs":refs,"source_spans":[{"line":1,"excerpt":"print(0)"}],
                    "claim":"wrong output","mechanism":"initialization","granularity":"single","falsifier":"a matching case"}
        accepted=root_scout.causal_add_hypotheses(hstate,[proposal(["good","bad","derived"])],refs,req)
        self.assertEqual(accepted,{"h":"H1"})
        self.assertEqual(hstate["hypotheses"][0]["clause_ids"],["C1"])
        rejected=root_scout.causal_add_hypotheses(hstate,[proposal(["bad"])],refs,req)
        self.assertEqual(rejected,{})
        self.assertIn("defect_candidate_requires_stated_or_caller_tool_authority",hstate["hypothesis_decisions"][-1]["reasons"])
        decisions_by_ref={x["ref"]:x for x in root_scout.contract_reference_decisions(contract)}
        self.assertEqual(decisions_by_ref["bad"]["resolution"],"rejected_stated_quote")

        full=self.request()
        answer={"contract":[
                    {"ref":"good","clause":"print the input value","status":"stated","statement_quote":"Print the input value."},
                    {"ref":"C1","clause":"invented range constraint","status":"stated","statement_quote":"x is at most 10"}],
                "assumptions":[],"hypotheses":[
                    {"kind":"defect_candidate","ref":"candidate","clause_refs":["good","C1"],
                     "source_spans":[{"line":1,"excerpt":"x=0"}],"claim":"the input value is ignored",
                     "mechanism":"initialization","granularity":"single","falsifier":"a trusted singleton matches"}],
                "experiments":[]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(answer)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner), \
             patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            saved=root_scout.diagnose_causal(full,strategy="one_shot")
        self.assertEqual(saved["hypotheses"][0]["clause_ids"],["C1"])
        self.assertEqual(saved["contract"][1]["source_kind"],"rejected_model_stated_quote")
        self.assertEqual(root_scout.verify_saved_case_state(saved),full)

    def test_failed_trusted_execution_cannot_be_reported_as_no_bug(self):
        req=self.request(); req["known_failing_tests"][0]["expected"]="0\n"
        answer={"contract":[],"assumptions":[],"hypotheses":[],"experiments":[]}
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(answer)
        def failed_runner(*_args,**_kwargs): return {"status":"compile_failed","stdout":"","stderr":"compile error","wall_seconds":.01}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=failed_runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_causal(req,strategy="one_shot")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")
        self.assertEqual(state["coverage"]["trusted_test_status"],"execution_blocked")
        self.assertEqual(state["coverage"]["execution_failures"][0]["profile"],"formal")
        self.assertEqual(root_scout.verify_saved_case_state(state),req)

    def test_token_admission_stops_causal_run_without_http_call(self):
        req=self.request(); answer={"contract":[],"assumptions":[],"hypotheses":[],"experiments":[]}
        with patch.dict(os.environ,{"ROOT_SCOUT_MAX_TOKENS":"1600","OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock"}), \
             patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner), \
             patch("urllib.request.urlopen",side_effect=AssertionError("must be rejected before HTTP")):
            state=root_scout.diagnose_causal(req)
        self.assertEqual(state["stop_reason"],"budget_exhausted")
        self.assertEqual(state["budget"]["calls"][0]["status"],"rejected")
        self.assertEqual(state["budget"]["calls"][0]["phase"],"HYPOTHESIS_EXPERIMENT")

    def test_failed_first_packet_and_rejected_oracle_input_snapshots_verify(self):
        req=root_scout.load_json("examples/max_oracle_demo.json")
        def runner(source,_lang,case,_env,profile="formal",build_cache=None):
            out=case.get("expected","") if "best=-1000000001" in source else "0\n"
            return {"status":"ok","stdout":out,"stderr":"","returncode":0,"matched":out==case.get("expected",""),"wall_seconds":.01}
        invalid={"contract":[{"ref":"c1","clause":"maximum output","status":"stated","statement_quote":"Given n integers, output their maximum."}],"assumptions":[],
                 "hypotheses":[{"kind":"defect_candidate","ref":"h1","clause_refs":["c1"],"source_spans":[{"line":3,"excerpt":"best=0"}],"claim":"invalid initialization","mechanism":"initial state","granularity":"single","falsifier":"a negative-only input passes unchanged"}],
                 "experiments":[{"ref":"bad-input","target_refs":["h1"],"background_refs":[],"edits":[{"hypothesis_refs":["h1"],"line":3,"old_text":"best=0","new_text":"best=-1000000001"}],"inputs":["0\n"],"non_target_clause_refs":[],"distinguishes":"negative input","rationale":"test rejected out-of-domain input"}]}
        bad_schema={"contract":[],"assumptions":[],"hypotheses":[]}
        def run(answer):
            def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(answer)
            with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}),patch.object(root_scout,"run_case",side_effect=runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
                return root_scout.diagnose_causal(req,strategy="one_shot")
        invalid_state=run(invalid)
        self.assertEqual(invalid_state["experiments"][0]["status"],"blocked")
        self.assertIn("variant_source_sha256",invalid_state["experiments"][0])
        self.assertEqual(root_scout.verify_saved_case_state(invalid_state),req)
        failed_state=run(bad_schema)
        self.assertEqual(failed_state["contract_binding_status"],"failed")
        self.assertEqual(failed_state["contract"],[])
        self.assertEqual(root_scout.verify_saved_case_state(failed_state),req)


class V2DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.req=root_scout.load_json("examples/max_oracle_demo.json")

    def test_v2_prompt_schema_is_generated_from_the_action_protocol(self):
        prompt=root_scout.system_for_phase("EXPLORE_V2",prompt_profile="baseline")
        self.assertIn('"candidate_refs"',prompt)
        self.assertIn('"run_variant"',prompt)
        self.assertIn('"variant_edits":8',prompt)
        self.assertIn('"defect_candidate"',prompt)
        self.assertIn('"example_response"',prompt)

    def test_clarification_keeps_the_cacheable_system_prefix_identical(self):
        # The system prompt is the provider cache prefix: appending a clarification
        # sentence to it makes the repair call re-read the whole conversation as an
        # uncached prompt, so the scope text travels in the message instead.
        self.assertEqual(root_scout.system_for_phase("EXPLORE_V2"),
                         root_scout.system_for_phase("EXPLORE_V2_CLARIFY"))
        prompt=json.loads(root_scout.v2_clarification_prompt('{"turn":1}',{"raw_content":"{"}))
        self.assertIn("single budgeted protocol clarification",prompt["instruction"])
        self.assertIn("do not add evidence or change claims",prompt["instruction"])
        self.assertEqual(prompt["unparsable_visible_response"],"{")
        self.assertEqual(prompt["original_turn_request"],{"turn":1})

    def test_none_low_causal_forces_low_review_with_full_source_after_initial_stop(self):
        calls=[]
        first={"candidates":[{"kind":"defect_candidate","ref":"h1","claim":"Possible initialization issue."}],"updates":[],"actions":[],"next_question":"","stop":True}
        second={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        def ask(_model,phase,prompt,budget,max_output=None,reasoning_effort_override=None,reasoning_route_reason=None,diagnostic_turn=None):
            payload=json.loads(prompt); calls.append((phase,payload,reasoning_effort_override,diagnostic_turn))
            budget.add({"phase":phase,"model":"deepseek-v4.1-flash","status":"ok","diagnostic_turn":diagnostic_turn,
                        "reasoning_policy":"none_low","reasoning_route_reason":reasoning_route_reason,
                        "reasoning_base_url":"http://127.0.0.1:8788/v1","requested_reasoning_effort":reasoning_effort_override,
                        "thinking_disabled_sent":reasoning_effort_override=="none","confirmed_usage_tokens":4,
                        "reserved_unknown_tokens":0,"usage_complete":True,"finish_reason":"stop"})
            return copy.deepcopy(first if diagnostic_turn==1 else second)
        settings={"ROOT_SCOUT_REASONING_POLICY":"none_low","OPENAI_REASONING_EFFORT":"low",
                  "OPENAI_MODEL":"deepseek-v4.1-flash","OPENAI_BASE_URL":"http://127.0.0.1:8788/v1"}
        with patch.dict(os.environ,settings,clear=True),patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_v2(self.req,strategy="causal")
        self.assertEqual(len(calls),2)
        self.assertEqual([(x[0],x[2],x[3]) for x in calls],[("EXPLORE_V2","none",1),("EXPLORE_V2","low",2)])
        self.assertEqual(calls[1][1]["source_context"]["full_source"],self.req["source"])
        self.assertTrue(any("independently at effort=low" in x for x in calls[1][1]["controller_notes"]))
        self.assertEqual(state["reasoning_routing"]["low_review_status"],"completed")
        self.assertEqual(len(state["candidates"]),1)
        self.assertEqual(state["candidates"][0]["lifecycle"],"open")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_none_clarification_cannot_count_as_low_review_and_invalid_accept_is_incomplete(self):
        good={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        for low_mode in ("parse_failure_then_clarify","parse_failure_unclarified","invalid_schema"):
            with self.subTest(low_mode=low_mode):
                calls=[]
                def ask(_model,phase,prompt,budget,max_output=None,reasoning_effort_override=None,reasoning_route_reason=None,diagnostic_turn=None):
                    calls.append((phase,diagnostic_turn))
                    record={"phase":phase,"model":"deepseek-v4.1-flash","diagnostic_turn":diagnostic_turn,
                            "reasoning_policy":"none_low","reasoning_route_reason":reasoning_route_reason,
                            "reasoning_base_url":"http://127.0.0.1:8788/v1","requested_reasoning_effort":reasoning_effort_override,
                            "thinking_disabled_sent":reasoning_effort_override=="none","confirmed_usage_tokens":3,
                            "reserved_unknown_tokens":0,"usage_complete":True,"finish_reason":"stop"}
                    if phase=="EXPLORE_V2" and diagnostic_turn==2 and low_mode.startswith("parse_failure"):
                        raw="" if low_mode=="parse_failure_unclarified" else "{partial"
                        record.update(status="failed",error="JSONDecodeError",error_class="response_parse_failed",
                                      raw_content=raw,raw_chars=len(raw),visible_response_sha256=root_scout.sha(raw))
                        budget.add(record)
                        raise json.JSONDecodeError("partial JSON",raw,len(raw))
                    record["status"]="ok"
                    budget.add(record)
                    if phase=="EXPLORE_V2" and diagnostic_turn==2 and low_mode=="invalid_schema":
                        return {"candidates":[],"updates":[],"actions":[],"next_question":17,"stop":True}
                    return copy.deepcopy(good)
                with patch.dict(os.environ,{"ROOT_SCOUT_REASONING_POLICY":"none_low","OPENAI_REASONING_EFFORT":"low",
                                            "OPENAI_MODEL":"deepseek-v4.1-flash","OPENAI_BASE_URL":"http://127.0.0.1:8788/v1"},clear=True), \
                     patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
                     patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
                    state=root_scout.diagnose_v2(self.req,strategy="causal")
                self.assertEqual(state["reasoning_routing"]["low_review_status"],"failed")
                self.assertEqual(state["stop_reason"],"model_protocol_incomplete")
                self.assertNotEqual(state["stop_reason"],"no_bug_found_within_scope")
                self.assertEqual(root_scout.verify_saved_case_state(state),state)
                if low_mode=="parse_failure_then_clarify":
                    self.assertEqual(calls,[ ("EXPLORE_V2",1),("EXPLORE_V2",2),("EXPLORE_V2_CLARIFY",2) ])
                    self.assertEqual(state["protocol_clarifications"][0]["status"],"recovered")
                elif low_mode=="parse_failure_unclarified":
                    self.assertEqual(calls,[ ("EXPLORE_V2",1),("EXPLORE_V2",2) ])
                    self.assertEqual(state["protocol_clarifications"],[])
                else:
                    self.assertEqual(calls,[ ("EXPLORE_V2",1),("EXPLORE_V2",2) ])

    def test_none_low_one_shot_does_not_reroute_or_add_a_call(self):
        calls=[]; response={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        def ask(_model,phase,prompt,budget,max_output=None):
            calls.append(phase); budget.add({"phase":phase,"model":"fixture","status":"ok","confirmed_usage_tokens":1,"reserved_unknown_tokens":0,"usage_complete":True})
            return copy.deepcopy(response)
        with patch.dict(os.environ,{"ROOT_SCOUT_REASONING_POLICY":"none_low","OPENAI_REASONING_EFFORT":"low"},clear=True), \
             patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_v2(self.req,strategy="one_shot")
        self.assertEqual(calls,["EXPLORE_V2"])
        self.assertFalse(state["reasoning_routing"]["applied"])
        self.assertEqual(state["reasoning_routing"]["low_review_status"],"not_applied")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_none_low_budget_rejection_keeps_case_unresolved_and_does_not_send_second_request(self):
        calls=[]; response={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        def ask(_model,phase,prompt,budget,max_output=None,reasoning_effort_override=None,reasoning_route_reason=None,diagnostic_turn=None):
            calls.append(diagnostic_turn)
            if diagnostic_turn==1:
                budget.add({"phase":phase,"model":"deepseek-v4.1-flash","status":"ok","diagnostic_turn":1,
                            "reasoning_policy":"none_low","reasoning_route_reason":reasoning_route_reason,
                            "reasoning_base_url":"http://127.0.0.1:8788/v1","requested_reasoning_effort":"none",
                            "thinking_disabled_sent":True,"confirmed_usage_tokens":95000,"reserved_unknown_tokens":0,
                            "usage_complete":True,"finish_reason":"stop"})
                return copy.deepcopy(response)
            budget.add({"phase":phase,"model":"deepseek-v4.1-flash","status":"rejected","error":"token_budget_exhausted_before_call_reserved_report",
                        "diagnostic_turn":2,"reasoning_policy":"none_low","reasoning_route_reason":reasoning_route_reason,
                        "reasoning_base_url":"http://127.0.0.1:8788/v1","requested_reasoning_effort":"low",
                        "thinking_disabled_sent":False,"confirmed_usage_tokens":0,"reserved_unknown_tokens":0,"usage_complete":True})
            raise root_scout.BudgetExhausted("token_budget_exhausted_before_call_reserved_report")
        with patch.dict(os.environ,{"ROOT_SCOUT_REASONING_POLICY":"none_low","OPENAI_REASONING_EFFORT":"low"},clear=True), \
             patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose_v2(self.req,strategy="causal")
        self.assertEqual(calls,[1,2])
        self.assertEqual(state["stop_reason"],"budget_exhausted")
        self.assertEqual(state["reasoning_routing"]["low_review_status"],"skipped_budget")
        self.assertNotEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_none_and_low_call_overrides_are_scoped_and_provider_gated(self):
        class Response:
            def __init__(self,payload): self.payload=payload
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit): return self.payload
        answer=json.dumps({"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True})
        body_parts=[
            {"choices":[{"delta":{"content":answer}}]},
            {"choices":[{"delta":{},"finish_reason":"stop"}]},
            {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4,"completion_tokens_details":{"reasoning_tokens":0}}},
        ]
        wire="".join("data: "+json.dumps(x)+"\n\n" for x in body_parts)+"data: [DONE]\n\n"
        requests=[]
        def open_request(req,timeout=None): requests.append(json.loads(req.data)); return Response(wire.encode())
        settings={"ROOT_SCOUT_REASONING_POLICY":"none_low","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","OPENAI_API_KEY":"mock","OPENAI_MODEL":"deepseek-v4.1-flash",
                  "OPENAI_BASE_URL":"http://127.0.0.1:8788/v1","OPENAI_REASONING_EFFORT":"low"}
        before=os.environ.get("OPENAI_REASONING_EFFORT")
        with patch.dict(os.environ,settings,clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=open_request):
            budget=root_scout.Budget(max_tokens=48000)
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget,reasoning_effort_override="none",reasoning_route_reason="initial",diagnostic_turn=1)
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget,reasoning_effort_override="low",reasoning_route_reason="review",diagnostic_turn=2)
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2_CLARIFY","{}",budget,reasoning_effort_override="none",reasoning_route_reason="clarification",diagnostic_turn=2)
        self.assertEqual(requests[0]["reasoning_effort"],"none")
        self.assertEqual(requests[0]["thinking"],{"type":"disabled"})
        self.assertEqual(requests[1]["reasoning_effort"],"low")
        self.assertNotIn("thinking",requests[1])
        self.assertEqual(requests[2]["reasoning_effort"],"none")
        self.assertEqual(requests[2]["thinking"],{"type":"disabled"})
        self.assertEqual([x["requested_reasoning_effort"] for x in budget.calls],["none","low","none"])
        self.assertEqual(budget.calls[0]["reasoning_tokens"],0)
        self.assertTrue(all("reasoning_content" not in x for x in budget.calls))
        self.assertEqual(os.environ.get("OPENAI_REASONING_EFFORT"),before)

    @staticmethod
    def runner(source,_language,case,_env,profile="formal",build_cache=None):
        out="-7\n" if "best=-1000000001" in source else "0\n"
        return {"status":"ok","stdout":out,"stderr":"","returncode":0,"wall_seconds":0.01,"profile":profile}

    def run_v2(self,req,response,runner=None):
        def ask(_model,_phase,_prompt,_budget,max_output=None): return copy.deepcopy(response)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
             patch.object(root_scout,"run_case",side_effect=runner or self.runner), \
             patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            return root_scout.diagnose(req,strategy="one_shot")

    def run_v2_packets(self,req,packets,runner=None):
        calls=[]
        def ask(_model,_phase,_prompt,_budget,max_output=None):
            value=copy.deepcopy(packets[len(calls)]); calls.append(value); return value
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
             patch.object(root_scout,"run_case",side_effect=runner or self.runner), \
             patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose(req,strategy="causal")
        return state,calls

    def test_v2_dynamic_oracle_recomputes_negative_singleton_and_replay_integrity(self):
        response={"candidates":[{"ref":"init","claim":"best starts above all-negative inputs","expected":"0"}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"fix","parent_version_id":"original","candidate_refs":["init"],"edits":[{"start_line":3,"end_line":3,"old_text":"best=0","new_text":"best=-1000000001"}],"input_refs":[],"inputs":["1\n-7"],"question":"Compare the sentinel variant on a negative singleton."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        pair=[e for e in state["evidence"] if e["input"]=="1\n-7"]
        self.assertEqual(len(pair),2)
        self.assertEqual({e["expected"] for e in pair},{"-7\n"})
        self.assertTrue(all(e["trusted_expected"] for e in pair))
        original=next(e for e in pair if e["version_id"]=="original")
        variant=next(e for e in pair if e["version_id"]!="original")
        self.assertIs(original["result"]["matched"],False)
        self.assertIs(variant["result"]["matched"],True)
        self.assertEqual(root_scout.verify_saved_case_state(state),state)
        forged=copy.deepcopy(state); target=next(e for e in forged["evidence"] if e["input"]=="1\n-7" and e["version_id"]=="original")
        target["expected"]="-8\n"; target["expected_sha256"]=root_scout.sha(target["expected"])
        forged["integrity"]=root_scout.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"trusted expected does not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_v2_model_expected_and_missing_oracle_never_create_matched_result(self):
        req=copy.deepcopy(self.req); req.pop("oracle_tool_contract")
        response={"candidates":[{"ref":"x","claim":"possibly wrong","expected":"999999"}],"updates":[],
                  "actions":[{"kind":"run_original","ref":"run","input":"1\n-7","question":"Observe output."}],"next_question":"","stop":True}
        state=self.run_v2(req,response)
        evidence=next(e for e in state["evidence"] if e["input"]=="1\n-7")
        self.assertFalse(evidence["trusted_expected"])
        self.assertNotIn("expected",evidence)
        self.assertIsNone(evidence["result"]["matched"])
        self.assertEqual(state["roots"],[])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_invalid_action_does_not_drop_candidate_and_reports_no_roots(self):
        response={"candidates":[{"ref":"candidate","claim":"There may be an initialization issue."}],"updates":[],
                  "actions":[{"kind":"run_original","ref":"bad","input":"1\n-7","question":"run","expected":"-7\n"}],"next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(len(state["candidates"]),1)
        self.assertEqual(state["tool_actions"],[])
        self.assertTrue(any("action fields must exactly match protocol" in x["reason"] for x in state["protocol_issues"]))
        self.assertEqual(state["roots"],[])
        report=root_scout.markdown(state)
        self.assertIn("未作语义验证",report)
        self.assertIn("C_raw / C_store / C_deliver",report)
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_missing_action_alias_is_filled_instead_of_costing_a_clarification(self):
        response={"candidates":[{"ref":"candidate","claim":"There may be an initialization issue."}],"updates":[],
                  "actions":[{"kind":"run_original","input":"1\n-7","question":"Observe the negative singleton."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(state["protocol_issues"],[])
        action=state["tool_actions"][0]
        self.assertEqual(action["status"],"accepted",action.get("blocked_reason"))
        self.assertEqual(action["proposal"]["ref"],"auto-ref-1")
        self.assertEqual([r["part"] for r in state["response_repairs"]],["actions[0].ref"])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_missing_action_question_is_filled_with_an_explicit_placeholder(self):
        response={"candidates":[{"ref":"candidate","claim":"There may be an initialization issue."}],"updates":[],
                  "actions":[{"kind":"run_original","ref":"run","input":"1\n-7"}],"next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(state["protocol_issues"],[])
        self.assertEqual(state["tool_actions"][0]["proposal"]["question"],"(model omitted this field)")
        self.assertEqual([r["part"] for r in state["response_repairs"]],["actions[0].question"])

    def test_v2_missing_disposition_reason_is_filled_instead_of_rejecting_the_update(self):
        response={"candidates":[{"ref":"candidate","claim":"There may be an initialization issue."}],"updates":[
                      {"candidate_id":"C0001","disposition":"withdraw"}],
                  "actions":[],"next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(state["protocol_issues"],[])
        self.assertEqual(state["candidates"][0]["lifecycle"],"withdrawn")
        self.assertEqual(state["candidate_update_decisions"][0]["proposal"]["reason"],"(model omitted this field)")
        self.assertEqual([r["part"] for r in state["response_repairs"]],["updates[0].reason"])

    def test_v2_restated_claim_is_merged_not_counted_twice(self):
        first={"candidates":[{"kind":"defect_candidate","ref":"a","claim":"The seed may break all-negative input.","locations":["line 3"]}],
               "updates":[],"actions":[],"next_question":"Check the seed range.","stop":False}
        second={"candidates":[{"kind":"defect_candidate","ref":"b","claim":"The   seed may break all-negative input.","locations":["line 3"]}],
                "updates":[],"actions":[],"next_question":"","stop":True}
        state,_=self.run_v2_packets(self.req,[first,second])
        self.assertEqual([c["lifecycle"] for c in state["candidates"]],["open","merged"])
        self.assertEqual(state["candidates"][1]["merged_into_candidate_id"],"C0001")
        self.assertEqual(state["candidates"][1]["revisions"][0]["disposition"],"merge")
        self.assertEqual(state["localization"]["status"],"located_with_line")
        self.assertEqual(state["localization"]["primary_candidate_id"],"C0001")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_site_check_grades_the_real_source(self):
        source="a=1\nbest=0\ncin >> n;\n\n}\n"
        candidate=lambda locations: {"source_location_claims":locations}
        self.assertEqual(root_scout.v2_site_check(candidate([]),source),"no_line")
        self.assertEqual(root_scout.v2_site_check(candidate(["line 2"]),source),"ok")
        self.assertEqual(root_scout.v2_site_check(candidate(["lines 1-2"]),source),"ok")
        self.assertEqual(root_scout.v2_site_check(candidate(["line 3"]),source),"weak_site")
        self.assertEqual(root_scout.v2_site_check(candidate(["line 5"]),source),"weak_site")
        self.assertEqual(root_scout.v2_site_check(candidate(["line 99"]),source),"out_of_range")
        self.assertEqual(root_scout.v2_location_spans(["lines 20-25","line 3","see line 7"]),[(20,25),(3,3),(7,7)])

    def test_v2_site_check_flags_a_function_wide_range_as_coarse(self):
        source="\n".join(f"x{i} = {i};" for i in range(1,60))
        candidate=lambda locations: {"source_location_claims":locations}
        limit=root_scout.V2_SITE_MAX_LINES
        self.assertEqual(root_scout.v2_site_check(candidate([f"lines 1-{limit}"]),source),"ok")
        self.assertEqual(root_scout.v2_site_check(candidate([f"lines 1-{limit+1}"]),source),"coarse_site")
        # one precise span next to a coarse one is still a usable pointer
        self.assertEqual(root_scout.v2_site_check(candidate(["lines 1-40","lines 7-7"]),source),"ok")
        coarse={"candidates":[{"id":"C0001","lifecycle":"open","candidate_kind":"defect_candidate","current_claim":"x",
                               "current_evidence_ids":[],"source_location_claims":["lines 1-40"]}],
                "evidence":[],"tool_actions":[],"request_snapshot":{"source":source}}
        self.assertEqual(root_scout.v2_localization(coarse)["status"],"located_coarse_line")

    def test_v2_localization_states_what_a_reader_gets(self):
        empty={"candidates":[],"evidence":[],"tool_actions":[],"request_snapshot":{"source":"a=1\n"}}
        self.assertEqual(root_scout.v2_localization(empty)["status"],"no_finding")
        without_line={"candidates":[{"id":"C0001","lifecycle":"open","candidate_kind":"defect_candidate","current_claim":"x","current_evidence_ids":[],
                                     "source_location_claims":["the type-1 handler"]}],
                      "evidence":[],"tool_actions":[],"request_snapshot":{"source":"a=1\nb=2\n"}}
        self.assertEqual(root_scout.v2_localization(without_line)["status"],"located_without_line")
        self.assertEqual(root_scout.v2_localization(without_line)["sites"][0]["site_check"],"no_line")

    def test_v2_quoted_location_resolves_without_counting_lines(self):
        source="int main() {\n  int best = 0;\n  for (int i = 1; i < n - 1; i++) {\n    best = min(best, a[i]);\n  }\n}\n"
        quoted={"source_location_claims":["`for (int i = 1; i < n - 1; i++) {`"]}
        self.assertEqual(root_scout.v2_quote_span(source,"for (int i = 1; i < n - 1; i++) {"),((3,3),1))
        self.assertEqual(root_scout.v2_site_check(quoted,source),"ok")
        located=root_scout.v2_resolve_locations(quoted["source_location_claims"],source)
        self.assertEqual(located["spans"],[(3,3)])
        self.assertEqual(located["quotes_resolved"],["for (int i = 1; i < n - 1; i++) {"])
        multi={"source_location_claims":["`for (int i = 1; i < n - 1; i++) {\n    best = min(best, a[i]);`"]}
        self.assertEqual(root_scout.v2_resolve_locations(multi["source_location_claims"],source)["spans"],[(3,4)])

    def test_v2_ambiguous_or_missing_quote_is_not_a_location(self):
        source="a(1);\nb(2);\na(1);\n"
        self.assertEqual(root_scout.v2_quote_span(source,"a(1);"),(None,2))
        self.assertEqual(root_scout.v2_quote_span(source,"zzz();"),(None,0))
        ambiguous={"source_location_claims":["`a(1);`"]}
        self.assertEqual(root_scout.v2_site_check(ambiguous,source),"ambiguous_quote")
        self.assertEqual(root_scout.v2_site_check({"source_location_claims":["`zzz();`"]},source),"no_line")
        # an explicit line range nearby disambiguates the repeated quote
        both={"source_location_claims":["line 3","`a(1);`"]}
        self.assertEqual(root_scout.v2_resolve_locations(both["source_location_claims"],source)["spans"],[(3,3),(3,3)])
        away={"source_location_claims":["line 1","`a(1);`"]}
        self.assertEqual(root_scout.v2_resolve_locations(away["source_location_claims"],source)["spans"],[(1,1),(1,1)])

    def test_v2_prompt_asks_for_a_line_range_and_for_merging_restatements(self):
        text=root_scout.system_for_phase("EXPLORE_V2",prompt_profile="baseline")
        self.assertIn("must be locatable in the current source",text)
        self.assertIn("quoting the exact source text in backticks",text)
        self.assertIn("give the shortest extent that still covers the change",text)
        self.assertIn("merge a later restatement of a site you already hold",text)
        self.assertIn("Keep the first response short",text)
        # the claim that quoting is never more expensive was measured false and removed
        self.assertNotIn("never more expensive",text)

    def test_v2_missing_action_kind_is_inferred_when_one_spec_fits(self):
        response={"candidates":[{"ref":"candidate","claim":"There may be an initialization issue."}],"updates":[],
                  "actions":[{"ref":"run","input":"1\n-7","question":"Observe the negative singleton."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(state["protocol_issues"],[])
        action=state["tool_actions"][0]
        self.assertEqual((action["kind"],action["status"]),("run_original","accepted"))
        self.assertEqual([r["part"] for r in state["response_repairs"]],["actions[0].kind"])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_action_kind_inference_declines_on_ambiguity_and_never_overrides_a_given_kind(self):
        specs=root_scout.v2_protocol_spec(5)["actions"]
        self.assertEqual(root_scout.v2_infer_action_kind({"ref":"r","question":"q","command":"echo 1"},specs),"run_bash")
        self.assertEqual(root_scout.v2_infer_action_kind({"ref":"r","question":"q","line_start":1,"line_end":2},specs),"read_source")
        self.assertIsNone(root_scout.v2_infer_action_kind({"ref":"r","question":"q"},specs),"no payload field fits exactly one spec")
        self.assertIsNone(root_scout.v2_infer_action_kind({"kind":"run_bash","ref":"r","question":"q","input":"1"},specs),"a stated kind is never overridden")
        self.assertIsNone(root_scout.v2_infer_action_kind({"kind":[],"ref":"r","question":"q"},specs))

    def test_v2_single_arguments_envelope_is_unwrapped(self):
        inner={"candidates":[{"ref":"candidate","claim":"There may be an initialization issue."}],"updates":[],
               "actions":[],"next_question":"","stop":True}
        fixed,repairs=root_scout.v2_response_repairs({"arguments":json.dumps(inner)},5)
        self.assertEqual(fixed,inner)
        self.assertEqual([r["reason"] for r in repairs],["unwrapped a single arguments envelope"])
        self.assertEqual(root_scout.validate_v2_response(fixed,5)["issues"],[])

    def test_v2_missing_action_kind_bad_command_or_extra_key_still_rejects(self):
        cases=[({"ref":"run","question":"Observe."},"unknown action kind"),
               ({"kind":"run_original","ref":"run","question":"Observe."},"action fields must exactly match protocol"),
               ({"kind":"run_original","ref":"run","input":"1\n-7","question":"Observe.","expected":"-7\n"},"action fields must exactly match protocol")]
        for action,reason in cases:
            with self.subTest(action=action):
                response={"candidates":[{"kind":"defect_candidate","ref":"candidate","claim":"There may be an initialization issue."}],
                          "updates":[],"actions":[action],"next_question":"","stop":True}
                state=self.run_v2(self.req,response)
                self.assertEqual(state["tool_actions"],[])
                self.assertTrue(any(reason in x["reason"] for x in state["protocol_issues"]),state["protocol_issues"])

    def test_v2_malformed_action_kind_never_discards_readable_candidate(self):
        for bad_kind in ([],{},None,42):
            response={"candidates":[{"kind":"defect_candidate","ref":"candidate","claim":"There may be a boundary defect."}],"updates":[],
                      "actions":[{"kind":bad_kind,"ref":"bad","question":"probe"}],"next_question":"","stop":True}
            with self.subTest(kind=bad_kind):
                state=self.run_v2(self.req,response)
                self.assertEqual(len(state["candidates"]),1)
                self.assertEqual(state["candidates"][0]["original_claim"],"There may be a boundary defect.")
                self.assertTrue(state["protocol_issues"])

    def test_v2_positive_coverage_claim_is_finite_not_open_bug(self):
        response={"candidates":[{"kind":"correctness_coverage","ref":"coverage","claim":"The submitted program appears to handle the tested values."}],
                  "updates":[],"actions":[],"next_question":"","stop":True}
        def passing(source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":case.get("expected",""),"stderr":"","returncode":0,"wall_seconds":0.01,"profile":profile}
        state=self.run_v2(self.req,response,passing)
        self.assertEqual(state["candidates"][0]["validation_summary"]["status"],"finite_tests_passed")
        self.assertEqual(state["coverage"]["unresolved_candidate_ids"],[])
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_parse_failure_keeps_full_visible_response_in_call_ledger(self):
        visible='prefix {"candidates":[{"claim":"unfinished'
        payload='data: '+json.dumps({"choices":[{"delta":{"content":visible}}]})+'\n\n'
        payload+='data: '+json.dumps({"choices":[{"delta":{},"finish_reason":"stop"}]})+'\n\n'
        payload+='data: '+json.dumps({"choices":[],"usage":{"prompt_tokens":31,"completion_tokens":7}})+'\n\n'
        payload+='data: [DONE]\n\n'
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit): return payload.encode("utf-8")
        budget=root_scout.Budget(max_tokens=48000)
        with patch.dict(os.environ,{"OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock","OPENAI_BASE_URL":"http://127.0.0.1:1/v1","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"}),patch("urllib.request.urlopen",return_value=Response()):
            with self.assertRaises(json.JSONDecodeError): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
        call=budget.calls[0]
        self.assertEqual(call["raw_content"],visible)
        self.assertFalse(call["raw_content_truncated"])
        self.assertEqual(call["visible_response_sha256"],root_scout.sha(visible))
        state={"budget":{"calls":budget.calls},"raw_candidates":[],"candidate_sets":{"C_raw":[]},"candidate_intake_failures":[]}
        root_scout.v2_preserve_failed_response(state,1,json.JSONDecodeError("bad","",0))
        self.assertEqual(state["candidate_sets"]["C_raw"],["CR0001"])
        self.assertEqual(state["raw_candidates"][0]["proposal"],visible)

    def test_v2_sse_stream_handles_usage_only_multiline_and_utf8_chunks(self):
        visible=json.dumps({"candidates":[{"claim":"值域边界可能有误"}],"updates":[],"actions":[],"next_question":"","stop":True},ensure_ascii=False)
        events=[
            {"choices":[{"delta":{"content":visible[:20],"reasoning_content":"do not store"}}]},
            {"choices":[{"delta":{"content":visible[20:]}}]},
            {"choices":[{"delta":{},"finish_reason":"stop"}]},
        ]
        wire="".join("data: "+json.dumps(x,ensure_ascii=False)+"\r\n\r\n" for x in events)
        wire+='data: {"choices":[],"usage":{"prompt_tokens":30,\r\n'
        wire+='data: "completion_tokens":12001,"completion_tokens_details":{"reasoning_tokens":11900}}}\r\n\r\n'
        wire+="data: [DONE]\r\n\r\n"
        encoded=wire.encode("utf-8")
        split=encoded.index("值".encode("utf-8"))+1
        chunks=[encoded[:split],encoded[split:split+17],encoded[split+17:]]
        class Response:
            def __init__(self): self.chunks=list(chunks)
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit): return self.chunks.pop(0) if self.chunks else b""
        budget=root_scout.Budget(max_tokens=48000); requests=[]
        def mock_urlopen(req,**_kwargs): requests.append(json.loads(req.data)); return Response()
        with patch.dict(os.environ,{"OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_OUTPUT_POLICY":"fixed"},clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=mock_urlopen):
            answer=root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
        self.assertEqual(answer["candidates"][0]["claim"],"值域边界可能有误")
        body=requests[0]
        self.assertTrue(body["stream"]); self.assertEqual(body["stream_options"],{"include_usage":True})
        self.assertEqual(body["max_completion_tokens"],20000)
        call=budget.calls[0]
        self.assertEqual(call["finish_reason"],"stop")
        self.assertTrue(call["stream_metadata"]["done"])
        self.assertTrue(call["stream_metadata"]["response_complete"])
        self.assertEqual(call["stream_metadata"]["termination_basis"],"done_and_finish_stop")
        self.assertEqual(call["stream_metadata"]["events"],4)
        self.assertTrue(call["output_threshold_reached"])
        self.assertEqual(call["reasoning_tokens"],11900)
        self.assertNotIn("reasoning_content",call["raw_content"])
        self.assertNotIn("do not store",json.dumps(call,ensure_ascii=False))
        self.assertEqual(call["raw_content"],visible)
        self.assertTrue(call["usage_complete"])

    def test_v2_stream_defaults_to_20k_with_12k_usage_observation_threshold(self):
        with patch.dict(os.environ,{},clear=True):
            cfg=root_scout.model_budget_configuration()
        self.assertEqual(cfg["output_policy"],"adaptive")
        self.assertIsNone(cfg["phase_output_tokens"]["EXPLORE_V2"])
        self.assertIsNone(cfg["phase_output_tokens"]["EXPLORE_V2_CLARIFY"])
        self.assertEqual(cfg["stream_output_observation_threshold_tokens"],12000)
        with patch.dict(os.environ,{"ROOT_SCOUT_MAX_TOKENS":"96000","ROOT_SCOUT_MAX_OUTPUT_TOKENS":"14000"},clear=True):
            cfg=root_scout.model_budget_configuration()
        self.assertEqual(cfg["phase_output_tokens"]["EXPLORE_V2"],14000)
        with patch.dict(os.environ,{"ROOT_SCOUT_MAX_TOKENS":"96000","ROOT_SCOUT_MAX_OUTPUT_TOKENS_CONTRACT_PLAN":"13000"},clear=True):
            cfg=root_scout.model_budget_configuration()
        self.assertEqual(cfg["phase_output_tokens"]["EXPLORE_V2"],13000)

    def test_adaptive_output_recomputes_from_usage_and_keeps_utf8_budget_reservation(self):
        answer={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        visible=json.dumps(answer)
        events=[
            {"choices":[{"delta":{"content":visible}}]},
            {"choices":[{"delta":{},"finish_reason":"stop"}]},
            {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":200}},
        ]
        wire="".join("data: "+json.dumps(x)+"\n\n" for x in events)+"data: [DONE]\n\n"
        class Response:
            def __init__(self): self.sent=False
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                if self.sent: return b""
                self.sent=True; return wire.encode()
        bodies=[]
        def open_request(req,timeout=None):
            bodies.append(json.loads(req.data)); return Response()
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"96000",
             "ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","OPENAI_API_KEY":"test-only",
             "OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1","OPENAI_REASONING_EFFORT":"low"}
        budget=root_scout.Budget(max_tokens=96000,max_wall_seconds=600)
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=open_request):
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short request",budget)
            used_after_first=budget.total()
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short request",budget)
        self.assertEqual(len(bodies),2)
        caps=[body["max_completion_tokens"] for body in bodies]
        self.assertTrue(16000<caps[0]<128000)
        self.assertLess(caps[1],caps[0])  # previous confirmed usage reduces the next admitted cap
        for i,(body,call) in enumerate(zip(bodies,budget.calls)):
            raw=json.dumps(body,ensure_ascii=False).encode()
            self.assertEqual(call["input_reservation"],len(raw))
            self.assertEqual(call["max_output_reserved"],body["max_completion_tokens"])
            prior=0 if i==0 else used_after_first
            self.assertLessEqual(prior+len(raw)+body["max_completion_tokens"]+root_scout.REPORT_TOKEN_RESERVE,96000)
        self.assertEqual(budget.confirmed_total(),600)
        self.assertEqual(budget.unknown_total(),0)

    def test_adaptive_unknown_usage_consumes_reservation_and_rejects_without_second_http(self):
        class Response:
            def __init__(self): self.n=0
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                self.n+=1
                if self.n==1: return b": heartbeat\n\n"
                raise TimeoutError("injected stream failure")
        calls=[]
        def open_request(req,timeout=None): calls.append(req); return Response()
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"20000",
             "ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","OPENAI_API_KEY":"test-only",
             "OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}
        budget=root_scout.Budget(max_tokens=20000)
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=open_request):
            with self.assertRaises(TimeoutError): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short request",budget)
            self.assertGreaterEqual(budget.total()+root_scout.REPORT_TOKEN_RESERVE,19900)
            with self.assertRaises(root_scout.BudgetExhausted): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short request",budget)
        self.assertEqual(len(calls),1)
        self.assertEqual([x["status"] for x in budget.calls], ["failed","rejected"])
        self.assertFalse(budget.calls[0]["usage_complete"])
        self.assertGreater(budget.calls[0]["reserved_unknown_tokens"],16000)

    def test_adaptive_explicit_caps_and_budget_admission_are_hard(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                wire="data: "+json.dumps({"choices":[{"delta":{"content":"{}"}}]})+"\n\n"
                wire+="data: "+json.dumps({"choices":[{"delta":{},"finish_reason":"stop"}]})+"\n\n"
                wire+="data: "+json.dumps({"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}})+"\n\n"
                return (wire+"data: [DONE]\n\n").encode()
        bodies=[]
        def open_request(req,timeout=None): bodies.append(json.loads(req.data));return Response()
        settings={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"48000",
                  "ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","ROOT_SCOUT_MAX_OUTPUT_TOKENS_EXPLORE_V2":"7000",
                  "OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}
        with patch.dict(os.environ,settings,clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=open_request):
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short",root_scout.Budget(max_tokens=48000),max_output=3000)
        self.assertEqual(bodies[0]["max_completion_tokens"],3000)
        blocked=root_scout.Budget(max_tokens=2000)
        with patch.dict(os.environ,{k:v for k,v in settings.items() if k not in ("ROOT_SCOUT_MAX_OUTPUT_TOKENS_EXPLORE_V2",)},clear=True),patch.object(root_scout.urllib.request,"urlopen") as http:
            with self.assertRaises(root_scout.BudgetExhausted): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","x"*1000,blocked)
        http.assert_not_called()
        self.assertEqual(blocked.calls[0]["status"],"rejected")
        self.assertEqual(blocked.calls[0]["error"],"token_budget_exhausted_before_call_reserved_report")

    def test_adaptive_global_cap_is_upper_bound_without_phase_override(self):
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"96000",
             "ROOT_SCOUT_MAX_OUTPUT_TOKENS":"60000","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"}
        with patch.dict(os.environ,env,clear=True):
            cfg=root_scout.model_budget_configuration()
        self.assertEqual(cfg["phase_output_tokens"]["EXPLORE_V2"],60000)
        self.assertEqual(cfg["phase_output_tokens"]["EXPLORE_V2_CLARIFY"],60000)

    def test_adaptive_rejects_too_small_explicit_caller_cap_without_http(self):
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"48000",
             "ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","OPENAI_API_KEY":"test-only",
             "OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}
        for cap in (0,15):
            with self.subTest(cap=cap),patch.dict(os.environ,env,clear=True), \
                 patch.object(root_scout.urllib.request,"urlopen") as http:
                with self.assertRaisesRegex(ValueError,"max_output must be at least 16"):
                    root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",root_scout.Budget(),max_output=cap)
                http.assert_not_called()

    def test_adaptive_cap_digit_boundary_uses_monotone_utf8_reservation(self):
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000",
             "ROOT_SCOUT_MAX_TOKENS":"200000","OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture",
             "OPENAI_BASE_URL":"http://mock/v1","OPENAI_REASONING_EFFORT":"low"}
        with patch.dict(os.environ,env,clear=True):
            body={"model":"fixture","messages":[{"role":"system","content":root_scout.system_for_phase("EXPLORE_V2")},
                {"role":"user","content":"{}"}],"max_completion_tokens":128000,
                "response_format":{"type":"json_object"},"stream":True,"stream_options":{"include_usage":True},"reasoning_effort":"low"}
            request_bytes=len(json.dumps(body,ensure_ascii=False).encode())
        total=root_scout.REPORT_TOKEN_RESERVE+request_bytes+99999
        env["ROOT_SCOUT_MAX_TOKENS"]=str(total)
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                parts=[{"choices":[{"delta":{"content":"{}"}}]},
                       {"choices":[{"delta":{},"finish_reason":"stop"}]},
                       {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}]
                wire="".join("data: "+json.dumps(x)+"\n\n" for x in parts)+"data: [DONE]\n\n"
                return wire.encode()
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            budget=root_scout.Budget(max_tokens=total)
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
        call=budget.calls[0]
        self.assertEqual(call["max_output_reserved"],99999)
        self.assertEqual(call["request_bytes"],call["input_reservation"])
        self.assertLessEqual(call["input_reservation"]+call["max_output_reserved"]+root_scout.REPORT_TOKEN_RESERVE,total)

    def test_adaptive_wall_budget_default_and_explicit_600_seconds(self):
        with patch.dict(os.environ,{},clear=True): cfg=root_scout.model_budget_configuration()
        self.assertEqual(cfg["max_wall_seconds"],180)
        with patch.dict(os.environ,{"ROOT_SCOUT_MAX_WALL_SECONDS":"600"},clear=True):
            cfg=root_scout.model_budget_configuration(); budget=root_scout.Budget(started=100,max_wall_seconds=cfg["max_wall_seconds"])
        with patch.object(root_scout.time,"monotonic",return_value=100): self.assertEqual(budget.remaining_wall(),600)
        with patch.object(root_scout.time,"monotonic",return_value=100.5): self.assertEqual(budget.remaining_wall(),599.5)

    def test_adaptive_configuration_and_launcher_do_not_inject_fixed_global_cap(self):
        with patch.dict(os.environ,{"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"96000","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"},clear=True):
            cfg=root_scout.model_budget_configuration()
        self.assertEqual(cfg["output_policy"],"adaptive")
        self.assertEqual(cfg["phase_output_tokens"]["EXPLORE_V2"],None)
        self.assertEqual(cfg["provider_output_ceiling"],128000)
        launcher=pathlib.Path(__file__).with_name("run-local.ps1").read_text(encoding="utf-8")
        self.assertIn("$env:ROOT_SCOUT_MAX_TOKENS = $null",launcher)
        self.assertIn("$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS = $null",launcher)
        with patch.dict(os.environ,{"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MAX_TOKENS":"96000","OPENAI_MODEL":"fixture","OPENAI_API_KEY":"test-only"},clear=True), \
             patch.object(root_scout.urllib.request,"urlopen") as http, self.assertRaisesRegex(ValueError,"ROOT_SCOUT_MODEL_OUTPUT_LIMIT"):
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",root_scout.Budget())
        http.assert_not_called()

    def test_v2_sse_length_and_missing_done_never_accept_json_actions(self):
        answer={"candidates":[{"claim":"must not be accepted"}],"updates":[],"actions":[],"next_question":"","stop":True}
        visible=json.dumps(answer)
        def stream(content,finish,usage,done):
            chunks=[]
            chunks.append("data: "+json.dumps({"choices":[{"delta":{"content":content}}]})+"\n\n")
            chunks.append("data: "+json.dumps({"choices":[{"delta":{},"finish_reason":finish}]})+"\n\n")
            if usage is not None: chunks.append("data: "+json.dumps({"choices":[],"usage":usage})+"\n\n")
            if done: chunks.append("data: [DONE]\n\n")
            return "".join(chunks).encode()
        class Response:
            def __init__(self,payload,timeout=False): self.payload=payload; self.timeout=timeout; self.reads=0
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                if self.reads: return b""
                self.reads+=1
                if self.timeout:
                    class Partial(Exception): pass
                    return self.payload
                return self.payload
        env={"OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"}
        for finish,done,usage in (("length",True,{"prompt_tokens":20,"completion_tokens":15999}),("stop",False,None)):
            budget=root_scout.Budget(max_tokens=48000); response=Response(stream(visible,finish,usage,done))
            with self.subTest(finish=finish,done=done),patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",return_value=response):
                with self.assertRaises(root_scout.IncompleteStreamError):
                    root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
            call=budget.calls[0]
            self.assertEqual(call["status"],"failed")
            self.assertEqual(call["raw_content"],visible)
            self.assertTrue(call["stream_metadata"]["done"] is done)
            self.assertEqual(call["raw_content_sha256"] if "raw_content_sha256" in call else call["visible_response_sha256"],root_scout.sha(visible))
            if usage is None:
                self.assertEqual(call["reserved_unknown_tokens"],call["input_reservation"]+call["max_output_reserved"])
                self.assertFalse(call["usage_complete"])
            else:
                self.assertEqual(call["confirmed_usage_tokens"],16019)
                self.assertEqual(call["reserved_unknown_tokens"],0)
                self.assertTrue(call["usage_complete"])
                self.assertTrue(call["stream_metadata"]["usage_final"])
                self.assertFalse(call["stream_metadata"]["response_complete"])
                self.assertEqual(call["stream_metadata"]["termination_basis"],"done_with_nonstop_finish")

    def test_sse_heartbeat_cannot_extend_wall_deadline(self):
        class WallBudget:
            def __init__(self): self.remaining=[2.0,.5,0.0]; self.flushes=0
            def remaining_wall(self): return self.remaining.pop(0)
            def flush(self): self.flushes+=1
        class Response:
            def __init__(self): self.n=0
            def read1(self,_limit): self.n+=1; return b": heartbeat\n\n"
        budget=WallBudget(); rec={}
        with self.assertRaises(TimeoutError):
            root_scout.read_chat_sse(Response(),budget,rec,root_scout.time.monotonic())
        self.assertEqual(rec["stream_metadata"]["bytes_received"],len(b": heartbeat\n\n")*2)
        self.assertEqual(budget.flushes,1)

    def test_v2_sse_midstream_timeout_keeps_partial_text_and_unknown_reservation(self):
        partial='data: '+json.dumps({"choices":[{"delta":{"content":"{"+'"candidates":'}}]})+'\n\n'
        class Response:
            def __init__(self): self.first=True
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                if self.first: self.first=False; return partial.encode()
                raise TimeoutError("stream stalled")
        budget=root_scout.Budget(max_tokens=48000)
        env={"OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"}
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            with self.assertRaises(TimeoutError): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
        call=budget.calls[0]
        self.assertEqual(call["error_class"],"model_call_timeout")
        self.assertFalse(call["stream_metadata"]["done"])
        self.assertTrue(call["raw_content"].startswith('{"candidates":'))
        self.assertEqual(call["reserved_unknown_tokens"],call["input_reservation"]+call["max_output_reserved"])
        self.assertFalse(call["usage_complete"])

    def test_v2_sse_intermediate_usage_is_lower_bound_until_done(self):
        usage_event='data: '+json.dumps({"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":100}})+'\n\n'
        class Response:
            def __init__(self): self.first=True
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                if self.first: self.first=False; return usage_event.encode()
                raise TimeoutError("before final usage/DONE")
        budget=root_scout.Budget(max_tokens=48000)
        env={"OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"}
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            with self.assertRaises(TimeoutError): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
        call=budget.calls[0]
        self.assertEqual(call["confirmed_usage_tokens"],120)
        self.assertEqual(call["reserved_unknown_tokens"],call["max_output_reserved"]-100)
        self.assertFalse(call["usage_complete"])
        self.assertEqual(call["usage_accounting_basis"],"server_usage_lower_bound")
        self.assertEqual(call["stream_metadata"]["termination_basis"],"read_interrupted_before_done")

    def test_v2_sse_refusal_delta_is_recorded_and_rejected(self):
        events=[
            {"choices":[{"delta":{"refusal":"refusal text must not be stored"}}]},
            {"choices":[{"delta":{},"finish_reason":"stop"}]},
            {"choices":[],"usage":{"prompt_tokens":15,"completion_tokens":3}},
        ]
        wire="".join("data: "+json.dumps(x)+"\n\n" for x in events)+"data: [DONE]\n\n"
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit): return wire.encode()
        budget=root_scout.Budget(max_tokens=48000)
        env={"OPENAI_API_KEY":"mock","OPENAI_MODEL":"mock","OPENAI_BASE_URL":"http://mock/v1","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000"}
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",return_value=Response()):
            with self.assertRaisesRegex(RuntimeError,"model_refusal"):
                root_scout.OpenAICompatible({}).ask("EXPLORE_V2","{}",budget)
        call=budget.calls[0]
        self.assertTrue(call["stream_metadata"]["refusal_observed"])
        self.assertNotIn("refusal text must not be stored",json.dumps(call))
        self.assertTrue(call["usage_complete"])
        self.assertEqual(call["stream_metadata"]["termination_basis"],"done_and_finish_stop")

    def test_v2_later_stream_failure_keeps_earlier_candidate_and_partial_ledger(self):
        first={"candidates":[{"kind":"defect_candidate","ref":"seed","claim":"The accumulator seed may fail for negative values."}],
               "updates":[],"actions":[],"next_question":"Check a negative singleton.","stop":False}
        partial='{"candidates":[{"claim":"unfinished'
        calls=[]
        def ask(_model,phase,_prompt,budget,max_output=None):
            calls.append(phase)
            if len(calls)==1:
                budget.add({"phase":phase,"status":"ok","confirmed_usage_tokens":9,"reserved_unknown_tokens":0,"usage_complete":True})
                return copy.deepcopy(first)
            budget.add({"phase":phase,"status":"failed","error":"IncompleteStreamError","error_class":"stream_incomplete",
                        "input_reservation":100,"max_output_reserved":16000,"confirmed_usage_tokens":0,"reserved_unknown_tokens":16100,
                        "usage_complete":False,"raw_content":partial,"raw_content_truncated":False,"raw_chars":len(partial),
                        "visible_response_sha256":root_scout.sha(partial),"stream_metadata":{"mode":"sse","bytes_received":80,"events":1,"done":False,"finish_reason":"length"}})
            raise root_scout.IncompleteStreamError("incomplete test stream")
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose(self.req,strategy="causal")
        self.assertEqual(len(state["candidates"]),1)
        self.assertEqual(state["candidates"][0]["original_claim"],first["candidates"][0]["claim"])
        self.assertEqual(state["raw_candidates"][-1]["proposal"],partial)
        self.assertEqual(state["stop_reason"],"model_protocol_incomplete")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_action_ref_bound_in_a_later_turn_stays_unresolved_on_the_earlier_action(self):
        # A turn-1 action naming a ref that no turn-1 candidate owns is rejected with
        # candidate_id null. Binding that ref in turn 2 must not attach it to the
        # earlier action retroactively, or the saved state can never be re-verified.
        first={"candidates":[{"kind":"defect_candidate","ref":"seed","claim":"The accumulator seed may fail for negative values."}],
               "updates":[],"actions":[{"kind":"run_bash","ref":"probe","command":"echo 1","question":"Does the seed survive?","candidate_refs":["later"]}],
               "next_question":"Which path fixes the seed?","stop":False}
        second={"candidates":[{"kind":"defect_candidate","ref":"later","claim":"The comparison may also drop the largest value."}],
                "updates":[],"actions":[],"next_question":"","stop":True}
        calls=[]
        def ask(_model,phase,_prompt,budget,max_output=None):
            calls.append(phase)
            budget.add({"phase":phase,"status":"ok","confirmed_usage_tokens":9,"reserved_unknown_tokens":0,"usage_complete":True})
            return copy.deepcopy(first if len(calls)==1 else second)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner),patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose(self.req,strategy="causal")
        self.assertEqual(len(state["tool_actions"]),1)
        action=state["tool_actions"][0]
        self.assertEqual((action["turn"],action["status"]),(1,"rejected"))
        self.assertEqual(action["candidate_links"],[{"ref":"later","candidate_id":None}])
        self.assertEqual([c["proposal_ref"] for c in state["candidates"]],["seed","later"])
        self.assertEqual(state["candidate_aliases"],{"seed":"C0001","later":"C0002"})
        self.assertEqual(root_scout.verify_saved_case_state(state),state)
        forged=copy.deepcopy(state)
        forged["tool_actions"][0]["candidate_links"][0]["candidate_id"]="C0002"
        forged["integrity"]=root_scout.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"associations do not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_v2_parse_clarification_preserves_failed_response_and_accounting(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"init","claim":"The zero seed may break all-negative input.","locations":["line 3"]}],"updates":[],"actions":[],"next_question":"Inspect the seed range.","stop":True}
        visible='{"candidates":[{"claim":"The zero seed may break all-negative input."}'
        def ask(_model,phase,_prompt,budget,max_output=None):
            if phase=="EXPLORE_V2":
                budget.add({"phase":phase,"status":"failed","error":"JSONDecodeError","error_class":"response_parse_failed",
                            "raw_content":visible,"raw_content_truncated":False,"raw_chars":len(visible),
                            "visible_response_sha256":root_scout.sha(visible),"input_reservation":100,"max_output_reserved":12000,
                            "confirmed_usage_tokens":73,"reserved_unknown_tokens":0,"usage_complete":True,"wall_seconds":0.2})
                raise json.JSONDecodeError("invalid","",0)
            self.assertEqual(phase,"EXPLORE_V2_CLARIFY")
            prior=len(budget.calls)-1
            budget.add({"phase":phase,"status":"ok","response_complete":True,"max_output_reserved":12000,
                        "confirmed_usage_tokens":41,"reserved_unknown_tokens":0,"usage_complete":True,"wall_seconds":0.1})
            self.assertEqual(prior,0)
            return copy.deepcopy(response)
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
             patch.object(root_scout,"run_case",side_effect=self.runner), \
             patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose(self.req,strategy="causal")
        self.assertEqual(len(state["candidates"]),1)
        self.assertEqual(state["candidates"][0]["original_claim"],response["candidates"][0]["claim"])
        self.assertEqual(state["raw_candidates"][0]["proposal"],visible)
        self.assertEqual(state["budget"]["calls"][0]["confirmed_usage_tokens"],73)
        self.assertEqual(state["budget"]["calls"][1]["confirmed_usage_tokens"],41)
        self.assertEqual(state["protocol_clarifications"][0]["status"],"recovered")
        self.assertEqual(state["protocol_clarifications"][0]["clarification_call_index"],1)
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_feedback_prompt_keeps_source_extent_and_candidate_locations(self):
        req=copy.deepcopy(self.req)
        req["source"]="\n".join(f"line_{i}:"+"x"*200 for i in range(1,101))
        candidate={"id":"C0001","candidate_kind":"defect_candidate","current_claim":"possible seed issue","trigger_claim":"negative singleton",
                   "source_location_claims":["line 50"],"lifecycle":"open","current_evidence_ids":["T0002"],"evidence_reference_events":[],"evidence_links":[]}
        state={"tool_actions":[],"evidence":[],"candidates":[candidate],"protocol_issues":[],
               "model_turns":[{"turn":1,"next_question":"Check the loop boundary."}]}
        prompt=json.loads(root_scout.v2_prompt(req,state,2))
        self.assertEqual(prompt["source_line_count"],len(req["source"].splitlines()))
        self.assertEqual(prompt["source_context"]["full_source"],None)
        self.assertEqual(prompt["candidates"][0]["original_locations"],["line 50"])
        self.assertEqual(prompt["candidates"][0]["current_evidence_ids"],["T0002"])
        self.assertEqual(prompt["previous_next_question"],"Check the loop boundary.")
        self.assertTrue(any("windows shown here are partial" in note for note in prompt["controller_notes"]))
        self.assertEqual(prompt["source_context"]["source_windows"],[{"start_line":48,"end_line":52,
                                                                      "text":"\n".join(f"line_{i}:"+"x"*200 for i in range(48,53))}])
        self.assertIsNone(prompt["candidates"][0]["proposal_ref"])

    def test_v2_short_source_is_retained_in_later_turn(self):
        req={"statement":"small task","language":"python3","source":"a=1\nprint(a)"}
        prompt=json.loads(root_scout.v2_prompt(req,{"tool_actions":[],"evidence":[],"candidates":[],"protocol_issues":[],"model_turns":[]},2))
        self.assertEqual(prompt["source_context"]["full_source"],req["source"])
        self.assertEqual(prompt["source_context"]["source_windows"],[])

    def test_v2_latest_empty_question_does_not_resurrect_old_question(self):
        req={"statement":"small task","language":"python3","source":"a=1\nprint(a)"}
        state={"tool_actions":[],"evidence":[],"candidates":[],"protocol_issues":[],
               "model_turns":[{"turn":1,"next_question":"old question"},{"turn":2,"next_question":""}]}
        prompt=json.loads(root_scout.v2_prompt(req,state,3))
        self.assertEqual(prompt["previous_next_question"],"")

    def test_v2_083_followup_without_candidates_keeps_short_full_source(self):
        req=root_scout.load_json("validation/codecontests/hard-sample-pass-private-fail-v1/requests/cc-test-row-083.json")
        state={"tool_actions":[],"evidence":[],"candidates":[],"protocol_issues":[],
               "model_turns":[{"turn":1,"next_question":"Inspect the transition condition."}]}
        prompt=json.loads(root_scout.v2_prompt(req,state,2))
        self.assertEqual(prompt["source_line_count"],106)
        self.assertEqual(prompt["source_context"]["full_source"],req["source"])
        self.assertEqual(prompt["previous_next_question"],"Inspect the transition condition.")

    def test_v2_prompt_compacts_duplicate_action_payloads_without_losing_evidence(self):
        req={"statement":"sum values","language":"python3","source":"a\n\nb\nc\ntail\n\n\n"+"\n".join(f"line_{i}:"+"x"*200 for i in range(8,101))}
        state={"evidence":[
            {"id":"T1","version_id":"original","source_kind":"caller_trusted","input":"1","trusted_expected":True,
             "expected":"7","result":{"status":"ok","matched":True,"stdout":"7\n","stderr":"","returncode":0}},
            {"id":"T2","version_id":"V1","source_kind":"variant","input":"1","trusted_expected":True,
             "expected":"7","result":{"status":"ok","matched":True,"stdout":"7\n","stderr":"","returncode":0}},
            {"id":"T3","version_id":"original","source_kind":"caller_trusted","input":"bad","trusted_expected":True,
             "expected":"0","result":{"status":"tool_failed","matched":False,"stdout":"","stderr":"parse failed","returncode":None,"error_type":"ValueError"}}],
            "versions":{"original":{"id":"original","parent_version_id":None,"source_sha256":"orig","source":req["source"],"edits":[]},
                       "V1":{"id":"V1","parent_version_id":"original","source_sha256":"variant","source":req["source"],
                             "edits":[{"start_line":1,"end_line":1,"old_text":"a","new_text":"z"}]}},
            "candidates":[{"id":"C1","proposal_ref":"sum","candidate_kind":"defect_candidate","current_claim":"May omit one endpoint",
                           "trigger_claim":"n is positive","source_location_claims":["line 1"],"lifecycle":"active","current_evidence_ids":["T1"],
                           "evidence_reference_events":[],"evidence_links":[]}],
            "tool_actions":[
                {"id":"A1","turn":1,"kind":"read_source","status":"accepted","proposal":{"kind":"read_source","ref":"r1","line_start":1,"line_end":2,"question":"read"},
                 "result":{"lines":[{"line":1,"text":"a"},{"line":2,"text":""}]}},
                {"id":"A2","turn":1,"kind":"read_source","status":"accepted","proposal":{"kind":"read_source","ref":"r2","line_start":3,"line_end":3,"question":"read"},
                 "result":{"lines":[{"line":3,"text":"b"}]}},
                {"id":"A6","turn":2,"kind":"read_source","status":"accepted","proposal":{"kind":"read_source","ref":"r4","line_start":5,"line_end":7,"question":"read tail"},
                 "result":{"lines":[{"line":5,"text":"tail"},{"line":6,"text":""},{"line":7,"text":""}]}},
                {"id":"A3","turn":1,"kind":"read_source","status":"rejected","proposal":{"kind":"read_source","ref":"r3","line_start":99,"line_end":99,"question":"read"},
                 "blocked_reason":"line range exceeds source","result":{"error":"line range exceeds source","huge_debug":"x"*1000}},
                {"id":"A4","turn":1,"kind":"run_original","status":"accepted","proposal":{"kind":"run_original","ref":"o1","input":"1","question":"run"},
                 "result":{"evidence_ids":["T1"],"observation":{"status":"ok","stdout":"7\n","build":{"stages":["large"]}}}},
                {"id":"A7","turn":2,"kind":"run_original","status":"rejected","proposal":{"kind":"run_original","ref":"o2","input":"bad","question":"retry"},
                 "blocked_reason":"tool admission failed","result":{"evidence_ids":["T3"],"error_type":"ValueError","reason":"invalid input","observation":{"status":"tool_failed","stderr":"parse failed","build":{"container":"do not include"}}}},
                {"id":"A5","turn":2,"kind":"run_variant","status":"accepted","version_id":"V1","proposal":{"kind":"run_variant","ref":"v1","parent_version_id":"original",
                 "candidate_refs":["sum"],"input_refs":["T1"],"inputs":["2"],"edits":[{"start_line":1,"end_line":1,"old_text":"a","new_text":"z"}],"question":"compare"},
                 "result":{"source_sha256":"variant","build":{"container":"large"},"observations":[{"original_evidence_id":"T1","variant_evidence_id":"T2",
                   "trusted_expected":True,"original_status":"ok","variant_status":"ok","original_matched":True,"variant_matched":True,"stdout_changed":False,"docker_stages":["large"]}]}}
            ],"protocol_issues":[]}
        before=json.dumps(state,sort_keys=True)
        data=json.loads(root_scout.v2_prompt(req,state,3))
        self.assertEqual(json.dumps(state,sort_keys=True),before)
        self.assertEqual(data["source_context"]["source_windows"],[{"start_line":1,"end_line":3,"text":"a\n\nb"},
                                                                     {"start_line":5,"end_line":7,"text":"tail\n\n"}])
        history={x["id"]:x for x in data["tool_action_history"]}
        self.assertNotIn("result",history["A1"])
        self.assertEqual(history["A3"]["result"],{"error":"line range exceeds source"})
        self.assertNotIn("input",history["A4"]["proposal"])
        self.assertEqual(history["A4"]["result"],{"evidence_ids":["T1"]})
        self.assertEqual(history["A7"]["result"],{"error_type":"ValueError","reason":"invalid input","evidence_ids":["T3"]})
        self.assertEqual(history["A7"]["proposal"]["input"],"bad")
        self.assertIn("parent_version_id",history["A5"]["proposal"])
        self.assertIn("edits",history["A5"]["proposal"])
        self.assertNotIn("inputs",history["A5"]["proposal"])
        self.assertEqual(history["A5"]["result"]["observations"][0]["variant_matched"],True)
        self.assertEqual({x["id"] for x in data["observations"]},{"T1","T2","T3"})
        self.assertEqual(data["candidates"][0]["claim"],"May omit one endpoint")
        self.assertEqual(data["candidates"][0]["lifecycle"],"active")

    def test_default_has_no_hard_token_cap_and_launcher_clears_inherited_caps(self):
        with patch.dict(os.environ,{},clear=True):
            cfg=root_scout.model_budget_configuration()
            self.assertIsNone(cfg["token_limit"])
            self.assertEqual(cfg["soft_token_target"],30000)
            self.assertEqual(cfg["output_policy"],"adaptive")
            self.assertIsNone(cfg["phase_output_tokens"]["EXPLORE_V2"])
            self.assertIsNone(root_scout.Budget().max_tokens)
        with patch.dict(os.environ,{"ROOT_SCOUT_MAX_TOKENS":"30000"},clear=True):
            cfg=root_scout.model_budget_configuration()
            self.assertEqual(cfg["output_policy"],"fixed")
            self.assertEqual(cfg["phase_output_tokens"]["EXPLORE_V2"],20000)
        launcher=pathlib.Path(__file__).with_name("run-local.ps1").read_text(encoding="utf-8")
        self.assertIn("$env:ROOT_SCOUT_MAX_TOKENS = $null",launcher)
        self.assertIn("$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS = $null",launcher)
        self.assertIn("$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS_EXPLORE_V2 = $null",launcher)
        self.assertIn("$env:ROOT_SCOUT_OUTPUT_POLICY = 'adaptive'",launcher)
        self.assertIn("$env:OPENAI_MODEL -eq 'deepseek-v4.1-flash'",launcher)
        self.assertIn("$env:ROOT_SCOUT_MODEL_OUTPUT_LIMIT = '128000'",launcher)

    def test_unlimited_v2_adaptive_calls_continue_past_soft_target_at_provider_cap(self):
        answer={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        events=[{"choices":[{"delta":{"content":json.dumps(answer)}}]},
                {"choices":[{"delta":{},"finish_reason":"stop"}]},
                {"choices":[],"usage":{"prompt_tokens":20000,"completion_tokens":10000,"reasoning_tokens":5000}}]
        wire=("".join("data: "+json.dumps(x)+"\n\n" for x in events)+"data: [DONE]\n\n").encode()
        class Response:
            def __init__(self): self.sent=False
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                if self.sent: return b""
                self.sent=True; return wire
        bodies=[]
        def open_request(req,timeout=None): bodies.append(json.loads(req.data)); return Response()
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000",
             "OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=open_request):
            cfg=root_scout.model_budget_configuration(); budget=root_scout.Budget(max_tokens=cfg["token_limit"])
            self.assertIsNone(cfg["token_limit"])
            for _ in range(2): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","x"*40000,budget)
        self.assertEqual(len(bodies),2)
        self.assertEqual([x["max_completion_tokens"] for x in bodies],[128000,128000])
        self.assertGreater(budget.confirmed_total(),30000)
        summary=budget.accounting_summary()
        self.assertEqual(summary["soft_target_overage_tokens"],budget.confirmed_total()-30000)
        self.assertEqual(summary["reserved_unknown_tokens"],0)

    def test_unlimited_usage_without_final_usage_remains_unknown_and_does_not_stop_next_call(self):
        answer={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        partial=("data: "+json.dumps({"choices":[{"delta":{},"finish_reason":None}],"usage":{"prompt_tokens":20,"completion_tokens":100}})+"\n\n").encode()
        complete=("data: "+json.dumps({"choices":[{"delta":{"content":json.dumps(answer)}}]})+"\n\n"
                  +"data: "+json.dumps({"choices":[{"delta":{},"finish_reason":"stop"}]})+"\n\n"
                  +"data: "+json.dumps({"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4}})+"\n\n"
                  +"data: [DONE]\n\n").encode()
        class Response:
            def __init__(self,data,timeout=False): self.data=data; self.timeout=timeout; self.done=False
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read1(self,_limit):
                if not self.done:
                    self.done=True
                    if self.timeout: raise TimeoutError("cut before DONE")
                    return self.data
                return b""
        responses=[Response(partial,True),Response(complete)]
        requests=[]
        def open_request(req,timeout=None): requests.append(req); return responses.pop(0)
        env={"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000",
             "OPENAI_API_KEY":"test-only","OPENAI_MODEL":"fixture","OPENAI_BASE_URL":"http://mock/v1"}
        with patch.dict(os.environ,env,clear=True),patch.object(root_scout.urllib.request,"urlopen",side_effect=open_request):
            budget=root_scout.Budget()
            with self.assertRaises(TimeoutError): root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short",budget)
            unknown_after_failure=budget.unknown_total()
            self.assertGreater(unknown_after_failure,0)
            self.assertEqual(budget.confirmed_total(),0)
            root_scout.OpenAICompatible({}).ask("EXPLORE_V2","short",budget)
        self.assertEqual(len(requests),2)
        self.assertGreaterEqual(budget.unknown_total(),unknown_after_failure)
        self.assertEqual(budget.confirmed_total(),14)
        self.assertEqual(budget.accounting_summary()["soft_target_overage_tokens"],0)

    def test_adaptive_unlimited_requires_provider_ceiling_only_when_model_configured(self):
        with patch.dict(os.environ,{"ROOT_SCOUT_OUTPUT_POLICY":"adaptive"},clear=True):
            cfg=root_scout.model_budget_configuration()
            self.assertIsNone(cfg["provider_output_ceiling"])
        with patch.dict(os.environ,{"ROOT_SCOUT_OUTPUT_POLICY":"adaptive","OPENAI_MODEL":"fixture","OPENAI_API_KEY":"test-only"},clear=True), \
             patch.object(root_scout.urllib.request,"urlopen") as http:
            with self.assertRaisesRegex(ValueError,"ROOT_SCOUT_MODEL_OUTPUT_LIMIT"):
                root_scout.OpenAICompatible({}).ask("EXPLORE_V2","prompt",root_scout.Budget())
            http.assert_not_called()

    def test_v2_explicit_next_question_allows_bounded_actionless_feedback(self):
        packets=[{"candidates":[],"updates":[],"actions":[],"next_question":"Review whether the stated range covers zero.","stop":False},
                 {"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}]
        seen=[]
        def ask(_model,phase,prompt,_budget,max_output=None):
            seen.append((phase,json.loads(prompt)))
            return packets[len(seen)-1]
        def passing(source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":case.get("expected",""),"stderr":"","returncode":0,"wall_seconds":0.01,"profile":profile}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
             patch.object(root_scout,"run_case",side_effect=passing), \
             patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state=root_scout.diagnose(self.req,strategy="causal")
        self.assertEqual(len(seen),2)
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(sum(1 for turn in state["model_turns"] if turn.get("response_complete")),2)
        self.assertEqual(seen[1][1]["versions"][0]["id"],"original")
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_cli_saved_replay_rehashes_final_case_state(self):
        response={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        initial=self.run_v2(self.req,response)
        sandbox=initial["run_configuration"]["sandbox"]
        self.assertEqual(sandbox["stdout_bytes_max"],root_scout.SANDBOX_STDOUT_LIMIT_BYTES)
        self.assertEqual(sandbox["stderr_bytes_max"],root_scout.SANDBOX_STDERR_LIMIT_BYTES)
        self.assertEqual(sandbox["output_bytes_max"],root_scout.SANDBOX_OUTPUT_LIMIT_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            source=pathlib.Path(directory)/"source-case.json"
            source.write_text(json.dumps(initial),encoding="utf-8")
            out=pathlib.Path(directory)/"replay"
            args=argparse.Namespace(request=str(source),out=str(out),provider="replay",strategy="causal",engine="v2")
            with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:v2-test"}), \
                 patch.object(root_scout,"run_case",side_effect=self.runner):
                root_scout.cmd_diagnose(args)
            saved=root_scout.load_json(out/"case_state.json")
            self.assertIn("replay_origin",saved)
            self.assertEqual(root_scout.verify_saved_case_state(saved),saved)
            self.assertEqual(saved["request_snapshot"],self.req)

    def test_v2_result_exports_current_claims_with_only_real_linked_evidence(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"init","claim":"best may start too high.",
                                 "trigger":"a single negative value","locations":["line 3"],"evidence_ids":["T0002","T9999"]}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"fix","parent_version_id":"original","candidate_refs":["init"],
                              "edits":[{"start_line":3,"end_line":3,"old_text":"best=0","new_text":"best=-1000000001"}],
                              "input_refs":[],"inputs":["1\n-7"],"question":"Compare the source variant."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        result=root_scout.v2_result(state)
        self.assertEqual(result["schema_version"],"rootscout.result.v2")
        self.assertIn("status",result)
        self.assertEqual(len(result["findings"]),1)
        finding=result["findings"][0]
        self.assertEqual(set(finding),{"location","description","trigger","evidence","status"})
        self.assertEqual(finding["description"],state["candidates"][0]["current_claim"])
        self.assertEqual(finding["location"],state["candidates"][0]["source_location_claims"])
        self.assertEqual(finding["trigger"],state["candidates"][0]["trigger_claim"])
        actual={e["id"] for e in state["evidence"]}
        self.assertEqual([e["id"] for e in finding["evidence"] if e["association"]=="model_citation"],["T0002"])
        self.assertTrue({e["id"] for e in finding["evidence"]}<=actual)
        self.assertEqual(finding["status"],{"id":state["candidates"][0]["id"],"kind":"defect_candidate","lifecycle":"open","verification":"unverified",
                                          "conclusion_review":"not_needed","uncertainties":[],"protocol_status":"clean","trigger_domain_verified":False})
        self.assertEqual(state["candidates"][0]["evidence_links"][0]["association_kind"],"single_candidate")
        self.assertEqual(state["candidates"][0]["evidence_reference_events"][0]["rejected"],[{"id":"T9999","reason":"unknown, non-controller, or not yet supplied to this response"}])

    def test_v2_result_keeps_empty_withdrawn_and_merged_candidates_unconfirmed(self):
        empty=root_scout.v2_result({"candidates":[],"evidence":[],"tool_actions":[]})
        self.assertEqual(empty["findings"],[])
        self.assertEqual(empty["status"],"inconclusive")
        state={"tool_actions":[{"id":"A1"}],"evidence":[{"id":"T1"}],"candidates":[
            {"id":"C1","current_claim":"revised text","source_location_claims":["line 2"],"trigger_claim":"input x",
             "evidence_links":[{"action_id":"A1","evidence_ids":["T1","not-a-tool-record"]}],"lifecycle":"revised"},
            {"id":"C2","current_claim":"withdrawn text","source_location_claims":[],"trigger_claim":None,
             "evidence_links":[],"lifecycle":"withdrawn"},
            {"id":"C3","current_claim":"merged text","source_location_claims":["line 4"],"trigger_claim":"input y",
             "evidence_links":[],"lifecycle":"merged","merged_into_candidate_id":"C1"},
            {"id":"C4","current_claim":"untrusted status text","source_location_claims":[],"trigger_claim":None,
             "evidence_links":[],"lifecycle":"confirmed"}]}
        findings=root_scout.v2_result(state)["findings"]
        self.assertEqual([x["description"] for x in findings],["revised text","withdrawn text","merged text","untrusted status text"])
        self.assertEqual(findings[0]["evidence"],["T1"])
        self.assertEqual(findings[0]["status"],{"id":"C1","kind":"unclassified","lifecycle":"revised","verification":"unverified"})
        self.assertEqual(findings[1]["status"],{"id":"C2","kind":"unclassified","lifecycle":"withdrawn","verification":"unverified"})
        self.assertEqual(findings[2]["status"],{"id":"C3","kind":"unclassified","lifecycle":"merged","verification":"unverified","merged_into":"C1"})
        self.assertEqual(findings[3]["status"],{"id":"C4","kind":"unclassified","lifecycle":"unknown","verification":"unverified"})

    def test_v2_joint_action_association_is_separate_from_explicit_evidence(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"one","claim":"Claim one.","evidence_ids":["T0002"]},
                                 {"kind":"defect_candidate","ref":"two","claim":"Claim two."}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"joint","parent_version_id":"original","candidate_refs":["one","two"],
                              "edits":[{"start_line":3,"end_line":3,"old_text":"best=0","new_text":"best=-1000000001"}],
                              "input_refs":[],"inputs":["1\n-7"],"question":"Compare this shared variant."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        delivered=root_scout.v2_result(state)["findings"]
        self.assertEqual([e["id"] for e in delivered[0]["evidence"] if e["association"]=="model_citation"],["T0002"])
        self.assertFalse(any(e["association"]=="model_citation" for e in delivered[1]["evidence"]))
        self.assertTrue(all(e["association"]=="joint_candidates" for e in delivered[1]["evidence"]))
        self.assertEqual(state["candidates"][1]["current_evidence_ids"],[],"Action association must not become a model citation")
        self.assertTrue(all(x["evidence_links"][0]["association_kind"]=="joint_candidates" for x in state["candidates"]))
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_edit_range_off_by_one_is_repaired_from_the_text(self):
        # The unique old_text match locates the edit; a one-line slip in the declared
        # range is repaired and recorded instead of destroying the run.
        response={"candidates":[{"kind":"defect_candidate","ref":"init","claim":"The sentinel may be too high for all-negative input."}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"fix","parent_version_id":"original","candidate_refs":["init"],
                              "edits":[{"start_line":2,"end_line":3,"old_text":"best=0","new_text":"best=-1000000001"}],
                              "input_refs":[],"inputs":["1\n-7"],"question":"Try the repaired sentinel."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        action=state["tool_actions"][0]
        self.assertEqual(action["status"],"accepted",action.get("blocked_reason"))
        version=state["versions"][action["version_id"]]
        self.assertEqual(version["edit_range_repairs"],[{"edit_index":0,"declared":[2,3],"actual":[3,3],
            "reason":"declared line range was off by at most one line; the unique old_text match located the edit"}])
        self.assertEqual(action["result"]["edit_range_repairs"],version["edit_range_repairs"])
        self.assertIn("best=-1000000001",version["source"])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)
        forged=copy.deepcopy(state)
        forged["versions"][action["version_id"]]["edit_range_repairs"][0]["actual"]=[2,3]
        forged["integrity"]=root_scout.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"edit range repairs do not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_v2_edit_range_beyond_one_line_is_still_rejected_and_names_the_real_range(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"init","claim":"The sentinel may be too high."}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"fix","parent_version_id":"original","candidate_refs":["init"],
                              "edits":[{"start_line":6,"end_line":6,"old_text":"best=0","new_text":"best=-1000000001"}],
                              "input_refs":[],"inputs":["1\n-7"],"question":"Try the sentinel variant."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        action=state["tool_actions"][0]
        self.assertEqual(action["status"],"rejected")
        self.assertIn("matches lines 3-3",action["blocked_reason"])
        self.assertIn("declared 6-6",action["blocked_reason"])

    def test_v2_ambiguous_old_text_is_never_relocated(self):
        with self.assertRaisesRegex(ValueError,"old_text must match exactly once"):
            root_scout.v2_apply_edits("a=1\nb=2\na=1\n",[{"start_line":2,"end_line":2,"old_text":"a=1","new_text":"a=9"}])
        with self.assertRaisesRegex(ValueError,"old_text must match exactly once"):
            root_scout.v2_apply_edits("a=1\nb=2\n",[{"start_line":1,"end_line":1,"old_text":"zzz","new_text":"a=9"}])

    def test_v2_unknown_candidate_ref_creates_one_rejected_action_and_keeps_claim(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"known","claim":"A claim survives a bad action reference."}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"bad-ref","parent_version_id":"original","candidate_refs":["missing"],
                              "edits":[{"start_line":3,"end_line":3,"old_text":"best=0","new_text":"best=-1000000001"}],
                              "input_refs":[],"inputs":[],"question":"Try the local variant."}],"next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(len(state["candidates"]),1)
        self.assertEqual(len(state["tool_actions"]),1)
        self.assertEqual(len({x["id"] for x in state["tool_actions"]}),1)
        self.assertEqual(state["tool_actions"][0]["status"],"rejected")
        self.assertEqual(state["tool_actions"][0]["blocked_reason"],"candidate_refs contains an unknown or ambiguous proposal ref")
        self.assertEqual(state["candidates"][0]["evidence_links"],[])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_rejected_known_candidate_actions_are_associated_without_evidence(self):
        for candidate_specs,refs in [
            ([{"ref":"one","claim":"First readable claim."}], ["one"]),
            ([{"ref":"one","claim":"First readable claim."},{"ref":"two","claim":"Second readable claim."}], ["one","two"]),
        ]:
            with self.subTest(refs=refs):
                response={"candidates":[{"kind":"defect_candidate",**item} for item in candidate_specs],"updates":[],
                          "actions":[{"kind":"run_variant","ref":"bad-edit","parent_version_id":"original","candidate_refs":refs,
                                      "edits":[{"start_line":3,"end_line":3,"old_text":"not the source","new_text":"best=-1000000001"}],
                                      "input_refs":[],"inputs":["1\n-7"],"question":"Test a rejected edit."}],
                          "next_question":"","stop":True}
                state=self.run_v2(self.req,response)
                action=state["tool_actions"][0]
                self.assertEqual(action["status"],"rejected")
                self.assertEqual(action["blocked_reason"],"old_text must match exactly once in parent source")
                self.assertEqual(len(state["tool_actions"]),1)
                for candidate in state["candidates"]:
                    self.assertEqual(candidate["evidence_links"],[{
                        "action_id":"A0001","evidence_ids":[],
                        "association_kind":"joint_candidates" if len(refs)>1 else "single_candidate",
                        "interpretation":"tool-action association only; not model citation or semantic/independent support"}])
                self.assertEqual(root_scout.verify_saved_case_state(state),state)
                forged=copy.deepcopy(state)
                forged["candidates"][0]["evidence_links"][0]["evidence_ids"]=["T0001"]
                forged["integrity"]=root_scout.v2_integrity(forged)
                with self.assertRaisesRegex(ValueError,"tool-action associations do not recompute"):
                    root_scout.verify_saved_case_state(forged)

    def test_v2_mixed_known_and_unknown_action_refs_do_not_link_known_candidate(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"known","claim":"This claim remains readable."}],"updates":[],
                  "actions":[{"kind":"run_variant","ref":"mixed-ref","parent_version_id":"original","candidate_refs":["known","missing"],
                              "edits":[{"start_line":3,"end_line":3,"old_text":"best=0","new_text":"best=-1000000001"}],
                              "input_refs":[],"inputs":["1\n-7"],"question":"This mixed-reference action is rejected."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertEqual(state["tool_actions"][0]["status"],"rejected")
        self.assertEqual(state["candidates"][0]["evidence_links"],[])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_future_evidence_id_is_rejected_even_if_same_turn_action_creates_it(self):
        _,_,base=root_scout.build_testcases(self.req); _,oracle_tests,_=root_scout.make_oracle_plan(self.req)
        future_id=f"T{len(root_scout.combine_testcases(base,oracle_tests))+1:04d}"
        response={"candidates":[{"kind":"defect_candidate","ref":"c","claim":"A new run may distinguish this claim.","evidence_ids":[future_id]}],
                  "updates":[],"actions":[{"kind":"run_original","ref":"run","input":"1\n123456789","question":"Observe the original output."}],
                  "next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        self.assertIn(future_id,{x["id"] for x in state["evidence"]})
        self.assertEqual(state["candidates"][0]["current_evidence_ids"],[])
        self.assertEqual(state["candidates"][0]["evidence_reference_events"][0]["rejected"][0]["id"],future_id)
        self.assertEqual(root_scout.verify_saved_case_state(state),state)

    def test_v2_revision_updates_claim_trigger_location_and_evidence(self):
        packets=[{"candidates":[{"kind":"defect_candidate","ref":"c","claim":"Initial claim.","trigger":"initial trigger",
                                   "locations":["line 3"],"evidence_ids":["T0002"]}],"updates":[],"actions":[],
                  "next_question":"Check another observation.","stop":False},
                 {"candidates":[],"updates":[{"candidate_id":"C0001","disposition":"revise","claim":"Revised claim.",
                                                 "trigger":"updated trigger","locations":["line 4"],"evidence_ids":["T0001","T9999"],"reason":"The second observation narrows the condition."}],
                  "actions":[],"next_question":"","stop":True}]
        state,calls=self.run_v2_packets(self.req,packets)
        candidate=state["candidates"][0]
        self.assertEqual(len(calls),2)
        self.assertEqual(candidate["original_claim"],"Initial claim.")
        self.assertEqual(candidate["current_claim"],"Revised claim.")
        self.assertEqual(candidate["trigger_claim"],"updated trigger")
        self.assertEqual(candidate["source_location_claims"],["line 4"])
        self.assertEqual(candidate["current_evidence_ids"],["T0001"])
        self.assertEqual(candidate["revisions"][0]["locations"],["line 4"])
        self.assertEqual(root_scout.v2_result(state)["findings"][0]["description"],"Revised claim.")
        self.assertEqual([e["id"] for e in root_scout.v2_result(state)["findings"][0]["evidence"]],["T0001"])
        self.assertEqual(root_scout.verify_saved_case_state(state),state)
        forged=copy.deepcopy(state); forged["candidates"][0]["current_evidence_ids"]=["T0002"]; forged["integrity"]=root_scout.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"citations do not recompute"):
            root_scout.verify_saved_case_state(forged)

    def test_v3_candidate_cannot_downgrade_to_legacy_by_removing_record_fields(self):
        state=self.run_v2(self.req,{"candidates":[{"ref":"c","claim":"A preserved candidate."}],"updates":[],"actions":[],"next_question":"","stop":True})
        forged=copy.deepcopy(state); candidate=forged["candidates"][0]
        for key in ("record_schema_version","current_evidence_ids","evidence_reference_events"):
            candidate.pop(key,None)
        forged["integrity"]=root_scout.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"v3 candidate schema marker is missing"):
            root_scout.verify_saved_case_state(forged)

    def test_v2_old_schema_snapshot_keeps_its_original_validation_rules(self):
        old=root_scout.load_json("validation/design-v2-full-eight-v1/max-loop-count-cpp17/case_state.json")
        self.assertIn(old["run_configuration"]["protocol_schema_version"],(1,2))
        self.assertEqual(root_scout.verify_saved_case_state(old),old)
        req=root_scout.verify_saved_case_state(old)
        def passing(_source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":case.get("expected",""),"stderr":"","returncode":0,"wall_seconds":0.01,"profile":profile}
        with patch.object(root_scout,"docker_preflight",return_value=old["run_configuration"]["runtime"]),patch.object(root_scout,"run_case",side_effect=passing):
            replayed=root_scout.diagnose(req,provider="replay",replay_state=old)
        self.assertEqual(replayed["run_configuration"]["protocol_schema_version"],old["run_configuration"]["protocol_schema_version"])
        self.assertEqual(replayed["case_state_schema_version"],1)
        self.assertEqual(replayed["run_configuration"]["replay_compatibility"]["source_payload_sha256"],old["integrity"]["payload_sha256"])
        self.assertTrue(all("record_schema_version" not in x for x in replayed["candidates"]))
        self.assertEqual(root_scout.verify_saved_case_state(replayed),replayed)

    def test_v2_cli_reports_technical_failure_with_nonzero_return_and_no_fake_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            source=pathlib.Path(directory)/"request.json"; source.write_text(json.dumps(self.req),encoding="utf-8")
            out=pathlib.Path(directory)/"out"
            args=argparse.Namespace(request=str(source),out=str(out),provider="model",strategy="causal",engine="v2")
            with patch.object(root_scout,"diagnose",side_effect=RuntimeError("controller failure")):
                self.assertEqual(root_scout.cmd_diagnose(args),1)
            result=root_scout.load_json(out/"result.json")
            self.assertEqual(result["findings"],[])
            self.assertEqual(result["status"],"error")
            self.assertEqual(root_scout.load_json(out/"case_state.json")["stop_reason"],"failed")
            with patch.object(root_scout,"diagnose",side_effect=RuntimeError("controller failure")),patch.object(sys,"argv",["root_scout.py","diagnose",str(source),"--out",str(out)]):
                with self.assertRaises(SystemExit) as raised: root_scout.main()
            self.assertEqual(raised.exception.code,1)

    def test_v2_cli_writes_result_json_and_keeps_case_state_without_markdown(self):
        response={"candidates":[{"kind":"defect_candidate","ref":"init","claim":"possible initialization issue",
                                 "trigger":"negative singleton","locations":["line 3"]}],
                  "updates":[],"actions":[],"next_question":"","stop":True}
        state=self.run_v2(self.req,response)
        with tempfile.TemporaryDirectory() as directory:
            source=pathlib.Path(directory)/"request.json"
            source.write_text(json.dumps(self.req),encoding="utf-8")
            out=pathlib.Path(directory)/"out"
            args=argparse.Namespace(request=str(source),out=str(out),provider="replay",strategy="causal",engine="v2")
            with patch.object(root_scout,"diagnose",return_value=state): root_scout.cmd_diagnose(args)
            saved=root_scout.load_json(out/"case_state.json")
            result=root_scout.load_json(out/"result.json")
            self.assertEqual(root_scout.verify_saved_case_state(saved),saved)
            self.assertEqual(result["schema_version"],"rootscout.result.v2")
            self.assertTrue((out/"probe.json").exists())
            self.assertFalse((out/"report.md").exists())
            self.assertTrue((out/"case_state.json").exists())

    def test_legacy_cli_delivery_still_writes_markdown(self):
        state={"diagnostic_core_version":1,"id":"legacy","stop_reason":"oracle_unresolved","roots":[],
               "suspected_failures":[],"budget":{},"coverage":{"untested":[]}}
        with tempfile.TemporaryDirectory() as directory:
            source=pathlib.Path(directory)/"request.json"
            source.write_text(json.dumps(self.req),encoding="utf-8")
            out=pathlib.Path(directory)/"legacy-out"
            args=argparse.Namespace(request=str(source),out=str(out),provider="model",strategy="causal",engine="legacy")
            with patch.object(root_scout,"diagnose",return_value=state): root_scout.cmd_diagnose(args)
            self.assertTrue((out/"report.md").exists())
            self.assertFalse((out/"result.json").exists())


class ReviewedBuiltinOracleTests(unittest.TestCase):
    def setUp(self):
        self.xor_req=root_scout.load_json("validation/codecontests/hard-sample-pass-private-fail-v1/requests/cc-test-row-034.json")
        self.sort_req=root_scout.load_json("validation/codecontests/hard-sample-pass-private-fail-v1/requests/cc-test-row-083.json")

    def contract(self,kind,req):
        if kind=="increasing_xor.v1":
            n_quote="The first line contains a single integer n (1 ≤ n ≤ 10^6)."
            value_quote="The second line contains n integers a_1, a_2, …, a_n (0 ≤ a_i ≤ 5000) — the elements of the array a."
            return {"kind":kind,"version":1,"caller_attested":True,"reviewed_by":"caller attested; AI-assisted engineering review",
                    "statement_sha256":root_scout.sha(req["statement"]),"statement_quote":oracle_tools.CONTRACTS[kind]["statement_anchor"],
                    "semantics":oracle_tools.CONTRACTS[kind]["semantics"],"input_format":oracle_tools.CONTRACTS[kind]["input_format"],
                    "domain":{"n_min":1,"n_max":8,"value_min":0,"value_max":5000},
                    "domain_quote":n_quote,"domain_quotes":[n_quote,value_quote]}
        quote="The first line contains the single integer n (1 ≤ n ≤ 10^{12})."
        return {"kind":kind,"version":1,"caller_attested":True,"reviewed_by":"caller attested; AI-assisted engineering review",
                "statement_sha256":root_scout.sha(req["statement"]),"statement_quote":oracle_tools.CONTRACTS[kind]["statement_anchor"],
                "semantics":oracle_tools.CONTRACTS[kind]["semantics"],"input_format":oracle_tools.CONTRACTS[kind]["input_format"],
                "domain":{"n_min":1,"n_max":1000},"domain_quote":quote}

    def test_xor_oracle_small_domain_generators_and_independent_check(self):
        raw=self.contract("increasing_xor.v1",self.xor_req)
        canonical,error=oracle_tools.validate_contract(self.xor_req["statement"],raw)
        self.assertIsNone(error)
        canonical["contract_sha256"]=root_scout.canonical_sha(canonical)
        self.assertTrue(oracle_tools.self_check("increasing_xor.v1"))
        initial=oracle_tools.generate(canonical,"initial")
        residual=oracle_tools.generate(canonical,"residual")
        self.assertEqual(len(initial),8)
        self.assertEqual(len(residual),8)
        self.assertFalse({x["input"] for x in initial}&{x["input"] for x in residual})
        self.assertTrue(all(x["oracle_implementation_sha256"]==oracle_tools.ORACLE_IMPLEMENTATION_SHA256 for x in initial+residual))
        for case in initial+residual:
            self.assertEqual(oracle_tools.evaluate("increasing_xor.v1",case["input"],canonical["domain"])["expected"],case["expected"])
            self.assertTrue(case["legal_input"])
        self.assertEqual(oracle_tools.evaluate("increasing_xor.v1","9\n0 1 2 3 4 5 6 7 8\n",canonical["domain"])["status"],"unknown")
        self.assertEqual(oracle_tools.evaluate("increasing_xor.v1","1\n5001\n",canonical["domain"])["status"],"unknown")
        req=copy.deepcopy(self.xor_req); req["oracle_tool_contract"]=raw
        bound,tests,error=root_scout.make_oracle_plan(req)
        self.assertIsNone(error)
        generated,error=root_scout.causal_test_from_input(req,bound,"3\n1 2 3\n")
        self.assertIsNone(error)
        self.assertEqual(generated["expected"],"4\n0 1 2 3\n")
        self.assertEqual(generated["source_kind"],"builtin_oracle:increasing_xor.v1")
        self.assertIsNone(generated.get("expected_model"))
        self.assertIsNotNone(root_scout.causal_test_from_input(req,bound,"9\n0 1 2 3 4 5 6 7 8\n")[1])

    def test_xor_whitespace_rule_is_local_and_preserves_raw_stdout(self):
        result={"status":"ok","stdout":"3\n0   1\t2 \n","stdout_truncated":False}
        raw=result["stdout"]
        expected="3\n0 1 2\n"
        self.assertTrue(root_scout.causal_result_match(result,expected,"builtin_oracle:increasing_xor.v1"))
        self.assertFalse(root_scout.causal_result_match(result,expected,"caller_trusted"))
        self.assertFalse(root_scout.causal_result_match({**result,"stdout":"3\n0 1 3\n"},expected,"builtin_oracle:increasing_xor.v1"))
        self.assertEqual(result["stdout"],raw)

    def test_two_sorts_prefix_oracle_and_small_generation(self):
        raw=self.contract("two_sorts.v1",self.sort_req)
        canonical,error=oracle_tools.validate_contract(self.sort_req["statement"],raw)
        self.assertIsNone(error)
        canonical["contract_sha256"]=root_scout.canonical_sha(canonical)
        self.assertTrue(oracle_tools.self_check("two_sorts.v1"))
        initial=oracle_tools.generate(canonical,"initial")
        self.assertEqual(len(initial),8)
        self.assertIn("500\n",{x["input"] for x in initial})
        residual=oracle_tools.generate(canonical,"residual")
        self.assertEqual(len(residual),8)
        self.assertFalse({x["input"] for x in initial}&{x["input"] for x in residual})
        for n,expected in ((3,"0\n"),(12,"994733045\n"),(21,"978932159\n")):
            self.assertEqual(oracle_tools.evaluate("two_sorts.v1",f"{n}\n",canonical["domain"])["expected"],expected)
        self.assertEqual(oracle_tools.evaluate("two_sorts.v1","1001\n",canonical["domain"])["status"],"unknown")
        self.assertEqual(oracle_tools.evaluate("two_sorts.v1","9\n10\n",canonical["domain"])["status"],"unknown")

    def test_oracle_contract_is_caller_and_statement_bound(self):
        raw=self.contract("two_sorts.v1",self.sort_req)
        self.assertIsNone(oracle_tools.validate_contract(self.sort_req["statement"],raw)[1])
        for bad in (dict(raw,caller_attested=False),dict(raw,reviewed_by="model"),dict(raw,statement_sha256="0"*64),
                    dict(raw,statement_quote="1 ≤ n ≤ 10^12")):
            self.assertIsNotNone(oracle_tools.validate_contract(self.sort_req["statement"],bad)[1])

    def test_old_oracle_hash_is_allowed_for_replay_only(self):
        old="451bbb948fd595408464000783a074b39e24f9dc344a0157fe433d9a22bfbdf0"
        for kind in ("range_sum.v1","max.v1","range_min.v1"):
            self.assertTrue(oracle_tools.implementation_sha256_supported(old,kind))
        self.assertFalse(oracle_tools.implementation_sha256_supported(old,"increasing_xor.v1"))
        self.assertFalse(oracle_tools.implementation_sha256_supported(old,"two_sorts.v1"))
        self.assertTrue(oracle_tools.implementation_sha256_supported(oracle_tools.ORACLE_IMPLEMENTATION_SHA256,"increasing_xor.v1"))
        self.assertTrue(oracle_tools.implementation_sha256_known(old))
        self.assertFalse(oracle_tools.implementation_sha256_supported("0"*64,"max.v1"))

    def test_old_global_hash_cannot_authorize_new_oracle_kind(self):
        req=copy.deepcopy(self.sort_req)
        req["source"]="print(0)\n"
        req["oracle_tool_contract"]=self.contract("two_sorts.v1",req)
        response={"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True}
        def expected_runner(_source,_language,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":case.get("expected",""),"stderr":"","returncode":0,"wall_seconds":0.01,"profile":profile}
        state=V2DiscoveryTests().run_v2(req,response,expected_runner)
        self.assertEqual(root_scout.verify_saved_case_state(state),state)
        forged=copy.deepcopy(state)
        old="451bbb948fd595408464000783a074b39e24f9dc344a0157fe433d9a22bfbdf0"
        forged["run_configuration"]["oracle_implementation_sha256"]=old
        forged["integrity"]=root_scout.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"not allowed for this kind"):
            root_scout.verify_saved_case_state(forged)

        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:oracle-kind-test"}), \
             patch.object(root_scout,"run_case",side_effect=expected_runner):
            causal=root_scout.diagnose_causal(req,provider="replay")
        self.assertEqual(root_scout.verify_saved_case_state(causal),req)
        forged_causal=copy.deepcopy(causal)
        forged_causal["run_configuration"]["oracle"]["implementation_sha256"]=old
        forged_causal["integrity"]["run_configuration_sha256"]=root_scout.canonical_sha(forged_causal["run_configuration"])
        forged_causal["integrity"]["causal_ledger_sha256"]=root_scout.canonical_sha(root_scout.causal_ledger_payload(forged_causal))
        with self.assertRaisesRegex(ValueError,"not allowed for this kind"):
            root_scout.verify_saved_case_state(forged_causal)


    def test_v2_protocol_accepts_invariants_and_validates_bounds(self):
        valid = {"candidates":[],"updates":[],"actions":[],"invariants":["loop counter starts at 1","sentinel is 5001"],"next_question":"","stop":True}
        parsed = root_scout.validate_v2_response(valid)
        self.assertEqual(parsed["issues"], [])
        self.assertEqual(parsed["invariants"], ["loop counter starts at 1", "sentinel is 5001"])

        # established_invariants alias
        alias = {"candidates":[],"updates":[],"actions":[],"established_invariants":["G array persists between cases"],"next_question":"","stop":True}
        parsed_alias = root_scout.validate_v2_response(alias)
        self.assertEqual(parsed_alias["issues"], [])
        self.assertEqual(parsed_alias["invariants"], ["G array persists between cases"])

        # Bounds: too many invariants
        too_many = {"candidates":[],"updates":[],"actions":[],"invariants":["inv"]*9,"next_question":"","stop":True}
        parsed_bad = root_scout.validate_v2_response(too_many)
        self.assertTrue(any("invariants count exceeds per-turn limit" in x["reason"] for x in parsed_bad["issues"]))

        # Bounds: invariant too long
        too_long = {"candidates":[],"updates":[],"actions":[],"invariants":["x"*513],"next_question":"","stop":True}
        parsed_long = root_scout.validate_v2_response(too_long)
        self.assertTrue(any("invariant exceeds protocol byte limit" in x["reason"] for x in parsed_long["issues"]))

    def test_v2_prompt_surfaces_established_invariants_and_controller_notes(self):
        req = {"statement":"problem statement","source":"int main(){return 0;}\n","language":"cpp17","environment":{}}
        # Turn 1: empty invariants
        state1 = {"tool_actions":[],"evidence":[],"candidates":[],"protocol_issues":[],"model_turns":[],"invariants":[]}
        prompt1 = json.loads(root_scout.v2_prompt(req, state1, 1))
        self.assertEqual(prompt1["established_invariants"], [])
        self.assertFalse(any("Working memory" in x for x in prompt1.get("controller_notes", [])))

        # Turn 2 with invariants: should surface in established_invariants and controller_notes
        state2 = {"tool_actions":[],"evidence":[],"candidates":[],"protocol_issues":[],"model_turns":[],
                  "invariants":["nxt[x][it] is a skip-list cursor"]}
        prompt2 = json.loads(root_scout.v2_prompt(req, state2, 2))
        self.assertEqual(prompt2["established_invariants"], ["nxt[x][it] is a skip-list cursor"])
        self.assertTrue(any("Working memory: established_invariants lists program semantics" in x for x in prompt2.get("controller_notes", [])))

    def test_v2_invariants_accumulate_in_case_state_and_verify_integrity(self):
        req = {"statement":"Compute XOR sum","source":"int main(){}\n","language":"cpp17","environment":{}}
        first_resp = {"candidates":[{"kind":"defect_candidate","ref":"h1","claim":"Possible indexing flaw"}],
                      "updates":[],"actions":[],"invariants":["initial array is zeroed"],"next_question":"check loop","stop":False}
        second_resp = {"candidates":[],"updates":[],"actions":[],
                       "invariants":["initial array is zeroed","loop boundary reaches n+1"],"next_question":"","stop":True}
        calls = []
        def ask(_model, phase, prompt, budget, **kwargs):
            turn = kwargs.get("diagnostic_turn", len(calls)+1)
            calls.append((phase, turn))
            budget.add({"phase":phase,"model":"deepseek-v4.1-flash","status":"ok","diagnostic_turn":turn,
                        "confirmed_usage_tokens":10,"reserved_unknown_tokens":0,"usage_complete":True,"finish_reason":"stop"})
            return copy.deepcopy(first_resp if turn==1 else second_resp)
        def runner(_source, _language, case, _env, profile="formal", build_cache=None):
            return {"status":"ok","stdout":"","stderr":"","returncode":0,"wall_seconds":0.01,"profile":profile}
        with patch.object(root_scout,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(root_scout,"run_case",side_effect=runner), \
             patch.object(root_scout.OpenAICompatible,"ask",new=ask):
            state = root_scout.diagnose_v2(req, strategy="causal")
        self.assertEqual(state["invariants"], ["initial array is zeroed", "loop boundary reaches n+1"])
        completed_turns = [t for t in state["model_turns"] if t.get("response_complete")]
        self.assertEqual(len(completed_turns), 2)
        self.assertEqual(completed_turns[0]["invariants"], ["initial array is zeroed"])
        self.assertEqual(completed_turns[1]["invariants"], ["initial array is zeroed", "loop boundary reaches n+1"])
        self.assertEqual(root_scout.verify_saved_case_state(state), state)

    def test_sse_total_reasoning_stream_can_exceed_16_mib_and_keep_final_usage(self):
        reasoning = "r" * 900_000
        chunks = []
        for _ in range(19):
            event = {"choices":[{"delta":{"reasoning_content":reasoning}}]}
            chunks.append(("data: " + json.dumps(event) + "\n\n").encode())
        chunks.extend([
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":456}}\n\n',
            b"data: [DONE]\n\n",
        ])
        class Response:
            def __init__(self): self.chunks = list(chunks)
            def read1(self, _limit): return self.chunks.pop(0) if self.chunks else b""
        budget = root_scout.Budget(max_tokens=48000); rec = {}
        content, finish, usage, meta = root_scout.read_chat_sse(Response(), budget, rec, root_scout.time.monotonic())
        self.assertGreater(meta["bytes_received"], 16 * 1024 * 1024)
        self.assertEqual(content, "")
        self.assertEqual(finish, "stop")
        self.assertEqual(usage, {"prompt_tokens":123, "completion_tokens":456})
        self.assertTrue(meta["response_complete"])
        self.assertTrue(meta["usage_final"])
        self.assertNotIn("reasoning_content", json.dumps(rec))

    def test_sse_line_event_visible_and_explicit_total_limits_remain_bounded(self):
        class Response:
            def __init__(self, payload): self.payload = payload; self.sent = False
            def read1(self, _limit):
                if self.sent: return b""
                self.sent = True; return self.payload
        def read(payload, **kwargs):
            return root_scout.read_chat_sse(Response(payload), root_scout.Budget(max_tokens=48000), {}, root_scout.time.monotonic(), **kwargs)
        with self.assertRaisesRegex(RuntimeError, "line exceeded"):
            read((b"data: " + b"x" * (root_scout.SSE_LINE_LIMIT + 1)))
        multiline = b"data: " + b"a" * 700_000 + b"\ndata: " + b"b" * 700_000 + b"\ndata: " + b"c" * 700_000 + b"\n\n"
        with self.assertRaisesRegex(RuntimeError, "event exceeded"):
            read(multiline)
        visible_chunks = []
        for _ in range(5):
            data = json.dumps({"choices":[{"delta":{"content":"v"*900_000}}]}).encode()
            visible_chunks.append(b"data: " + data + b"\n\n")
        with self.assertRaisesRegex(RuntimeError, "visible content exceeded"):
            read(b"".join(visible_chunks))
        with self.assertRaisesRegex(RuntimeError, "configured byte safety limit"):
            read(b": ping\n\n", max_bytes=2)



    def test_run_bash_protocol_validation(self):
        # Valid run_bash
        valid = {
            "candidates": [], "updates": [],
            "actions": [{
                "kind": "run_bash",
                "ref": "test-bash-1",
                "command": "python3 -c 'print(1+1)'",
                "question": "test python calc"
            }],
            "invariants": [], "next_question": "", "stop": False
        }
        res = root_scout.validate_v2_response(valid)
        self.assertEqual(res["issues"], [])
        self.assertEqual(len(res["actions"]), 1)
        self.assertEqual(res["actions"][0]["proposal_ref"], "test-bash-1")

        # Valid with optional input and candidate_refs
        valid_with_opts = {
            "candidates": [{"ref": "C1", "claim": "claim 1"}], "updates": [],
            "actions": [{
                "kind": "run_bash",
                "ref": "test-bash-2",
                "command": "cat",
                "input": "hello stdin",
                "candidate_refs": ["C1"],
                "question": "test cat stdin"
            }],
            "invariants": [], "next_question": "", "stop": False
        }
        res_opts = root_scout.validate_v2_response(valid_with_opts)
        self.assertEqual(res_opts["issues"], [])

        # Missing required command
        invalid_cmd = {
            "candidates": [], "updates": [],
            "actions": [{
                "kind": "run_bash",
                "ref": "test-bash-bad",
                "question": "missing command"
            }],
            "invariants": [], "next_question": "", "stop": False
        }
        res_bad = root_scout.validate_v2_response(invalid_cmd)
        self.assertTrue(any("required=command,kind,question,ref" in x["reason"] for x in res_bad["issues"]))

        # Command too long
        too_long = {
            "candidates": [], "updates": [],
            "actions": [{
                "kind": "run_bash",
                "ref": "test-bash-long",
                "command": "echo " + "x" * 8193,
                "question": "too long"
            }],
            "invariants": [], "next_question": "", "stop": False
        }
        res_long = root_scout.validate_v2_response(too_long)
        self.assertTrue(any("run_bash command must be a nonempty string" in x["reason"] for x in res_long["issues"]))

    def test_run_bash_in_action_history_prompt(self):
        req = {"statement": "p", "source": "int main(){return 0;}\n", "language": "cpp17", "environment": {}}
        state = {
            "tool_actions": [{
                "id": "A0001",
                "kind": "run_bash",
                "status": "accepted",
                "proposal": {"ref": "bash-check", "command": "python3 -c 'print(42)'"},
                "result": {"exit_code": 0, "stdout": "42\n", "stderr": "", "status": "ok"}
            }],
            "evidence": [], "candidates": [], "protocol_issues": [], "model_turns": [], "invariants": []
        }
        prompt = json.loads(root_scout.v2_prompt(req, state, 2))
        history = prompt.get("tool_action_history", [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["kind"], "run_bash")
        self.assertEqual(history[0]["result"]["stdout"], "42\n")


if __name__ == "__main__": unittest.main()

