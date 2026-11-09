"""Native chat/SSE capture for this isolated experiment; no command execution."""
import hashlib
import json
import os
import time
import urllib.request
from contextlib import nullcontext
from pathlib import Path


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def request(body, folder, conversation_id, timeout=90):
    folder = Path(folder) if folder is not None else None
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "request.bin").write_bytes(raw)
        save(folder / "request-meta.json", {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                                          "conversation_id": conversation_id})
    rec = {"status": "started", "request_sha256": hashlib.sha256(raw).hexdigest(), "request_bytes": len(raw)}
    started = time.monotonic()
    deadline = started + timeout
    message = {"role": "assistant", "content": "", "reasoning_content": ""}
    calls, usage, data_lines = {}, {}, []
    finish, done, events = None, False, 0
    wire_hash, wire_size = hashlib.sha256(), 0

    def dispatch():
        nonlocal data_lines, finish, done, usage, events
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines = []
        if payload.strip() == "[DONE]":
            done = True
            return
        event = json.loads(payload)
        events += 1
        if isinstance(event.get("usage"), dict):
            for key, value in event["usage"].items():
                usage[key] = {**usage.get(key, {}), **value} if isinstance(value, dict) else value
        for choice in event.get("choices") or []:
            if choice.get("index", 0) != 0:
                raise ValueError("Unexpected multiple choices")
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("refusal"):
                raise ValueError("Native response refusal")
            for field in ("content", "reasoning_content"):
                if isinstance(delta.get(field), str):
                    message[field] += delta[field]
            for part in delta.get("tool_calls") or []:
                entry = calls.setdefault(part["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    if entry["id"] and entry["id"] != part["id"]:
                        raise ValueError("Tool-call ID changed within a stream")
                    entry["id"] = part["id"]
                function = part.get("function") or {}
                if function.get("name") and function["name"] != entry["function"]["name"]:
                    entry["function"]["name"] += function["name"]
                entry["function"]["arguments"] += function.get("arguments") or ""

    try:
        base = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8788/v1").rstrip("/")
        key = os.environ.get("OPENAI_API_KEY", "root-scout-local")
        req = urllib.request.Request(base + "/chat/completions", data=raw,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                         "X-Conversation-Id": conversation_id})
        with urllib.request.urlopen(req, timeout=min(90, timeout)) as response, \
             ((folder / "response.sse").open("wb") if folder is not None else nullcontext(None)) as output:
            while not done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Native request wall limit")
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(max(0.05, remaining))
                line = response.readline(1048577)
                if not line:
                    break
                if output is not None:
                    output.write(line)
                wire_hash.update(line)
                wire_size += len(line)
                if len(line) > 1048576:
                    raise ValueError("Native SSE line limit")
                text = line.decode("utf-8").rstrip("\r\n")
                if text == "":
                    dispatch()
                elif text.startswith("data:"):
                    data_lines.append(text[5:].lstrip(" "))
                    if sum(len(x) for x in data_lines) > 2097152:
                        raise ValueError("Native SSE event limit")
        if data_lines and not done:
            dispatch()
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
        if not done or finish not in ("stop", "tool_calls"):
            raise ValueError("Incomplete native stream: " + str(finish))
        if not all(isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0
                   for k in ("prompt_tokens", "completion_tokens")):
            raise ValueError("Native stream missing final usage")
        rec["status"] = "ok"
    except Exception as exc:
        rec.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
        rec.update(wall_seconds=time.monotonic() - started, finish_reason=finish, done=done,
                   usage=usage, events=events, response_bytes=wire_size, response_sha256=wire_hash.hexdigest(),
                   reasoning_chars=len(message["reasoning_content"]),
                   reasoning_sha256=hashlib.sha256(message["reasoning_content"].encode()).hexdigest())
        if folder is not None:
            save(folder / "assistant.json", message)
            save(folder / "call.json", rec)
    return message, rec
