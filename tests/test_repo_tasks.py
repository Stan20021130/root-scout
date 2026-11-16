"""Unit tests for repository-level work orders, test analysis, and diff review."""
import json
from pathlib import Path
import tempfile
import unittest

import repo_checks
import repo_tasks


class TestRepoTasks(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="root_scout_repo_test_")
        self.repo_root = Path(self.temp_dir.name).resolve()

        # Create sample project structure
        self.src_dir = self.repo_root / "src"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.calc_file = self.src_dir / "calculator.py"
        self.calc_file.write_text(
            "def add(a, b):\n    return a + b\n\ndef divide(a, b):\n    return a / b\n",
            encoding="utf-8"
        )

        self.tests_dir = self.repo_root / "tests"
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        self.test_calc_file = self.tests_dir / "test_calculator.py"
        self.test_calc_file.write_text(
            "from src.calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_repo_path_security_boundary(self):
        """Ensure paths escaping the repository boundary are strictly rejected."""
        valid_path = repo_tasks.validate_repo_path(self.repo_root, "src/calculator.py")
        self.assertEqual(valid_path, self.calc_file)

        # Path traversal escape attempt
        with self.assertRaises(repo_tasks.WorkOrderValidationError):
            repo_tasks.validate_repo_path(self.repo_root, "../../etc/passwd")

    def test_02_exit_code_classification(self):
        """Verify accurate categorization of test exit codes."""
        self.assertEqual(repo_checks.classify_exit("pytest", 0), "passed")
        self.assertEqual(repo_checks.classify_exit("pytest", 1), "assertion_failed")
        self.assertEqual(repo_checks.classify_exit("pytest", 5), "no_tests_collected")
        self.assertEqual(repo_checks.classify_exit("pytest", 124, timed_out=True), "timeout")

    def test_03_diff_parsing(self):
        sample_diff = """diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -3,2 +3,4 @@
 def divide(a, b):
+    if b == 0:
+        raise ValueError("Cannot divide by zero")
     return a / b
"""
        files = repo_tasks.parse_unified_diff(sample_diff)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["new_path"], "src/calculator.py")
        self.assertEqual(len(files[0]["hunks"]), 1)
        self.assertEqual(files[0]["hunks"][0]["new_start"], 3)

    def test_04_execute_test_analysis_workflow(self):
        """Test Phase 6: Test Analysis identifying affected symbols and test recommendations."""
        diff = """diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    # logging added
     return a + b
"""
        work_order = {
            "task_kind": "test_analysis",
            "repo_root": str(self.repo_root),
            "diff": diff,
            "requirements": "Add debug logging to add function"
        }
        res = repo_tasks.execute_test_analysis(work_order)
        self.assertEqual(res["task_kind"], "test_analysis")
        self.assertEqual(res["run_status"], "completed")
        self.assertEqual(len(res["affected_paths"]), 1)
        self.assertEqual(res["affected_paths"][0]["symbol"], "add")

        # Check existing test discovery
        reusable = res["existing_reusable_tests"]
        self.assertTrue(any("test_calculator.py" in r["test_file"] and r["targets_symbol"] == "add" for r in reusable))

    def test_05_execute_review_diff_detects_defect(self):
        """Test Phase 7: Code Review detecting an explicit defect in diff hunks."""
        bad_diff = """diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -3,2 +3,5 @@
 def divide(a, b):
+    try:
+        val = a / b
+    except:
+        pass
"""
        work_order = {
            "task_kind": "review_diff",
            "repo_root": str(self.repo_root),
            "diff": bad_diff,
            "requirements": "Handle division gracefully"
        }
        res = repo_tasks.execute_review_diff(work_order)
        self.assertEqual(res["task_kind"], "review_diff")
        self.assertEqual(res["review_conclusion"], "findings_found")
        self.assertGreaterEqual(res["findings_count"], 1)
        
        bare_except_finding = next((f for f in res["findings"] if f["rule"] == "bare_except"), None)
        self.assertIsNotNone(bare_except_finding)
        self.assertEqual(bare_except_finding["file"], "src/calculator.py")
        self.assertEqual(bare_except_finding["status"], "unverified")

    def test_06_execute_review_diff_clean(self):
        """Test Phase 7: Clean diff yields no findings."""
        clean_diff = """diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    \"\"\"Return sum of two numbers.\"\"\"
     return a + b
"""
        work_order = {
            "task_kind": "review_diff",
            "repo_root": str(self.repo_root),
            "diff": clean_diff,
            "requirements": "Add docstring to add"
        }
        res = repo_tasks.execute_review_diff(work_order)
        self.assertEqual(res["review_conclusion"], "no_findings_in_scope")
        self.assertEqual(res["findings_count"], 0)
        self.assertFalse(res["fast_path"]["verified_clean"])
        self.assertIn("summary", res)

    def test_07_in_place_zero_token_diff_and_difflib_format(self):
        """Verify that diff can be omitted (zero-token in-place audit) and difflib format is supported."""
        # Test 1: Plain difflib format without 'diff --git'
        difflib_diff = """--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    # difflib comment
     return a + b
"""
        parsed = repo_tasks.parse_unified_diff(difflib_diff)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["new_path"], "src/calculator.py")

        # Test 2: Omitted diff (in-place zero-token audit of repo_root)
        work_order = {
            "task_kind": "review_diff",
            "repo_root": str(self.repo_root)
            # diff is intentionally omitted!
        }
        res = repo_tasks.execute_review_diff(work_order)
        self.assertEqual(res["review_conclusion"], "no_findings_in_scope")
        self.assertEqual(res["findings_count"], 0)
        self.assertFalse(res["fast_path"]["verified_clean"])

    def test_08_fast_path_and_risk_evaluation(self):
        """Verify adaptive risk evaluation and fast-path recommendations for small changes."""
        # 1. Compact diff <= 50 lines with 0 missing tests
        fast = repo_tasks.evaluate_change_risk_and_fast_path(
            total_files=1,
            total_lines=15,
            affected_symbols_count=1,
            reusable_tests_count=1,
            missing_tests_count=0
        )
        self.assertFalse(fast["eligible"])
        self.assertEqual(fast["risk_level"], "UNKNOWN")
        self.assertEqual(fast["size_category"], "SMALL")

        # 2. Large diff > 150 lines
        deep = repo_tasks.evaluate_change_risk_and_fast_path(
            total_files=5,
            total_lines=200,
            affected_symbols_count=6,
            reusable_tests_count=2,
            missing_tests_count=2
        )
        self.assertFalse(deep["eligible"])
        self.assertEqual(deep["risk_level"], "UNKNOWN")
        self.assertEqual(deep["size_category"], "LARGE")

    def test_09_test_contract_patterns_inference(self):
        """Verify AST extraction of contract expectations from existing tests."""
        contract_test = self.tests_dir / "test_contract.py"
        contract_test.write_text(
            "def test_contract():\n"
            "    res = {'items': ['ID123']}\n"
            "    assert 'ID123' in res['items']\n",
            encoding="utf-8"
        )
        hints = repo_tasks.infer_test_contract_patterns(self.repo_root, {"test_contract"})
        self.assertGreaterEqual(len(hints), 1)
        self.assertTrue(any("ID123" in h and "items" in h for h in hints))

    def test_10_patch_hints_and_extended_defect_rules(self):
        """Verify that patch_hints and extended defect rules (mutable defaults, == None) work."""
        bad_diff = """--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,3 +1,5 @@
+def compute(items=[]):
+    if items == None:
+        pass
+    except:
+        pass
"""
        work_order = {
            "task_kind": "review_diff",
            "repo_root": str(self.repo_root),
            "diff": bad_diff
        }
        res = repo_tasks.execute_review_diff(work_order)
        self.assertEqual(res["review_conclusion"], "findings_found")
        rules_found = {f["rule"] for f in res["findings"]}
        self.assertIn("bare_except", rules_found)
        self.assertIn("mutable_default", rules_found)
        self.assertIn("none_equality", rules_found)

        # Verify patch_hints are populated with actionable suggested replacements
        for f in res["findings"]:
            self.assertIn("patch_hint", f)
            self.assertIn("suggested_replacement", f["patch_hint"])


if __name__ == "__main__":
    unittest.main()
