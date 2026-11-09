"""Repository-level Test Analysis and Diff Review Work Order orchestrator for RootScout."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple, Any

from repo_checks import RepoCheckRunner, classify_exit


class WorkOrderValidationError(ValueError):
    """Raised when an incoming work order fails schema or security constraints."""
    pass


def validate_repo_path(repo_root: Path, relative_or_abs_path: str) -> Path:
    """Safely resolve a path ensuring it does not escape repo_root via symlinks or '..'."""
    repo_root = repo_root.resolve()
    target = (repo_root / relative_or_abs_path).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError:
        raise WorkOrderValidationError(f"Path '{relative_or_abs_path}' escapes repository boundary: {repo_root}")
    return target


def parse_unified_diff(diff_text: str) -> List[Dict[str, Any]]:
    """Parse unified diff text into structured file modifications and line ranges.
    Supports git unified diffs (diff --git) and standard diffs (--- a/..., +++ b/...).
    """
    files = []
    current_file = None
    
    file_header_re = re.compile(r"^diff --git a/(.*?) b/(.*)$")
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        match_header = file_header_re.match(line)
        if match_header:
            if current_file:
                files.append(current_file)
            current_file = {
                "old_path": match_header.group(1),
                "new_path": match_header.group(2),
                "hunks": []
            }
            continue

        # Support standard unified diffs without 'diff --git'
        if line.startswith("--- "):
            raw_path = line[4:].strip()
            if raw_path.startswith("a/"):
                raw_path = raw_path[2:]
            # If current_file exists but already had hunks, finalize it
            if current_file and current_file.get("hunks"):
                files.append(current_file)
                current_file = None
            if not current_file:
                current_file = {"old_path": raw_path, "new_path": raw_path, "hunks": []}
            else:
                current_file["old_path"] = raw_path
            continue

        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path.startswith("b/"):
                raw_path = raw_path[2:]
            if not current_file:
                current_file = {"old_path": raw_path, "new_path": raw_path, "hunks": []}
            else:
                current_file["new_path"] = raw_path
            continue

        match_hunk = hunk_re.match(line)
        if match_hunk:
            if not current_file:
                current_file = {"old_path": "unknown", "new_path": "unknown", "hunks": []}
            old_start = int(match_hunk.group(1))
            old_len = int(match_hunk.group(2)) if match_hunk.group(2) else 1
            new_start = int(match_hunk.group(3))
            new_len = int(match_hunk.group(4)) if match_hunk.group(4) else 1
            current_file["hunks"].append({
                "old_start": old_start,
                "old_len": old_len,
                "new_start": new_start,
                "new_len": new_len,
                "lines": []
            })
            continue

        if current_file and current_file["hunks"]:
            current_file["hunks"][-1]["lines"].append(line)

    if current_file:
        files.append(current_file)

    return files


def extract_python_symbols_in_range(file_path: Path, start_line: int, end_line: int) -> List[Dict[str, Any]]:
    """Extract AST functions and classes intersecting with the given line range."""
    if not file_path.is_file() or not file_path.name.endswith(".py"):
        return []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except Exception:
        return []

    symbols = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s_line = getattr(node, "lineno", 0)
            e_line = getattr(node, "end_lineno", s_line)
            # Check for range overlap
            if max(s_line, start_line) <= min(e_line, end_line):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                symbols.append({
                    "name": node.name,
                    "kind": kind,
                    "start_line": s_line,
                    "end_line": e_line
                })
    return symbols


def find_callers_in_repo(repo_root: Path, symbol_name: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """Locate usage references to a symbol across Python files in the repository."""
    results = []
    for py_file in repo_root.rglob("*.py"):
        if any(part.startswith(".") or part in ("__pycache__", "node_modules", "venv", ".venv") for part in py_file.parts):
            continue
        try:
            lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for idx, line in enumerate(lines, 1):
                if re.search(r"\b" + re.escape(symbol_name) + r"\b", line):
                    rel = str(py_file.relative_to(repo_root))
                    results.append({
                        "file": rel,
                        "line": idx,
                        "snippet": line.strip()[:150]
                    })
                    if len(results) >= max_results:
                        return results
        except Exception:
            continue
    return results


def resolve_repo_diff_files(repo_root: Path, diff_text: Optional[str] = None, target_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Resolve diff files from provided text or directly in-place from the repo working tree."""
    if diff_text and diff_text.strip():
        parsed = parse_unified_diff(diff_text)
        if parsed:
            return parsed

    # 1. In-place Git diff resolution (Zero-Token diff)
    try:
        git_res = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5
        )
        if git_res.returncode == 0 and git_res.stdout.strip():
            parsed = parse_unified_diff(git_res.stdout)
            if parsed:
                return parsed
        git_unstaged = subprocess.run(
            ["git", "diff"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5
        )
        if git_unstaged.returncode == 0 and git_unstaged.stdout.strip():
            parsed = parse_unified_diff(git_unstaged.stdout)
            if parsed:
                return parsed
    except Exception:
        pass

    # 2. Non-git fallback: inspect python files in repo_root directly
    files_to_check = target_files or []
    if not files_to_check:
        files_to_check = [
            str(p.relative_to(repo_root))
            for p in repo_root.rglob("*.py")
            if not p.name.startswith("test_") and not p.name.endswith("_test.py") and "__pycache__" not in p.parts
        ]

    synth_files = []
    for rel_f in files_to_check:
        full = repo_root / rel_f
        if full.is_file():
            try:
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
                synth_files.append({
                    "old_path": rel_f,
                    "new_path": rel_f,
                    "hunks": [{
                        "old_start": 1,
                        "old_len": len(lines),
                        "new_start": 1,
                        "new_len": len(lines),
                        "lines": [f"+{l}" for l in lines]
                    }]
                })
            except Exception:
                pass
    return synth_files


def validate_work_order(order: Dict[str, Any]) -> None:
    """Validate mandatory structure and field types for a repo work order."""
    if not isinstance(order, dict):
        raise WorkOrderValidationError("Work order must be a JSON object")

    task_kind = order.get("task_kind")
    if task_kind not in ("test_analysis", "review_diff"):
        raise WorkOrderValidationError(f"Invalid task_kind: '{task_kind}'. Must be 'test_analysis' or 'review_diff'")

    repo_root = order.get("repo_root")
    if not repo_root or not Path(repo_root).is_dir():
        raise WorkOrderValidationError(f"Invalid repo_root: '{repo_root}' does not exist or is not a directory")

    diff = order.get("diff")
    if diff is not None and not isinstance(diff, str):
        raise WorkOrderValidationError("diff must be a string if provided")

    requirements = order.get("requirements")
    if requirements is not None and not isinstance(requirements, (str, list)):
        raise WorkOrderValidationError("requirements must be a string or list of strings")


def infer_test_contract_patterns(repo_root: Path, target_symbols: Set[str]) -> List[str]:
    """Scan existing test files to infer calling contracts and expected assertion patterns.
    Helps prevent model over-engineering or assertion type mismatches (e.g. string vs list of dicts).
    """
    contract_hints = []
    seen_hints = set()

    for test_file in list(repo_root.rglob("test_*.py")) + list(repo_root.rglob("*_test.py")):
        if any(p.startswith(".") or p in ("__pycache__", "node_modules", "venv") for p in test_file.parts):
            continue
        try:
            content = test_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=str(test_file))
            for node in ast.walk(tree):
                # Pattern 1: assert 'constant_str' in container
                if isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare):
                    cmp = node.test
                    if any(isinstance(op, ast.In) for op in cmp.ops):
                        left = cmp.left
                        for op, right in zip(cmp.ops, cmp.comparators):
                            if isinstance(op, ast.In) and isinstance(left, ast.Constant) and isinstance(left.value, str):
                                container_key = None
                                if isinstance(right, ast.Subscript) and isinstance(right.slice, ast.Constant):
                                    container_key = right.slice.value
                                elif isinstance(right, ast.Call) and getattr(right.func, "attr", "") == "get" and right.args and isinstance(right.args[0], ast.Constant):
                                    container_key = right.args[0].value
                                if container_key and isinstance(container_key, str):
                                    hint = (
                                        f"Contract Hint: Existing test '{test_file.name}' asserts string '{left.value}' in '{container_key}'. "
                                        f"Ensure '{container_key}' stores flat string codes/identifiers or dictionary keys rather than nested object dicts."
                                    )
                                    if hint not in seen_hints:
                                        seen_hints.add(hint)
                                        contract_hints.append(hint)

                # Pattern 2: self.assertIn('constant_str', container)
                elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "assertIn":
                    if len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        val = node.args[0].value
                        right = node.args[1]
                        container_key = None
                        if isinstance(right, ast.Subscript) and isinstance(right.slice, ast.Constant):
                            container_key = right.slice.value
                        elif isinstance(right, ast.Call) and getattr(right.func, "attr", "") == "get" and right.args and isinstance(right.args[0], ast.Constant):
                            container_key = right.args[0].value
                        if container_key and isinstance(container_key, str):
                            hint = (
                                f"Contract Hint: Existing test '{test_file.name}' asserts assertIn('{val}', '{container_key}'). "
                                f"Ensure '{container_key}' stores flat string codes/identifiers rather than nested object dicts."
                            )
                            if hint not in seen_hints:
                                seen_hints.add(hint)
                                contract_hints.append(hint)

                # Pattern 3: assert isinstance(container, list/dict)
                elif isinstance(node, ast.Assert) and isinstance(node.test, ast.Call) and getattr(node.test.func, "id", "") == "isinstance":
                    if len(node.test.args) >= 2:
                        target = node.test.args[0]
                        type_arg = node.test.args[1]
                        type_name = getattr(type_arg, "id", None)
                        if type_name:
                            t_name = getattr(target, "id", None) or "return value"
                            hint = f"Contract Hint: Test expects '{t_name}' to be an instance of {type_name}."
                            if hint not in seen_hints:
                                seen_hints.add(hint)
                                contract_hints.append(hint)
        except Exception:
            continue

    return contract_hints[:8]


