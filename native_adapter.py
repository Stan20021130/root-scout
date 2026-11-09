"""Experimental transport only; the frozen controller still owns every action."""
import copy
import hashlib
import json
import os
from pathlib import Path
import time

import native_transport
from probe_hard import STEP_TOOL, TRANSPORT_NOTE
from state_feedback import FEEDBACK_NOTE, make_feedback, canonical


def make_client(core, feedback_mode="full"):
    if feedback_mode not in ("full", "delta"):
        raise ValueError("Unexpected feedback mode")
    class NativeClient(core.OpenAICompatible):
        def __init__(self, state):
            super().__init__(state)
            self.messages = None
            self.pending_id = None
            self.previous_state = None

        def ask(self, phase, prompt, budget, max_output=None, reasoning_effort_override=None,
                reasoning_route_reason=None, diagnostic_turn=None):
            cfg = core.model_budget_configuration()
            if phase not in ("EXPLORE_V2", "EXPLORE_V2_CLARIFY") or budget.max_tokens is not None or cfg["output_policy"] != "adaptive":
                return super().ask(phase, prompt, budget, max_output=max_output,
                                   reasoning_effort_override=reasoning_effort_override,
                                   reasoning_route_reason=reasoning_route_reason,
                                   diagnostic_turn=diagnostic_turn)
            if budget.remaining_wall() <= 1:
                raise core.BudgetExhausted("wall budget exhausted before native call")
            configuration = self.state.get("run_configuration", {})
            modern = configuration.get("protocol_schema_version", 0) >= 5
            system = core.system_for_phase(phase, native_tool=modern, prompt_profile=configuration.get("prompt_profile")) + TRANSPORT_NOTE + FEEDBACK_NOTE
            data = json.loads(prompt)
            normal_state = phase == "EXPLORE_V2"
            feedback = prompt
            state_hash = core.sha(canonical(data)) if normal_state else None
            if self.messages is None:
                self.messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
            else:
                if self.pending_id is None:
                    raise ValueError("No pending native tool call for controller feedback")
                if normal_state:
                    feedback = json.dumps(make_feedback(self.previous_state, data, feedback_mode), ensure_ascii=False, separators=(",", ":"))
                self.messages[0]["content"] = system
                self.messages.append({"role": "tool", "tool_call_id": self.pending_id, "content": feedback})
                self.pending_id = None
            if normal_state:
                self.previous_state = copy.deepcopy(data)
            limits = [cfg["provider_output_ceiling"]]
            for limit in (cfg["phase_output_tokens"].get(phase), max_output):
                if limit is not None:
                    limits.append(limit)
            effort = reasoning_effort_override or cfg["reasoning_effort"]
            step_tool = core.v2_native_step_tool() if modern else STEP_TOOL
            tool_choice = {"type": "function", "function": {"name": "submit_diagnostic_step"}}
            body = {"model": os.environ["OPENAI_MODEL"], "messages": copy.deepcopy(self.messages),
                    "tools": [step_tool], "tool_choice": tool_choice, "parallel_tool_calls": False,
                    "reasoning_effort": effort, "thinking": {"type": "enabled"},
                    "max_completion_tokens": min(limits), "stream": True, "stream_options": {"include_usage": True}}
            raw = json.dumps(body, ensure_ascii=False).encode()
            index = len(budget.calls)
            rec = {"phase": phase, "model": body["model"], "conversation_id": self.conversation_id,
                   "transport": "native_submit_step_with_reasoning_history", "feedback_mode": feedback_mode,
                   "controller_state_sha256": state_hash, "controller_prompt_bytes": len(prompt.encode()),
                   "feedback_sent_bytes": len(feedback.encode()), "input_reservation": len(raw),
                   "request_bytes": len(raw), "request_sha256": hashlib.sha256(raw).hexdigest(),
                   "prompt_sha256": core.sha(prompt), "system_sha256": core.sha(system),
                   "max_output_reserved": body["max_completion_tokens"], "attempt": 1,
                   "retry_of_call_index": None, "status": "started", "usage_complete": False,
                   "forwarded_reasoning_chars": sum(len(m.get("reasoning_content", "")) for m in self.messages),
                   "forwarded_assistant_messages": sum(m["role"] == "assistant" for m in self.messages)}
            budget.begin(rec)
            started = time.monotonic()
            message, call = None, {}
            content = ""
            try:
                if os.environ.get("ROOT_SCOUT_TRACE_DIR"):
                    folder = Path(os.environ["ROOT_SCOUT_TRACE_DIR"]) / "native" / f"call-{index + 1:02d}"
                else:
                    folder = None
                message, call = native_transport.request(body, folder, self.conversation_id, timeout=budget.remaining_wall())
                if folder is not None:
                    (folder / "full-controller-prompt.json").write_text(prompt, encoding="utf-8")
                rec["stream_metadata"] = {"mode": "native_sse", "done": call["done"], "finish_reason": call["finish_reason"],
                    "response_complete": call["status"] == "ok", "usage_final": call["done"] and call["finish_reason"] is not None,
                    "bytes_received": call["response_bytes"]}
                rec.update(finish_reason=call["finish_reason"], reasoning_chars=call["reasoning_chars"],
                           reasoning_sha256=call["reasoning_sha256"], stream_usage=call["usage"])
                if call["status"] != "ok":
                    raise core.IncompleteStreamError(call.get("error", "Native transport failed"))
                calls = message["tool_calls"]
                content = message.get("content") or ""
                if calls:
                    if len(calls) != 1 or calls[0]["function"]["name"] != "submit_diagnostic_step" or not calls[0]["id"]:
                        raise ValueError("Expected exactly one native submit_diagnostic_step call")
                    self.messages.append(copy.deepcopy(message))
                    self.pending_id = calls[0]["id"]
                    content = calls[0]["function"]["arguments"]
                answer, repairs = (core.v2_decode_tool_arguments(content) if modern else core.parse_model_json(content))
                if repairs:
                    rec["json_repairs"] = repairs
                if not isinstance(answer, dict):
                    raise ValueError("Native step arguments are not an object")
                if not calls:
                    if isinstance(answer, dict) and ("candidates" in answer or "actions" in answer):
                        synth_id = f"call_synth_{int(time.monotonic()*1000)}"
                        message["tool_calls"] = [{
                            "id": synth_id,
                            "type": "function",
                            "function": {"name": "submit_diagnostic_step", "arguments": content}
                        }]
                        message["content"] = ""
                        self.messages.append(copy.deepcopy(message))
                        self.pending_id = synth_id
                    elif answer.get("stop") is True and answer.get("actions") == []:
                        self.messages.append(copy.deepcopy(message))
                        self.pending_id = None
                    else:
                        raise ValueError("A native response without a tool call must be terminal JSON with no actions")
                rec["status"] = "ok"
                return answer
            except Exception as exc:
                rec.update(status="failed", error=type(exc).__name__, error_class=core.model_error_class(exc))
                raise
            finally:
                usage = call.get("usage", {})
                core.add_cache_metrics(rec, usage)
                core._record_call_usage(rec, usage, stream_final=call.get("done", False) and call.get("finish_reason") is not None)
                rec.update(wall_seconds=time.monotonic() - started, raw_content=content, raw_content_truncated=False,
                           raw_chars=len(content), raw_sha256=core.sha(content), visible_response_sha256=core.sha(content))
                budget.flush()
    return NativeClient
