"""Render a human-facing localisation report from a saved CaseState.

Report layer only: no model call, no re-run, reads case_state.json as it is.
Goal is what a solver needs in one screen - where the problem is, how strong the
evidence is, what input shows it, and what the model says the fix direction is.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "validation/bench-100-full-2026-11-09/results"

sys.path.insert(0, str(ROOT))
try:  # reuse the controller's own site check so the report never disagrees with the state
    import root_scout as core
except Exception:  # pragma: no cover - the report must still render without the engine
    core = None

SITE_TEXT = {"ok": "行号可用", "weak_site": "引用的行只有输入/括号", "out_of_range": "行号越界", "no_line": "未给行号"}
LOC_TEXT = {"located_with_line": "已指认到可用行号", "located_weak_line": "指认的行不含可读语句",
            "located_without_line": "未指认具体行", "no_finding": "未发现缺陷"}

STOP_TEXT = {
    "oracle_unresolved": "已给出定位，但没有取得受信 expected",
    "no_bug_found_within_scope": "未发现缺陷",
    "model_protocol_incomplete": "已给出定位，但本次运行协议未完整收尾",
    "budget_exhausted": "预算耗尽，定位可能不完整",
    "environment_blocked": "环境不可用，未执行验证",
}
INPUT_LINE = re.compile(r"^\s*(cin\s*>>|scanf|input\(|getline|read\s*\(|n\s*,\s*\*\s*a\s*=)")


def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def code_line(source, number):
    lines = (source or "").splitlines()
    if not isinstance(number, int) or not 1 <= number <= len(lines):
        return None
    return lines[number - 1]


def parse_location(claim):
    """Pull leading line numbers out of strings like 'lines 20-25' or 'line 24'."""
    numbers = []
    for token in re.findall(r"lines?\s+(\d+)(?:\s*[-–]\s*(\d+))?", claim or ""):
        start = int(token[0])
        end = int(token[1]) if token[1] else start
        numbers.append((start, end))
    if not numbers:
        for token in re.findall(r"\(line[s]?\s+(\d+)", claim or ""):
            numbers.append((int(token), int(token)))
    return numbers


def spans_of(candidate):
    spans = []
    for claim in candidate.get("source_location_claims") or []:
        spans.extend(parse_location(claim))
    return spans


def overlaps(one, other):
    return any(a <= d and c <= b for a, b in one for c, d in other)


def evidence_index(state):
    return {e["id"]: e for e in state.get("evidence", []) if isinstance(e, dict)}


def grade(candidate, ev_by_id, actions_by_id=None):
    """Evidence grade from the records, not from the model's wording.

    The pinned tests in this dataset pass on the bug program (sample-pass, private-fail),
    so a trusted mismatch is rare; the meaningful line is whether the model got an actual
    sandbox observation or only reasoned about the source.
    """
    actions_by_id = actions_by_id or {}
    ids = [i for i in (candidate.get("current_evidence_ids") or [])]
    records = [ev_by_id[i] for i in ids if i in ev_by_id]
    acted = [actions_by_id[i] for i in ids if i in actions_by_id and actions_by_id[i].get("status") == "accepted"]
    trusted_mismatch = [r for r in records
                        if r.get("trusted_expected") and (r.get("result") or {}).get("matched") is False
                        and (r.get("result") or {}).get("status") == "ok"]
    observed = [r for r in records if (r.get("result") or {}).get("status") == "ok"]
    ran = [a for a in acted if isinstance((a.get("result") or {}), dict)
           and any(k in (a.get("result") or {}) for k in ("stdout", "observations"))]
    if trusted_mismatch:
        return 3, "受信 expected 不符"
    if observed or ran:
        return 2, "沙箱观测（无受信期望）"
    if ids:
        return 1, "证据引用存在，但无成功执行"
    return 0, "仅静态推理，未验证"


def report(case_id):
    state = json.loads((RESULTS / case_id / "diagnose/case_state.json").read_text(encoding="utf-8"))
    source = (state.get("request_snapshot") or {}).get("source", "")
    problem = (state.get("request_snapshot") or {}).get("environment", {}).get("problem_id", "")
    ev = evidence_index(state)
    acts = {a["id"]: a for a in state.get("tool_actions", []) if isinstance(a, dict)}
    loc = state.get("localization") or {}
    if not loc and core is not None:
        loc = core.v2_localization(state)
    calls = [c for c in state["budget"]["calls"] if c.get("status") not in ("rejected", "skipped")]

    active = [c for c in state["candidates"] if c["lifecycle"] not in ("withdrawn", "merged")
              and c["candidate_kind"] in ("defect_candidate", "unclassified")]

    # one entry per source site: candidates pointing at overlapping lines are one site,
    # because a solver reads locations, not claim wording
    groups = []
    for cand in active:
        spans = spans_of(cand)
        for group in groups:
            if spans and group["spans"] and overlaps(spans, group["spans"]):
                group["members"].append(cand)
                group["spans"] = sorted(group["spans"] + spans)
                break
            if not spans and not group["spans"]:
                group["members"].append(cand)
                break
        else:
            groups.append({"spans": spans, "members": [cand]})
    for group in groups:
        head = max(group["members"], key=lambda c: (grade(c, ev, acts)[0], -c["turn"]))
        group["head"] = head
        group["score"] = grade(head, ev, acts)[0]
        group["has_location"] = bool(group["spans"])
    groups.sort(key=lambda g: (-g["score"], not g["has_location"], g["head"]["id"]))
    site_checks = {s.get("candidate_id"): s.get("site_check") for s in loc.get("sites", []) if isinstance(s, dict)}

    lines = [f"## {case_id}  `{problem}`", ""]
    if loc:
        lines += [f"定位状态：**{LOC_TEXT.get(loc.get('status'), loc.get('status'))}**"
                  f"（最佳证据：{loc.get('best_evidence') or '无'}，主指针：{loc.get('primary_candidate_id') or '无'}）", ""]
    trusted = (state.get("coverage") or {}).get("trusted_test_count") or {}
    if trusted.get("available"):
        lines += [f"给定测试：{trusted.get('executed', 0)}/{trusted['available']} 已执行，"
                  f"{trusted.get('mismatches', 0)} 处不符 —— "
                  f"{'**没有可复现的失败用例**，以下定位全部来自算法推理与自建对照' if not trusted.get('mismatches') else '存在失败的受信用例'}", ""]
    if not groups:
        lines += [f"**结论**：未定位到缺陷（{STOP_TEXT.get(state['stop_reason'], state['stop_reason'])}）", ""]
    for rank, group in enumerate(groups):
        head = group["head"]
        _, label = grade(head, ev, acts)
        title = "**主要问题**" if rank == 0 else f"其他线索 {rank}"
        site_check = site_checks.get(head["id"]) or (core.v2_site_check(head, source) if core is not None else "")
        site_note = f" ｜ {SITE_TEXT.get(site_check, site_check)}" if site_check else ""
        lines.append(f"### {title}  [{label}{site_note}]  ({head['id']}{'  +' + str(len(group['members']) - 1) + ' 条同位置候选' if len(group['members']) > 1 else ''})")
        lines.append("")
        shown_numbers = set()
        if group["spans"]:
            first_start = min(s for s, _ in group["spans"])
            for number in range(max(1, first_start - 2), first_start):
                text = code_line(source, number)
                if text is not None:
                    lines.append(f"- `{number:>3}` `{text.strip()[:110]}`  ← 上文")
        for start, end in group["spans"]:
            span_lines = list(range(start, end + 1))
            truncated = len(span_lines) > 5
            for number in (span_lines[:4] if truncated else span_lines):
                if number in shown_numbers:
                    continue
                shown_numbers.add(number)
                text = code_line(source, number)
                if text is None:
                    continue
                flag = "  ← 疑似输入行" if INPUT_LINE.match(text) else ""
                lines.append(f"- `{number:>3}` `{text.strip()[:110]}`{flag}")
            if truncated:
                lines.append(f"- …（声明区间到第 {end} 行）")
        if not group["spans"]:
            prose = [c for c in (head.get("source_location_claims") or []) if str(c).strip()]
            if prose:
                lines.append("- 位置（模型用文字描述，未给行号）：")
                for item in prose[:3]:
                    lines.append(f"  - {str(item)[:160]}")
            else:
                lines.append("- 位置：**未给出**（模型没有指认具体位置）")
        lines.append("")
        lines.append(f"- 触发条件：{(head.get('trigger_claim') or '未给出')[:220]}")
        consequence = (head.get("consequence_claim") or "").strip()
        if consequence:
            lines.append(f"- 后果：{consequence[:220]}")
        claim_text = (head.get("current_claim") or "").strip()
        if claim_text:
            lines.append(f"- 模型陈述：{claim_text[:420]}")
        requirements = head.get("requirement_claims") or []
        if requirements:
            lines.append("- 应满足：")
            for item in requirements[:3]:
                lines.append(f"  - {str(item)[:200]}")
        doubts = head.get("uncertainties") or []
        if doubts:
            lines.append(f"- 模型自己不确定：{str(doubts)[:200]}")
        inputs = []
        for ev_id in head.get("current_evidence_ids") or []:
            record = ev.get(ev_id)
            if record and isinstance(record.get("input"), str) and record["input"].strip():
                stdout = (record.get("result") or {}).get("stdout", "")
                inputs.append((ev_id, record["input"], stdout))
        if inputs:
            lines.append("- 复现输入（来自证据记录）：")
            for ev_id, stdin, stdout in inputs[:2]:
                lines.append(f"  - `{ev_id}` 输入 `{stdin.strip()[:80]}` → 输出 `{stdout.strip()[:60]}`")
        others = [c for c in group["members"] if c["id"] != head["id"]]
        if others:
            lines.append("- 同一位置的其他解释：")
            for other in others:
                _, other_label = grade(other, ev)
                lines.append(f"  - `{other['id']}`（{other_label}）{(other.get('trigger_claim') or other.get('current_claim') or '')[:150]}")
        lines.append("")

    tokens = state["budget"]["confirmed_usage_tokens"]
    wall = state["budget"]["wall_seconds"]
    lines.append(f"---\n运行：{len(calls)} 次调用 / {tokens:,} token / {wall:.0f}s ｜ 结论：{STOP_TEXT.get(state['stop_reason'], state['stop_reason'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    cases = sys.argv[1:] or ["b100-p035", "b100-p077", "b100-p017", "b100-p041", "b100-p068"]
    out = ROOT / "validation/localize-report-sample.md"
    body = "\n\n".join(report(c) for c in cases)
    header = "# 定位报告样例（报告层重渲染）\n\n数据来自 `validation/bench-100-full-2026-11-09`，不重跑、不调用模型，只重新组织已有记录。\n"
    out.write_text(header + "\n" + body + "\n", encoding="utf-8")
    print(body)
