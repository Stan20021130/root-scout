"""Controller closure and real per-case Bash isolation; no paid model calls."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import root_scout as core


def answer(**fields):
    return {"candidates": [], "updates": [], "actions": [], "next_question": "", "stop": True, **fields}


class ConclusionReviewTests(unittest.TestCase):
    def run_case(self, final, *, regular_turns=4, strategy="causal"):
        req=core.load_json("examples/max_oracle_demo.json")
        prompts=[]; sessions=[]

        def ask(_client, phase, prompt, budget, **_kwargs):
            data=json.loads(prompt); prompts.append(data)
            budget.add({"phase":phase,"status":"ok","confirmed_usage_tokens":1,"reserved_unknown_tokens":0,"usage_complete":True})
            if data.get("controller_mode")=="final_review":
                if isinstance(final,Exception): raise final
                return copy.deepcopy(final)
            turn=data["turn"]
            return answer(candidates=[{"kind":"defect_candidate","ref":"suspect","claim":"Possible cache update defect.",
                                       "uncertainties":["Needs a discriminating check."]}] if turn==1 else [],
                actions=[{"kind":"run_bash","ref":f"check-{turn}","candidate_refs":["suspect"],"command":"printf 'all ok'","question":"Any mismatch?"}],
                next_question="Continue checking.", stop=turn==regular_turns and regular_turns<4)

        def run_source(_src,_lang,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","returncode":0,"stdout":case.get("expected",""),"stderr":"","matched":True,"wall_seconds":0}

        def run_bash(*_args,**kwargs):
            sessions.append(kwargs.get("session"))
            return {"exit_code":0,"stdout":"all ok\n","stderr":"","status":"ok","timed_out":False}

        with patch.dict(os.environ,{},clear=True), patch.object(core,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(core,"run_case",side_effect=run_source), patch.object(core,"docker_bash_run",side_effect=run_bash), \
             patch.object(core.OpenAICompatible,"ask",new=ask):
            state=core.diagnose_v2(req,strategy=strategy)
        self.assertEqual(core.verify_saved_case_state(state),state)
        self.assertTrue(all(s is sessions[0] for s in sessions))
        self.assertFalse(sessions[0].root.exists(),"diagnosis must release the case workspace")
        return state,prompts

    def test_last_action_is_seen_before_withdrawal_and_only_active_items_are_delivered(self):
        final=answer(updates=[{"candidate_id":"C0001","disposition":"withdraw","reason":"Source invariant resolves the concern; tests found no contrary evidence."}])
        state,prompts=self.run_case(final)
        self.assertEqual(len(prompts),5)
        self.assertEqual(prompts[-1]["tool_action_history"][-1]["result"]["stdout"],"all ok\n")
        self.assertEqual(state["final_review"],{"status":"completed","turn":5})
        self.assertEqual(state["stop_reason"],"no_bug_found_within_scope")
        self.assertEqual(len(state["tool_actions"]),4)
        self.assertEqual(state["candidates"][0]["lifecycle"],"withdrawn")
        self.assertEqual(len(state["raw_candidates"]),1)
        self.assertEqual(state["candidate_sets"]["C_store"],["C0001"])
        self.assertEqual(state["candidate_sets"]["C_deliver"],[])
        self.assertEqual(core.v2_result(state)["findings"],[])

    def test_review_preserves_an_unresolved_candidate_and_its_current_uncertainties(self):
        final=answer(updates=[{"candidate_id":"C0001","disposition":"revise","reason":"The finite checks do not cover the remaining mechanism.",
                              "claim":"A remaining cache concern requires review.","uncertainties":["Uncovered long-cycle case."]}])
        state,_=self.run_case(final)
        finding=core.v2_result(state)["findings"][0]
        self.assertEqual(finding["description"],"A remaining cache concern requires review.")
        self.assertEqual(finding["status"]["uncertainties"],["Uncovered long-cycle case."])
        self.assertEqual(finding["status"]["verification"],"unverified")
        self.assertEqual(state["stop_reason"],"oracle_unresolved")

    def test_final_review_cannot_execute_or_recursively_schedule_a_command(self):
        final=answer(candidates=[{"claim":"Readable new concern remains available."}],
                     actions=[{"kind":"run_bash","ref":"forbidden","command":"touch /tmp/must-not-run","question":"try"}])
        state,prompts=self.run_case(final)
        self.assertEqual(len(prompts),5)
        self.assertEqual(len(state["tool_actions"]),4)
        self.assertEqual(state["final_review"]["status"],"invalid_response")
        self.assertEqual(state["stop_reason"],"model_protocol_incomplete")
        self.assertEqual(len(state["candidates"]),2,"readable claims survive a rejected final action")
        self.assertEqual(state["tool_action_decisions"][-1]["reason"],"final review does not execute actions")

    def test_final_budget_or_request_failure_keeps_candidates_and_reports_no_closure(self):
        for exc,status in ((core.BudgetExhausted("no remaining budget"),"skipped_budget"),(RuntimeError("request failed"),"failed")):
            with self.subTest(status=status):
                state,prompts=self.run_case(exc)
                self.assertEqual(len(prompts),5)
                self.assertEqual(state["final_review"]["status"],status)
                self.assertEqual(core.v2_result(state)["findings"][0]["status"]["conclusion_review"],status)
                self.assertEqual(state["candidates"][0]["lifecycle"],"open")

    def test_early_stop_with_actions_still_gets_one_review_and_one_shot_does_not(self):
        state,prompts=self.run_case(answer(),regular_turns=1)
        self.assertEqual(len(prompts),2)
        self.assertEqual(state["final_review"],{"status":"completed","turn":2})
        state,prompts=self.run_case(answer(),regular_turns=1,strategy="one_shot")
        self.assertEqual(len(prompts),1)
        self.assertEqual(state["final_review"]["status"],"not_needed")

    def test_recomputed_hash_cannot_hide_tool_execution_in_the_final_review(self):
        state,_=self.run_case(answer())
        forged=copy.deepcopy(state); forged["tool_actions"][-1]["turn"]=5; forged["integrity"]=core.v2_integrity(forged)
        with self.assertRaisesRegex(ValueError,"final review must never execute"):
            core.verify_saved_case_state(forged)

    def test_closing_prompt_contains_archived_scripts_and_marks_clipped_output(self):
        state,_=self.run_case(answer())
        command="printf check\n"+"# original tester\n"*100
        state["tool_actions"][0]["proposal"]["command"]=command
        state["tool_actions"][0]["result"]["stdout"]="x"*5000
        prompt=json.loads(core.v2_final_review_prompt(state["request_snapshot"],state,6))
        first=prompt["tool_action_history"][0]
        self.assertEqual(first["proposal"]["command"],command)
        self.assertNotIn("command_artifact",first)
        self.assertTrue(first["result"]["stdout_truncated"])

    def test_replay_of_protocol_three_keeps_its_original_delivery_contract(self):
        old,_=self.run_case(answer(),regular_turns=1,strategy="one_shot")
        old["run_configuration"]["protocol_schema_version"]=3
        old.pop("final_review")
        old["integrity"]=core.v2_integrity(old)
        core.verify_saved_case_state(old)
        def runner(_src,_lang,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":case.get("expected",""),"stderr":"","matched":True,"returncode":0,"wall_seconds":0}
        with patch.dict(os.environ,{},clear=True), patch.object(core,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(core,"run_case",side_effect=runner), patch.object(core,"docker_bash_run",return_value={"status":"ok","exit_code":0,"stdout":"all ok\n","stderr":"","timed_out":False}), \
             patch.object(core.OpenAICompatible,"ask",side_effect=AssertionError("replay must not call a model")):
            replayed=core.diagnose_v2(old["request_snapshot"],provider="replay",replay_state=old)
        self.assertEqual(replayed["run_configuration"]["protocol_schema_version"],3)
        self.assertNotIn("final_review",replayed)
        self.assertEqual(core.verify_saved_case_state(replayed),replayed)

    def test_failed_bash_keeps_exit_status_and_stderr_in_model_feedback(self):
        req={"statement":"task","source":"print(1)","language":"python3"}
        state={"tool_actions":[],"evidence":[],"candidates":[],"protocol_issues":[],"model_turns":[],
               "run_configuration":{"protocol_schema_version":4}}
        action={"turn":1,"item":{"kind":"run_bash","ref":"missing","command":"/tmp/sol","question":"run"}}
        result={"exit_code":127,"stdout":"partial","stderr":"/tmp/sol: not found","timed_out":False,"status":"command_failed",
                "workspace_generation":2,"workspace_reset":False}
        with patch.object(core,"docker_bash_run",return_value=result):
            record=core.v2_execute_action(req,state,action,{}, {}, {},None,None)
        self.assertEqual(record["status"],"blocked")
        shown=json.loads(core.v2_prompt(req,state,2))["tool_action_history"][0]["result"]
        self.assertEqual(shown["exit_code"],127)
        self.assertEqual(shown["stderr"],"/tmp/sol: not found")
        self.assertEqual(shown["status"],"command_failed")
        self.assertEqual(shown["workspace_generation"],2)


class NativeConclusionReviewTests(unittest.TestCase):
    def test_delta_feedback_and_real_history_shape_reach_a_terminal_review(self):
        native=Path(__file__).resolve().parent.parent/"validation/system-reliability-v1/native"
        if not (native/"native_adapter.py").is_file():
            self.skipTest(f"Historical native adapter not installed: {native}")
        sys.path.insert(0,str(native))
        try:
            spec=importlib.util.spec_from_file_location("fixed_native_adapter",native/"native_adapter.py")
            adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
            from state_feedback import apply_feedback
        finally:
            sys.path.pop(0)
        seen=[]; previous=[]; current=None
        def request(body,folder,_conversation_id,timeout):
            nonlocal current
            Path(folder).mkdir(parents=True)
            self.assertEqual([m for m in body["messages"] if m["role"]=="assistant"],previous)
            last=body["messages"][-1]
            if not seen:
                current=json.loads(last["content"])
                response=answer(candidates=[{"ref":"concern","kind":"defect_candidate","claim":"Possible cache error."}],
                    actions=[{"kind":"run_bash","ref":"check","candidate_refs":["concern"],"command":"printf 'all ok'","question":"check"}])
                message={"role":"assistant","content":"","reasoning_content":"fixture reasoning 1",
                    "tool_calls":[{"id":"call-check-1","type":"function","function":{"name":"submit_diagnostic_step","arguments":json.dumps(response)}}]}
            else:
                self.assertEqual(last["tool_call_id"],"call-check-1")
                current=apply_feedback(current,json.loads(last["content"]))
                self.assertEqual(current["controller_mode"],"final_review")
                self.assertEqual(current["tool_action_history"][-1]["result"]["stdout"],"all ok\n")
                response=answer(updates=[{"candidate_id":"C0001","disposition":"withdraw","reason":"Source reasoning resolves the concern."}])
                message={"role":"assistant","content":json.dumps(response),"reasoning_content":"fixture reasoning 2","tool_calls":[]}
            seen.append(copy.deepcopy(body));previous.append(copy.deepcopy(message))
            call={"status":"ok","done":True,"finish_reason":"tool_calls" if message["tool_calls"] else "stop",
                  "reasoning_chars":len(message["reasoning_content"]),"reasoning_sha256":hashlib.sha256(message["reasoning_content"].encode()).hexdigest(),
                  "usage":{"prompt_tokens":20,"completion_tokens":10,"total_tokens":30,"completion_tokens_details":{"reasoning_tokens":4},
                           "prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":20},"response_bytes":100}
            return message,call
        def runner(_src,_lang,case,_env,profile="formal",build_cache=None):
            return {"status":"ok","stdout":case.get("expected",""),"stderr":"","matched":True,"returncode":0,"wall_seconds":0}
        cls=adapter.make_client(core,feedback_mode="delta")
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"ROOT_SCOUT_TRACE_DIR":td,"OPENAI_MODEL":"fixture","OPENAI_API_KEY":"fixture",
             "ROOT_SCOUT_MODEL_OUTPUT_LIMIT":"128000","ROOT_SCOUT_OUTPUT_POLICY":"adaptive","OPENAI_REASONING_EFFORT":"low"},clear=True), \
             patch.object(adapter.native_transport,"request",side_effect=request), patch.object(core,"OpenAICompatible",cls), \
             patch.object(core,"docker_preflight",return_value={"available":True,"image_id":"sha256:test"}), \
             patch.object(core,"run_case",side_effect=runner), \
             patch.object(core,"docker_bash_run",return_value={"status":"ok","exit_code":0,"stdout":"all ok\n","stderr":"","timed_out":False}):
            state=core.diagnose_v2(core.load_json("examples/max_oracle_demo.json"))
        self.assertEqual(len(seen),2,state.get("analysis_errors"))
        self.assertEqual(state["final_review"]["status"],"completed")
        self.assertEqual(core.verify_saved_case_state(state),state)
        self.assertEqual(core.v2_result(state)["findings"],[])
        self.assertTrue(state["budget"]["usage_complete"])


@unittest.skipUnless(os.environ.get("ROOT_SCOUT_INTEGRATION")=="1","requires real Docker")
class BashWorkspaceTests(unittest.TestCase):
    def test_persistence_isolation_failures_limits_and_cleanup(self):
        source='#include <iostream>\nint main(){int x;std::cin>>x;std::cout<<x+1<<"\\n";}'
        first,other=core.BashSession(),core.BashSession()
        names=[]
        def run(command,session=first,data="",timeout=10):
            result=core.docker_bash_run(source,"cpp17",command,data,timeout=timeout,session=session)
            if session.name and session.name not in names: names.append(session.name)
            return result
        try:
            built=run("g++ -O2 /work/solution.cpp -o /tmp/sol && printf kept > /tmp/data")
            self.assertEqual(built["status"],"ok",built)
            checked=run("cat /tmp/data; /tmp/sol",data="41\n")
            self.assertEqual(checked["stdout"],"kept42\n",checked)
            self.assertEqual(checked["workspace_generation"],1)
            isolated=run("test ! -e /tmp/sol && test ! -e /tmp/data",other)
            self.assertEqual(isolated["status"],"ok",isolated)
            failed=run("printf deliberate-error >&2; exit 7")
            self.assertEqual((failed["status"],failed["exit_code"]),("command_failed",7))
            self.assertIn("deliberate-error",failed["stderr"])
            self.assertFalse(failed["workspace_reset"])
            self.assertEqual(run("/tmp/sol",data="9\n")["stdout"],"10\n")
            inspected=subprocess.run(["docker","inspect",first.name],capture_output=True,text=True,check=True,timeout=10)
            config=json.loads(inspected.stdout)[0]
            self.assertTrue(config["HostConfig"]["ReadonlyRootfs"])
            self.assertEqual(config["HostConfig"]["NetworkMode"],"none")
            self.assertEqual(config["Config"]["User"],"65534:65534")
            self.assertIn("ALL",config["HostConfig"]["CapDrop"])
            self.assertEqual(config["HostConfig"]["Memory"],512*1024*1024)
            self.assertIn("size=64m",config["HostConfig"]["Tmpfs"]["/tmp"])
            self.assertTrue(config["HostConfig"]["AutoRemove"])
            self.assertEqual(config["Config"]["Cmd"][0],"sleep")
            self.assertTrue(0<float(config["Config"]["Cmd"][1])<=core.MAX_WALL_SECONDS)
            ulimits={item["Name"]:item["Soft"] for item in config["HostConfig"]["Ulimits"]}
            self.assertEqual(ulimits.get("stack"),core.SANDBOX_STACK_LIMIT_BYTES)
            self.assertEqual(ulimits.get("cpu"),core.SANDBOX_BASH_CPU_SECONDS)
            self.assertNotEqual(run("echo changed > /work/solution.cpp")["exit_code"],0)
            self.assertEqual((first.root/"solution.cpp").read_text(encoding="utf-8"),source)
            flooded=run("python3 -c 'import os;\nwhile True: os.write(1,b\"x\"*4096)'")
            self.assertEqual(flooded["status"],"truncated",flooded)
            self.assertLessEqual(len(flooded["stdout"].encode()),core.SANDBOX_STDOUT_LIMIT_BYTES)
            self.assertTrue(flooded["workspace_reset"])
            self.assertIsNone(first.name)
            rebuilt=run("test ! -e /tmp/sol && printf reset")
            self.assertEqual(rebuilt["stdout"],"reset")
            self.assertEqual(rebuilt["workspace_generation"],2)
            timed=run("sleep 60 & wait",timeout=.4)
            self.assertEqual(timed["status"],"timeout",timed)
            # The cap is enforced inside the container, so a timeout no longer
            # destroys the workspace; the generation stays and /tmp survives.
            self.assertFalse(timed["workspace_reset"],timed)
            self.assertEqual(timed["workspace_generation"],rebuilt["workspace_generation"])
            self.assertEqual(run("printf alive")["stdout"],"alive")
        finally:
            first.close();other.close()
        self.assertFalse(first.root.exists())
        for name in names:
            proc=subprocess.run(["docker","container","inspect",name],capture_output=True,timeout=10)
            self.assertNotEqual(proc.returncode,0,"sandbox must be removed on close/reset")


if __name__=="__main__": unittest.main()