def infer_requirements_contract_patterns(requirements: str) -> List[str]:
    """Analyze requirement text for common API pattern ambiguities and recommend idiomatic conventions."""
    hints = []
    if not requirements or not isinstance(requirements, str):
        return hints

    # Pattern 1: "X (with reason)" or "X with reasons"
    match = re.search(r"(\w+)\s*\((?:with\s+reason[s]?|reason)\)", requirements, re.IGNORECASE)
    if match:
        field_name = match.group(1)
        hints.append(
            f"API Shape Recommendation: For '{field_name} (with reason)', prefer returning a dictionary mapping "
            f"{{identifier: reason_str}} (e.g. {{'CODE': 'reason'}}). This satisfies reason requirements while allowing "
            f"callers to perform standard membership checks ('if code in {field_name}:')."
        )

    # Pattern 2: Luhn / Credit Card PII
    if re.search(r"\b(?:luhn|credit\s*card)\b", requirements, re.IGNORECASE):
        hints.append(
            "Data Sanitization Hint: Credit card numbers frequently contain delimiters ('-' or spaces). "
            "Strip all non-digit characters ('re.sub(r'\\D', '', card_str)') before running the Luhn checksum check, "
            "and preserve delimiter structure in the masked output (e.g. 1234-****-****-5678)."
        )

    # Pattern 3: Circular references / Recursion
    if re.search(r"\b(?:circular|cyclic|recursion)\b", requirements, re.IGNORECASE):
        hints.append(
            "Cycle Protection Hint: For recursive data structures, pass a 'seen=None' set tracking 'id(obj)' "
            "to safely replace circular references with a sentinel before traversing child elements."
        )

    # Pattern 4: Dead Letter Queue / Failed records metadata
    match_dlq = re.search(r"records\s+(?:failed\s+)?(?:items|records|entries)\s+with:\s*([^\n\.]+)", requirements, re.IGNORECASE)
    if match_dlq:
        fields = match_dlq.group(1).strip()
        hints.append(
            f"DLQ Packaging Contract: Failed records routed to DeadLetterQueue must be wrapped as dicts with required metadata: {fields}. "
            f"Ensure items stored in dlq.records are metadata dicts containing these keys, rather than raw unmodified record dicts."
        )

    return hints


