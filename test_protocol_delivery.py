"""Protocol recovery and lightweight observations; model/tool boundaries remain strict."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import root_scout as core


def answer(**fields):
    return {"candidates":[],"updates":[],"actions":[],"next_question":"","stop":True,**fields}


class ProtocolDeliveryTests(unittest.TestCase):
    def diagnose(self,responses):
        supplied=iter(responses); prompts=[]
        def ask(_client,phase,prompt,budget,**kwargs):
            prompts.append(json.loads(prompt))
            budget.add({"phase":phase,"status":"ok","confirmed_usage_tokens":1,
                        "reserved_unknown_tokens":0,"usage_complete":True})
            return copy.deepcopy(next(supplied))
        def runner(_src,_lang,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","returncode":0,"stdout":case.get("expected",""),
                    "stderr":"","matched":True,"wall_seconds":0}
        with patch.dict(os.environ,{},clear=True), patch.object(core,"docker_preflight",return_value={"available":True,"image_id":"fixture"}), \
             patch.object(core,"run_case",side_effect=runner), patch.object(core.OpenAICompatible,"ask",new=ask), \
             patch.object(core,"docker_bash_run",side_effect=AssertionError("Malformed requests must never execute")):
            state=core.diagnose_v2(core.load_json("examples/max_oracle_demo.json"))
        core.verify_saved_case_state(state)
        return state,prompts

    def test_explicit_recovery_preserves_original_error_and_candidate_history(self):
        bad=answer(candidates=[{"ref":"c","claim":"A readable concern."}],
                   actions=[{"ref":"bad","command":"echo must-not-run","question":"check"}])
        fixed=answer(updates=[{"candidate_id":"C0001","disposition":"withdraw","reason":"Source reasoning resolves this concern."}],
            protocol_recovery={"issue_turns":[1],"reason":"Discarded the malformed tester; no successful execution is claimed."})
        state,prompts=self.diagnose([bad,fixed])
        self.assertEqual(len(prompts),2)
        self.assertIn("protocol_recovery",str(prompts[1]["controller_notes"]))
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(core.v2_protocol_state(state)["status"],"recovered")
        self.assertEqual(len(state["protocol_issues"]),1)
        self.assertEqual(state["tool_actions"],[])
        self.assertEqual(len(state["raw_candidates"]),1)
        forged=copy.deepcopy(state);forged["protocol_issues"]=[];forged["integrity"]=core.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"protocol issues do not recompute"):
            core.verify_saved_case_state(forged)

    def test_a_valid_later_response_does_not_silently_erase_an_unacknowledged_error(self):
        bad=answer(actions=[{"kind":"run_bash","command":"echo x","question":"missing ref"}])
        state,_=self.diagnose([bad,answer(),answer()])
        self.assertEqual(state["stop_reason"],"model_protocol_incomplete")
        self.assertEqual(core.v2_protocol_state(state)["status"],"unresolved")

    def test_unknown_recovery_and_privilege_fields_are_rejected(self):
        parsed=core.v2_validate_response_for_state({"protocol_issues":[]},answer(
            protocol_recovery={"issue_turns":[1],"reason":"pretend recovered"}),2,5)
        self.assertTrue(any(i["part"]=="protocol_recovery" for i in parsed["issues"]))
        action={"kind":"run_original","ref":"r","question":"observe","input":"1\n","candidate_refs":["c"]}
        self.assertFalse(core.validate_v2_response(answer(actions=[action]),5)["issues"])
        self.assertTrue(core.validate_v2_response(answer(actions=[dict(action,expected="7")]),5)["issues"])
        self.assertTrue(core.validate_v2_response(answer(actions=[action]),4)["issues"],"Old protocol remains strict")

    def test_only_an_unambiguous_single_envelope_is_unwrapped(self):
        payload=answer(candidates=[{"claim":"Keep this exact readable claim."}])
        result,repairs=core.v2_decode_tool_arguments(json.dumps({"arguments":json.dumps(payload)}))
        self.assertEqual(result,payload)
        self.assertIn("unwrap_single_arguments_envelope",repairs)
        ambiguous={"arguments":json.dumps(payload),"stop":False}
        self.assertEqual(core.v2_decode_tool_arguments(json.dumps(ambiguous))[0],ambiguous)

    def test_one_unmatched_envelope_brace_is_repaired_without_discarding_another_payload(self):
        payload=answer(candidates=[{"kind":"defect_candidate","claim":"An actual defect survives serialization."}])
        result,repairs=core.v2_decode_tool_arguments(json.dumps({"arguments":json.dumps(payload)+"}"}))
        self.assertEqual(result,payload)
        self.assertIn("remove_single_extra_closing_brace_in_envelope",repairs)
        for suffix in ('} {"candidates":[]}', '} trailing commentary', '}}'):
            with self.subTest(suffix=suffix),self.assertRaises(json.JSONDecodeError):
                core.v2_decode_tool_arguments(json.dumps({"arguments":json.dumps(payload)+suffix}))

    def test_native_schema_covers_required_nested_action_and_update_keys(self):
        tool=core.v2_native_step_tool()["function"]["parameters"]["properties"]
        actions=tool["actions"]["items"]["oneOf"]
        for schema in actions:
            kind=schema["properties"]["kind"]["enum"][0]
            self.assertEqual(schema["required"],core.v2_protocol_spec()["actions"][kind]["required"])
            self.assertFalse(schema["additionalProperties"])
        self.assertTrue(all("disposition" in s["required"] and "reason" in s["required"] for s in tool["updates"]["items"]["oneOf"]))
        meanings=core.v2_protocol_spec()["candidate_kind_meanings"]
        self.assertIn("untested suspicion",meanings["defect_candidate"])
        self.assertIn("positive",meanings["correctness_coverage"])

    def projection_state(self):
        candidate={"id":"C0001","record_schema_version":3,"candidate_kind":"defect_candidate","lifecycle":"open",
            "current_claim":"The comparison is wrong.","trigger_claim":"negative input","source_location_claims":["line 3"],
            "uncertainties":[],"current_evidence_ids":[],"revisions":[],
            "evidence_links":[{"action_id":"A0001","evidence_ids":[],"association_kind":"joint_candidates"}]}
        return {"run_configuration":{"protocol_schema_version":5},"protocol_issues":[],"model_turns":[],
            "final_review":{"status":"not_needed"},"evidence":[],"candidates":[candidate],
            "tool_actions":[{"id":"A0001","kind":"run_bash","turn":1,"proposal":{"command":"echo model-expected"},
                "result":{"exit_code":137,"status":"timeout","stdout":"model expected = 99\n","stderr":"","timed_out":True}}]}

    def test_bash_facts_are_exported_without_trusting_their_expected_or_timeout(self):
        state=self.projection_state();original=copy.deepcopy(state)
        finding=core.v2_result(state)["findings"][0]
        record=finding["evidence"][0]
        self.assertEqual(record["status"],"timeout")
        self.assertEqual(record["association"],"joint_candidates")
        self.assertFalse(record["trusted_expected"])
        self.assertNotIn("expected",record)
        self.assertEqual(finding["status"]["verification"],"unverified")
        self.assertEqual(state,original)
        accepted,ids=core.v2_resolve_model_evidence(state,["A0001","A9999"],2,"candidate")
        self.assertEqual(ids,["A0001"]);self.assertEqual(len(accepted["rejected"]),1)
        self.assertEqual(core.v2_resolve_model_evidence(state,["A0001"],1,"candidate")[1],[])

    def test_exact_duplicates_fold_only_in_view_and_different_claims_survive(self):
        state=self.projection_state();second=copy.deepcopy(state["candidates"][0]);second["id"]="C0002"
        state["candidates"].append(second)
        view=core.v2_result(state)["findings"]
        self.assertEqual(len(view),1)
        self.assertEqual(view[0]["status"]["duplicate_candidate_ids"],["C0002"])
        self.assertEqual(len(state["candidates"]),2)
        state["candidates"][1]["current_claim"]="A distinct overflow defect."
        self.assertEqual(len(core.v2_result(state)["findings"]),2)

    def test_a_revision_cannot_silently_reuse_a_stale_trigger_and_location(self):
        state=self.projection_state();candidate=state["candidates"][0]
        candidate["revisions"]=[{"turn":2,"claim":"A single element now suffices.","reason":"new observation"}]
        candidate["current_claim"]="A single element now suffices."
        finding=core.v2_result(state)["findings"][0]
        self.assertIsNone(finding["trigger"]);self.assertEqual(finding["location"],[])
        self.assertEqual(finding["status"]["fields_missing_from_latest_revision"],["trigger","location"])
        self.assertEqual(candidate["trigger_claim"],"negative input","Original history remains available")
        state["run_configuration"]["protocol_schema_version"]=4
        self.assertEqual(core.v2_result(state)["findings"][0]["trigger"],"negative input")

    def test_complete_fields_are_not_lost_when_another_candidate_has_a_partial_revision(self):
        state=self.projection_state();first=state["candidates"][0]
        second=copy.deepcopy(first);second["id"]="C0002"
        first["revisions"]=[{"turn":2,"claim":first["current_claim"],"reason":"partial revision"}]
        state["candidates"].append(second)
        findings=core.v2_result(state)["findings"]
        self.assertEqual(len(findings),2)
        self.assertIsNone(findings[0]["trigger"])
        self.assertEqual(findings[1]["trigger"],"negative input")

    def test_lightweight_view_caps_observations_and_keeps_near_duplicates(self):
        state=self.projection_state();candidate=state["candidates"][0]
        for i in range(2,8):
            action=copy.deepcopy(state["tool_actions"][0]);action["id"]=f"A{i:04d}"
            state["tool_actions"].append(action)
            candidate["evidence_links"].append({"action_id":action["id"],"evidence_ids":[],"association_kind":"single_candidate"})
        other=copy.deepcopy(candidate);other["id"]="C0002";other["current_claim"]+=" Another explanation of this location."
        state["candidates"].append(other)
        findings=core.v2_result(state)["findings"]
        self.assertEqual(len(findings),2)
        self.assertEqual(len(findings[0]["evidence"]),4)
        self.assertEqual(findings[0]["status"]["evidence_records_omitted"],3)
        self.assertEqual(findings[0]["status"]["possible_duplicate_candidate_ids"],["C0002"])
        self.assertEqual(len(state["tool_actions"]),7)

    def test_content_diagnostic_json_without_tool_call_is_synthesized_into_tool_step(self):
        folder=Path(__file__).resolve().parent/"validation/protocol-delivery-v1/native"
        if not (folder/"native_adapter.py").is_file():
            self.skipTest(f"Historical native adapter not installed: {folder}")
        sys_path=list(sys.path)
        try:
            sys.path.insert(0,str(folder))
            spec=importlib.util.spec_from_file_location("delivery_native_adapter",folder/"native_adapter.py")
            adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
            payload=answer(actions=[{"kind":"read_source","ref":"r1","line_start":1,"line_end":5,"question":"q"}],stop=False)
            raw=json.dumps(payload)
            def request(body,_folder,_conversation,timeout=90):
                Path(_folder).mkdir(parents=True)
                message={"role":"assistant","content":raw,"reasoning_content":"fixture reasoning","tool_calls":[]}
                return message,{"status":"ok","done":True,"finish_reason":"stop","response_bytes":len(raw),
                    "reasoning_chars":17,"reasoning_sha256":core.sha("fixture reasoning"),
                    "usage":{"prompt_tokens":20,"completion_tokens":10}}
            state={"id":"fixture","run_configuration":{"protocol_schema_version":5}}
            with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{"ROOT_SCOUT_TRACE_DIR":td,"OPENAI_MODEL":"fixture",
                "OPENAI_API_KEY":"fixture","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","ROOT_SCOUT_OUTPUT_POLICY":"adaptive"},clear=True), \
                patch.object(adapter.native_transport,"request",side_effect=request):
                client=adapter.make_client(core,feedback_mode="delta")(state)
                budget=core.Budget(time.monotonic(),max_tokens=None)
                result=client.ask("EXPLORE_V2","{}",budget)
            self.assertEqual(result,payload)
            self.assertIsNotNone(client.pending_id)
            self.assertTrue(client.pending_id.startswith("call_"))
            self.assertEqual(client.messages[-1]["tool_calls"][0]["function"]["name"],"submit_diagnostic_step")
            self.assertEqual(client.messages[-1]["tool_calls"][0]["id"],client.pending_id)
        finally:
            sys.path[:]=sys_path

    def test_native_adapter_uses_nested_schema_and_preserves_original_wire_arguments(self):
        folder=Path(__file__).resolve().parent/"validation/protocol-delivery-v1/native"
        if not (folder/"native_adapter.py").is_file():
            self.skipTest(f"Historical native adapter not installed: {folder}")
        sys_path=list(sys.path)
        try:
            sys.path.insert(0,str(folder))
            spec=importlib.util.spec_from_file_location("delivery_native_adapter",folder/"native_adapter.py")
            adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
            raw=json.dumps({"arguments":json.dumps(answer())});seen=[]
            def request(body,_folder,_conversation,timeout=90):
                Path(_folder).mkdir(parents=True)
                seen.append(body)
                message={"role":"assistant","content":"","reasoning_content":"fixture reasoning",
                         "tool_calls":[{"id":"fixture-call","type":"function","function":{"name":"submit_diagnostic_step","arguments":raw}}]}
                return message,{"status":"ok","done":True,"finish_reason":"tool_calls","response_bytes":50,
                    "reasoning_chars":17,"reasoning_sha256":core.sha("fixture reasoning"),
                    "usage":{"prompt_tokens":20,"completion_tokens":10,"completion_tokens_details":{"reasoning_tokens":4}}}
            state={"id":"fixture","run_configuration":{"protocol_schema_version":5}}
            with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{"ROOT_SCOUT_TRACE_DIR":td,"OPENAI_MODEL":"fixture",
                "OPENAI_API_KEY":"fixture","ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","ROOT_SCOUT_OUTPUT_POLICY":"adaptive"},clear=True), \
                patch.object(adapter.native_transport,"request",side_effect=request):
                client=adapter.make_client(core,feedback_mode="delta")(state)
                budget=core.Budget(time.monotonic(),max_tokens=None)
                result=client.ask("EXPLORE_V2","{}",budget)
            self.assertEqual(result,answer())
            self.assertIn("oneOf",seen[0]["tools"][0]["function"]["parameters"]["properties"]["actions"]["items"])
            self.assertEqual(client.messages[-1]["tool_calls"][0]["function"]["arguments"],raw)
            self.assertEqual(budget.calls[0]["raw_content"],raw)
            self.assertIn("unwrap_single_arguments_envelope",budget.calls[0]["json_repairs"])
        finally:
            sys.path[:]=sys_path


class SchemaClarificationStopTests(unittest.TestCase):
    """A second repair attempt, and a stop that is not held hostage by format noise."""

    def diagnose(self,responses):
        supplied=iter(responses); prompts=[]
        def ask(_client,phase,prompt,budget,**kwargs):
            prompts.append((phase,json.loads(prompt)))
            budget.add({"phase":phase,"status":"ok","confirmed_usage_tokens":1,
                        "reserved_unknown_tokens":0,"usage_complete":True})
            return copy.deepcopy(next(supplied))
        def runner(_src,_lang,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","returncode":0,"stdout":case.get("expected",""),
                    "stderr":"","matched":True,"wall_seconds":0}
        with patch.dict(os.environ,{"ROOT_SCOUT_SCHEMA_CLARIFY":"1"},clear=True), \
             patch.object(core,"docker_preflight",return_value={"available":True,"image_id":"fixture"}), \
             patch.object(core,"run_case",side_effect=runner), patch.object(core.OpenAICompatible,"ask",new=ask), \
             patch.object(core,"docker_bash_run",side_effect=AssertionError("Malformed requests must never execute")):
            state=core.diagnose_v2(core.load_json("examples/max_oracle_demo.json"))
        core.verify_saved_case_state(state)
        return state,prompts

    def test_a_stop_after_a_repair_attempt_is_not_a_protocol_failure(self):
        # Turn 1 drops a malformed tester, the clarify replies and acknowledges turn 1.
        first=answer(actions=[{"kind":"run_bash","command":"echo must-not-run","question":"missing ref"}],stop=False)
        acknowledged=answer(protocol_recovery={"issue_turns":[1],"reason":"Discarded the malformed tester."},
                            next_question="Which path fixes the seed?",stop=False)
        # Turn 2 drops another tester, is clarified once more, and then stops.
        second=answer(actions=[{"kind":"run_bash","command":"echo must-not-run","question":"missing ref again"}],stop=True)
        clean_stop=answer()
        state,prompts=self.diagnose([first,acknowledged,second,clean_stop])
        self.assertEqual([phase for phase,_ in prompts],["EXPLORE_V2","EXPLORE_V2_CLARIFY","EXPLORE_V2","EXPLORE_V2_CLARIFY"])
        self.assertEqual([x["status"] for x in state["protocol_clarifications"]],["recovered","recovered"])
        # The terminal issue is kept in the ledger, but it no longer blocks the verdict.
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(core.v2_protocol_state(state)["unresolved_issue_turns"],[2])
        self.assertEqual(core.v2_tolerated_issue_turns(state),[2])
        self.assertEqual(core.v2_incomplete_protocol(state),[])
        self.assertEqual(len(state["protocol_issues"]),2)
        self.assertIn("收尾时容忍",core.markdown(state))

    def test_a_third_schema_issue_is_recorded_without_a_third_call(self):
        bad=lambda stop: answer(actions=[{"kind":"run_bash","command":"echo must-not-run","question":"missing ref"}],
                                next_question="" if stop else "Keep probing.",stop=stop)
        keep=lambda turn: answer(protocol_recovery={"issue_turns":[turn],"reason":"Discarded the malformed tester."},
                                 next_question="Keep probing.",stop=False)
        state,prompts=self.diagnose([bad(False),keep(1),bad(False),keep(2),bad(True)])
        self.assertEqual(len(prompts),5)
        self.assertEqual(len(state["protocol_clarifications"]),2)
        self.assertEqual(len(state["protocol_issues"]),3)
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(core.v2_tolerated_issue_turns(state),[3])


@unittest.skipUnless(os.environ.get("ROOT_SCOUT_INTEGRATION")=="1","requires real Docker")
class BashProgressTests(unittest.TestCase):
    def test_timeout_retains_flushed_python_progress_and_keeps_the_workspace(self):
        session=core.BashSession();names=[]
        try:
            source="print(0)\n"
            result=core.docker_bash_run(source,"python3","python3 -c 'import os; print(os.environ.get(\"PYTHONUNBUFFERED\"))'",session=session)
            self.assertEqual(result["stdout"],"1\n",result)
            names.append(session.name)
            result=core.docker_bash_run(source,"python3","python3 -c 'import time; print(\"PROGRESS\"); time.sleep(5)'",timeout=1,session=session)
            self.assertEqual(result["status"],"timeout",result)
            self.assertIn("PROGRESS",result["stdout"])
            # The cap is enforced inside the container, so the process dies but the
            # session workspace survives and the next call keeps its generation.
            self.assertFalse(result["workspace_reset"],result)
            self.assertEqual(result["timeout_scope"],"process",result)
            after=core.docker_bash_run(source,"python3","python3 -c 'print(\"ALIVE\")'",session=session)
            self.assertEqual(after["status"],"ok",after)
            self.assertEqual(after["stdout"],"ALIVE\n")
            self.assertEqual(after["workspace_generation"],result["workspace_generation"])
        finally:
            session.close()
        for name in names:
            self.assertNotEqual(subprocess.run(["docker","container","inspect",name],capture_output=True,timeout=10).returncode,0)


if __name__=="__main__": unittest.main()
