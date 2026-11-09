"""Lossless top-level controller-state updates; not a semantic summary."""
import copy
import json

FEEDBACK_NOTE = (
    " Controller feedback encoding: the initial user message is the full controller state. "
    "A later tool result containing controller_state replaces that state completely. "
    "A tool result containing controller_state_update replaces only the provided top-level fields; "
    "absent fields keep their previous values. Arrays are replaced as whole arrays, and null is a value, not deletion. "
    "Use the resulting current state, including its candidate IDs, evidence, versions and action results. "
    "A tool result with original_turn_request is a syntax clarification, not a state update. "
    "This encoding does not change the trust or validation status of any model claim or tool observation."
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def make_feedback(previous, current, mode):
    if not isinstance(current, dict):
        raise ValueError("Controller state must be an object")
    if previous is None or set(previous) - set(current):
        return {"controller_state": copy.deepcopy(current)}
    update = copy.deepcopy(current) if mode == "full" else {
        key: copy.deepcopy(value) for key, value in current.items()
        if key not in previous or canonical(previous[key]) != canonical(value)
    }
    reconstructed = copy.deepcopy(previous)
    reconstructed.update(update)
    if canonical(reconstructed) != canonical(current):
        raise ValueError("Controller feedback does not reconstruct exactly")
    return {"controller_state_update": update}


def apply_feedback(previous, feedback):
    if set(feedback) == {"controller_state"}:
        return copy.deepcopy(feedback["controller_state"])
    if set(feedback) != {"controller_state_update"} or previous is None:
        raise ValueError("Unexpected controller feedback")
    result = copy.deepcopy(previous)
    result.update(copy.deepcopy(feedback["controller_state_update"]))
    return result
