import io
import json
import os
import re
import unittest
from unittest.mock import patch

import root_scout


def dsml(command, input_text="", marker="｜｜DSML｜｜ "):
    def tag(name, closing=False):
        return "<" + ("/" if closing else "") + marker + name + ">"
    return (tag("calls") + tag('invoke name="run_bash"')
            + tag('parameter name="ref"') + "probe" + tag("parameter", True)
            + tag('parameter name="command"') + command + tag("parameter", True)
            + tag('parameter name="input"') + input_text + tag("parameter", True)
            + tag('parameter name="question"') + "check operators" + tag("parameter", True)
            + tag("invoke", True) + tag("calls", True))


class SessionAffinityDsmlTests(unittest.TestCase):
    def test_same_case_keeps_header_across_phases_and_reconstruction(self):
        answer = {"candidates": [], "updates": [], "actions": [], "next_question": "", "stop": True}
        events = [{"choices": [{"delta": {"content": json.dumps(answer)}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}]
        wire = ("".join("data: " + json.dumps(x) + "\n\n" for x in events) + "data: [DONE]\n\n").encode()

        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()

        requests = []
        def open_request(request, **_kwargs):
            requests.append(request)
            return Response(wire)

        settings = {"OPENAI_API_KEY": "fixture", "OPENAI_MODEL": "fixture",
                    "OPENAI_BASE_URL": "http://mock/v1", "ROOT_SCOUT_OUTPUT_POLICY": "fixed"}
        with patch.dict(os.environ, settings, clear=True), patch.object(root_scout.urllib.request, "urlopen", side_effect=open_request):
            budget = root_scout.Budget(max_tokens=48000)
            model = root_scout.OpenAICompatible({"id": "case-one\r\nunsafe-header-text"})
            for phase in ("EXPLORE_V2", "EXPLORE_V2_CLARIFY"):
                self.assertEqual(model.ask(phase, "{}", budget, max_output=128), answer)
            root_scout.OpenAICompatible({"id": "case-one\r\nunsafe-header-text"}).ask("EXPLORE_V2", "{}", budget, max_output=128)
            root_scout.OpenAICompatible({"id": "case-two"}).ask("EXPLORE_V2", "{}", budget, max_output=128)
        headers = [dict((k.lower(), v) for k, v in r.header_items()) for r in requests]
        keys = [h["x-conversation-id"] for h in headers]
        self.assertEqual(len(set(keys[:3])), 1)
        self.assertNotEqual(keys[0], keys[3])
        self.assertRegex(keys[0], r"^root-scout-[0-9a-f]{32}$")
        self.assertEqual([c["conversation_id"] for c in budget.calls], keys)
        for request in requests:
            body = json.loads(request.data)
            self.assertEqual(body["messages"][1], {"role": "user", "content": "{}"})
            self.assertNotIn("conversation_id", body)

    def test_missing_state_id_uses_one_key_per_client(self):
        one, two = root_scout.OpenAICompatible({}), root_scout.OpenAICompatible({})
        self.assertNotEqual(one.conversation_id, two.conversation_id)
        self.assertIsNotNone(re.fullmatch(r"root-scout-[0-9a-f]{32}", one.conversation_id))

    def test_dsml_marker_variants_preserve_shell_and_stdin_bytes(self):
        command = "\ncat <<'PY' | python3\nprint(1 | 2)\nprint('left｜right')\nPY\nfalse || printf 'fallback\\n'\n"
        input_text = "\n  a|b ｜\r\n"
        for marker in ("｜｜DSML｜｜ ", "｜DSML｜", "|DSML| ", "DSML ", ""):
            with self.subTest(marker=marker):
                parsed, repairs = root_scout.parse_model_json(dsml(command, input_text, marker))
                self.assertEqual(parsed["actions"][0]["command"].encode(), command.encode())
                self.assertEqual(parsed["actions"][0]["input"].encode(), input_text.encode())
                self.assertEqual(repairs, ["dsml_to_json_fallback"])

    def test_simple_tool_tags_preserve_whitespace_and_operators(self):
        command, stdin = "\n printf x | cat || true\r\n", "  1 | 2\n"
        raw = "<run_bash><command>" + command + "</command><input>" + stdin + "</input></run_bash>"
        parsed, _ = root_scout.parse_model_json(raw)
        self.assertEqual(parsed["actions"][0]["command"], command)
        self.assertEqual(parsed["actions"][0]["input"], stdin)

    def test_json_looking_command_and_input_remain_strings(self):
        parsed, _ = root_scout.parse_model_json(dsml('{"a":"b|c"}', '["x｜y"]\n'))
        self.assertEqual(parsed["actions"][0]["command"], '{"a":"b|c"}')
        self.assertEqual(parsed["actions"][0]["input"], '["x｜y"]\n')

    def test_structured_action_array_and_json_path_preserve_values(self):
        actions = [{"kind": "run_bash", "ref": "array", "command": "printf 'x|y｜z' | cat", "input": "\n|\r\n", "question": "check"}]
        raw = ('<｜DSML｜invoke name="actions"><｜DSML｜parameter name="actions_list">'
               + json.dumps(actions, ensure_ascii=False)
               + '</｜DSML｜parameter></｜DSML｜invoke>')
        parsed, _ = root_scout.parse_model_json(raw)
        self.assertEqual(parsed["actions"], actions)
        expected = {"candidates": [], "updates": [], "actions": actions, "next_question": "", "stop": False}
        self.assertEqual(root_scout.parse_model_json(json.dumps(expected)), (expected, []))


if __name__ == "__main__":
    unittest.main()