def evaluate_change_risk_and_fast_path(
    total_files: int,
    total_lines: int,
    affected_symbols_count: int,
    reusable_tests_count: int,
    missing_tests_count: int
) -> Dict[str, Any]:
    """Describe diff size without claiming that symbol matches establish safety."""
    if total_lines <= 50 and total_files <= 2:
        size_category = "SMALL"
    elif total_lines <= 150 and total_files <= 4:
        size_category = "MEDIUM"
    else:
        size_category = "LARGE"

    return {
        "eligible": False,
        "risk_level": "UNKNOWN",
        "size_category": size_category,
        "total_lines_changed": total_lines,
        "guidance": "Diff size and symbol references do not establish coverage or safety; review the change and run relevant checks."
    }


def generate_patch_hint(rule: str, code_line: str) -> Dict[str, str]:
    """Generate deterministic, actionable replacement code and guidance for detected defects."""
    if rule == "bare_except":
        return {
            "strategy": "Replace bare 'except:' with 'except Exception as e:' to preserve SystemExit and KeyboardInterrupt.",
            "suggested_replacement": "except Exception as e:"
        }
    elif rule == "hardcoded_secret":
        return {
            "strategy": "Extract hardcoded secret into environment variable or configuration parameter.",
            "suggested_replacement": "os.environ.get('SECRET_KEY', '')"
        }
    elif rule == "unsafe_execution":
        return {
            "strategy": "Avoid eval/exec on untrusted data; use ast.literal_eval or safe structured parser.",
            "suggested_replacement": "ast.literal_eval(payload)"
        }
    elif rule == "division_by_zero":
        return {
            "strategy": "Guard against division by zero with an explicit non-zero condition.",
            "suggested_replacement": "if denominator != 0: ... else: ..."
        }
    elif rule == "mutable_default":
        return {
            "strategy": "Replace mutable default argument with None and initialize inside the function body.",
            "suggested_replacement": "def func(param=None):\n    if param is None:\n        param = []"
        }
    elif rule == "none_equality":
        return {
            "strategy": "Use identity comparison 'is None' or 'is not None' instead of equality operator.",
            "suggested_replacement": code_line.replace("== None", "is None").replace("!= None", "is not None")
        }
    elif rule == "unsanitized_numeric_validator":
        return {
            "strategy": "Sanitize string by stripping hyphens and spaces before Luhn or numeric validation.",
            "suggested_replacement": "clean_digits = re.sub(r'\\D', '', raw_str)"
        }
    elif rule == "unbounded_recursion_guard":
        return {
            "strategy": "Pass a 'seen = set()' parameter and track id(obj) to avoid RecursionError on circular references.",
            "suggested_replacement": "if seen is None: seen = set()\nif id(obj) in seen: return '[CIRCULAR REFERENCE]'\nseen.add(id(obj))"
        }
    return {
        "strategy": "Refactor code to satisfy safety constraint.",
        "suggested_replacement": code_line
    }


