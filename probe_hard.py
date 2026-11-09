"""One actual hard first-turn request to disambiguate zero-reasoning trivial probes."""
import copy
import hashlib
import json
from pathlib import Path
import uuid

from native_transport import request, save

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]

TRANSPORT_NOTE = (" Transport for this experiment: submit the complete diagnostic response object through the native "
    "submit_diagnostic_step function. Its arguments must contain the same candidates, updates, actions, next_question and stop "
    "required by the protocol. The controller still validates and executes each declared action and returns the resulting state. "
    "Preserve all existing evidence and validation rules. Do not provide a diagnosis only as prose outside the function.")
STEP_TOOL = {"type": "function", "function": {"name": "submit_diagnostic_step",
    "description": "Submit candidate claims, updates and proposed actions to the existing diagnostic controller.",
    "parameters": {"type": "object", "properties": {
        "candidates": {"type": "array", "items": {"type": "object"}},
        "updates": {"type": "array", "items": {"type": "object"}},
        "actions": {"type": "array", "items": {"type": "object"}},
        "next_question": {"type": "string"}, "stop": {"type": "boolean"},
        "invariants": {"type": "array", "items": {"type": "string"}},
        "established_invariants": {"type": "array", "items": {"type": "string"}}},
        "required": ["candidates", "updates", "actions", "next_question", "stop"], "additionalProperties": False}}}


def main():
    output = BASE / "probe-hard-034"
    assert not output.exists(), "Refusing hard-probe rerun"
    batch = REPO / "validation/bash-full40-v1/batch"
    job = next(j for j in json.loads((batch / "run-plan.json").read_text(encoding="utf-8"))["jobs"] if j["job_id"] == "cc-test-row-034-bug")
    cap = batch / job["capture_path"]
    row = json.loads((cap / "requests.jsonl").read_text(encoding="utf-8").splitlines()[0])
    raw = (cap / row["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row["sha256"]
    source = json.loads(raw)
    body = copy.deepcopy(source)
    body["messages"][0]["content"] += TRANSPORT_NOTE
    body.pop("response_format", None)
    body.update(tools=[STEP_TOOL], tool_choice="auto", parallel_tool_calls=False,
                thinking={"type": "enabled"}, reasoning_effort="low", max_completion_tokens=128000,
                stream=True, stream_options={"include_usage": True})
    message, rec = request(body, output, "root-scout-native-hard-probe-" + uuid.uuid4().hex, timeout=600)
    reasoning = rec["usage"].get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    calls = message["tool_calls"]
    valid = len(calls) == 1 and calls[0]["function"]["name"] == "submit_diagnostic_step"
    parsed = None
    if valid:
        try:
            parsed = json.loads(calls[0]["function"]["arguments"])
            valid = isinstance(parsed, dict) and all(k in parsed for k in ("candidates", "updates", "actions", "next_question", "stop"))
        except json.JSONDecodeError:
            valid = False
    summary = {"source_request_sha256": row["sha256"], "user_payload_unchanged": body["messages"][1] == source["messages"][1],
               "native_step_received": valid, "reasoning_tokens": reasoning, "reasoning_chars": rec["reasoning_chars"],
               "transport_status": rec["status"], "call": rec,
               "ready_for_continuation_test": rec["status"] == "ok" and valid and reasoning > 0 and rec["reasoning_chars"] > 0,
               "tools_executed": 0, "not_a_full_diagnosis": True}
    save(output / "summary.json", summary)
    if parsed is not None:
        save(output / "step.json", parsed)
    print(json.dumps({k: summary[k] for k in ("native_step_received", "reasoning_tokens", "reasoning_chars", "transport_status", "ready_for_continuation_test")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