def execute_test_analysis(work_order: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 6: Analyze diff impact, map existing tests, identify coverage gaps and recommended tests."""
    validate_work_order(work_order)
    repo_root = Path(work_order["repo_root"]).resolve()
    diff_text = work_order.get("diff", "")
    target_files = work_order.get("files")
    existing_tests_config = work_order.get("existing_tests", [])

    diff_files = resolve_repo_diff_files(repo_root, diff_text, target_files)
    affected_paths = []
    affected_symbols = set()
    total_lines_changed = 0

    for df in diff_files:
        new_path = df.get("new_path")
        if not new_path or new_path == "/dev/null":
            continue
        full_path = validate_repo_path(repo_root, new_path)
        hunks = df.get("hunks", [])
        for h in hunks:
            start = h["new_start"]
            end = start + max(1, h["new_len"]) - 1
            total_lines_changed += len(h.get("lines", []))
            symbols = extract_python_symbols_in_range(full_path, start, end)
            for s in symbols:
                affected_symbols.add(s["name"])
                affected_paths.append({
                    "file": new_path,
                    "symbol": s["name"],
                    "kind": s["kind"],
                    "lines": f"{s['start_line']}-{s['end_line']}",
                    "reason": f"Modified lines {start}-{end} fall inside {s['kind']} '{s['name']}'"
                })

    # Search for matching existing tests
    reusable_tests = []
    discovered_tests = []
    for test_cand in repo_root.rglob("test_*.py"):
        rel_test = str(test_cand.relative_to(repo_root))
        discovered_tests.append(rel_test)
        try:
            content = test_cand.read_text(encoding="utf-8", errors="replace")
            for sym in affected_symbols:
                if re.search(r"\b" + re.escape(sym) + r"\b", content):
                    reusable_tests.append({
                        "test_file": rel_test,
                        "targets_symbol": sym,
                        "recommendation": "Execute as regression check against modified behavior"
                    })
        except Exception:
            pass

    # Identify potential risk gaps
    missing_test_risks = []
    for sym in affected_symbols:
        has_test = any(r["targets_symbol"] == sym for r in reusable_tests)
        if not has_test:
            missing_test_risks.append({
                "symbol": sym,
                "risk": "No existing test file explicitly references this symbol",
                "recommendation": f"Add isolated unit test covering modified branches of '{sym}'"
            })

    # Optionally execute configured checks
    check_results = []
    checks_to_run = work_order.get("run_checks", [])
    if checks_to_run:
        runner = RepoCheckRunner(repo_root)
        for c in checks_to_run:
            c_id = c.get("id", "check")
            c_cmd = c.get("command", ["pytest"])
            res = runner.run_check(c_id, c_cmd)
            check_results.append(res)

    if all(c.get("passed", False) for c in check_results):
        run_status = "completed"
    elif any(c.get("classification") == "environment_blocked" for c in check_results):
        run_status = "environment_blocked"
    elif any(c.get("classification") == "assertion_failed" for c in check_results):
        run_status = "assertion_failed"
    else:
        run_status = "inconclusive"

    # Compact reusable test files list
    unique_test_files = sorted(list({r["test_file"] for r in reusable_tests}))

    # Fast-Path and Contract Hints
    fast_path = evaluate_change_risk_and_fast_path(
        total_files=len(diff_files),
        total_lines=total_lines_changed,
        affected_symbols_count=len(affected_symbols),
        reusable_tests_count=len(reusable_tests),
        missing_tests_count=len(missing_test_risks)
    )
    contract_hints = infer_test_contract_patterns(repo_root, affected_symbols)
    req_hints = infer_requirements_contract_patterns(work_order.get("requirements", ""))
    for rh in req_hints:
        if rh not in contract_hints:
            contract_hints.append(rh)

    return {
        "task_kind": "test_analysis",
        "run_status": run_status,
        "repo_root": str(repo_root),
        "total_files_changed": len(diff_files),
        "total_lines_changed": total_lines_changed,
        "fast_path": fast_path,
        "affected_paths": affected_paths,
        "contract_hints": contract_hints,
        "existing_reusable_tests": reusable_tests,
        "reusable_test_files": unique_test_files,
        "missing_test_risks": missing_test_risks,
        "check_executions": check_results,
        "summary": {
            "affected_symbols_count": len(affected_symbols),
            "reusable_tests_count": len(reusable_tests),
            "missing_tests_count": len(missing_test_risks),
            "fast_path_eligible": fast_path["eligible"],
            "risk_level": fast_path["risk_level"]
        }
    }


def execute_review_diff(work_order: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 7: Code Review and defect localization over diff changes without modifying code."""
    validate_work_order(work_order)
    repo_root = Path(work_order["repo_root"]).resolve()
    diff_text = work_order.get("diff", "")
    target_files = work_order.get("files")
    requirements = work_order.get("requirements", "")
    diff_files = resolve_repo_diff_files(repo_root, diff_text, target_files)

    findings = []
    checked_files = []

    for df in diff_files:
        new_path = df.get("new_path")
        if not new_path or new_path == "/dev/null":
            continue
        full_path = validate_repo_path(repo_root, new_path)
        checked_files.append(new_path)
        hunks = df.get("hunks", [])

        # Heuristic static analysis over added/modified lines in hunks
        for hunk_idx, h in enumerate(hunks):
            line_no = h["new_start"] - 1
            for line in h.get("lines", []):
                if line.startswith(("-", "\\")):
                    continue
                line_no += 1
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                code_line = line[1:].strip()

                # Check 1: Hardcoded secrets or credentials
                if re.search(r"(?:api_key|password|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{8,}['\"]", code_line, re.IGNORECASE):
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "critical",
                        "status": "unverified",
                        "rule": "hardcoded_secret",
                        "description": "Possible hardcoded credential or secret committed directly in source.",
                        "trigger": "Static scan on diff hunk lines",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("hardcoded_secret", code_line)
                    })

                # Check 2: Raw eval / dangerous execution
                if re.search(r"\b(?:eval|exec)\s*\(", code_line):
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "critical",
                        "status": "unverified",
                        "rule": "unsafe_execution",
                        "description": "Unsafe dynamic code execution via eval/exec.",
                        "trigger": "Untrusted input to dynamic evaluator",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("unsafe_execution", code_line)
                    })

                # Check 3: Bare except clause suppressing errors
                if re.match(r"^except\s*:", code_line):
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "major",
                        "status": "unverified",
                        "rule": "bare_except",
                        "description": "Bare 'except:' suppresses all exceptions including SystemExit and KeyboardInterrupt.",
                        "trigger": "Any unhandled exception in try block",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("bare_except", code_line)
                    })

                # Check 4: Division by potential zero without guard
                if re.search(r"\b/\s*(?:0|[a-zA-Z_]\w*)\b", code_line) and "if" not in code_line and "// 0" not in code_line and code_line.endswith("/ 0"):
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "major",
                        "status": "unverified",
                        "rule": "division_by_zero",
                        "description": "Explicit division by zero detected in changed line.",
                        "trigger": "Execution of modified statement",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("division_by_zero", code_line)
                    })

                # Check 5: Mutable default arguments
                if re.search(r"def\s+\w+\([^)]*=\s*(?:\[\]|\{\})", code_line):
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "major",
                        "status": "unverified",
                        "rule": "mutable_default",
                        "description": "Mutable default argument ([] or {}) persists state across function calls.",
                        "trigger": "Function definition with mutable default argument",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("mutable_default", code_line)
                    })

                # Check 6: Comparison with None using equality instead of identity
                if re.search(r"(?:==|!=)\s*None\b", code_line):
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "minor",
                        "status": "unverified",
                        "rule": "none_equality",
                        "description": "Comparison with None should use identity ('is None' or 'is not None') rather than equality.",
                        "trigger": "Equality operator against None singleton",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("none_equality", code_line)
                    })

                # Check 7: Luhn or credit card validation on raw string without sanitizing delimiters
                if re.search(r"\b(?:luhn|is_valid_card|luhn_checksum)\s*\(", code_line, re.IGNORECASE) and "-" not in code_line and "replace" not in code_line:
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "major",
                        "status": "unverified",
                        "rule": "unsanitized_numeric_validator",
                        "description": "Card/Luhn validation called on potential raw string without sanitizing delimiters ('-' or ' ').",
                        "trigger": "Direct numeric validation on un-stripped formatted string",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("unsanitized_numeric_validator", code_line)
                    })

                # Check 8: Recursive function definition missing seen cycle guard
                if re.search(r"def\s+(?:sanitize|clean|traverse|walk|serialize)\b", code_line, re.IGNORECASE) and "seen" not in code_line and "visited" not in code_line:
                    findings.append({
                        "id": f"finding-{len(findings)+1:03d}",
                        "file": new_path,
                        "line": line_no,
                        "severity": "major",
                        "status": "unverified",
                        "rule": "unbounded_recursion_guard",
                        "description": "Recursive traversal/sanitization function lacks 'seen' or 'visited' cycle-detection parameter, risking RecursionError on circular data.",
                        "trigger": "Recursive data structure traversal without visited cycle guard",
                        "evidence": code_line,
                        "patch_hint": generate_patch_hint("unbounded_recursion_guard", code_line)
                    })

    has_defects = len(findings) > 0
    conclusion = "findings_found" if has_defects else "no_findings_in_scope"

    if not has_defects:
        return {
            "task_kind": "review_diff",
            "run_status": "completed",
            "review_conclusion": conclusion,
            "findings_count": 0,
            "checked_files": checked_files,
            "findings": [],
            "fast_path": {
                "verified_clean": False,
                "guidance": "No configured text rule matched; this does not establish correctness, security, or review completeness."
            },
            "summary": f"No configured text rules matched in {len(checked_files)} file(s); other defects remain unassessed."
        }

    return {
        "task_kind": "review_diff",
        "run_status": "completed",
        "review_conclusion": conclusion,
        "repo_root": str(repo_root),
        "checked_files": checked_files,
        "findings_count": len(findings),
        "findings": findings,
        "instructions_for_main_agent": "Review each unverified rule match and suggested patch in context before deciding whether a change is needed."
    }
