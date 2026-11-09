---
name: rootscout
description: >-
  Use this skill when performing adversarial code review, minimal counterexample generation, and causal defect diagnosis for algorithm problems (C++17/20, Python 3).
---

# RootScout Algorithmic Code Review & Adversarial Diagnosis

RootScout is an evidence-driven white-box adversarial QA and causal diagnosis probe designed to review algorithmic source code (C++17, C++20, Python 3) inside an isolated Docker sandbox.

## Core Capabilities

1. **Adversarial Input Generation**: Automatically searches and constructs legal within-domain counterexamples for corner cases, boundary values, and extremal constraints.
2. **Causal Defect Localization**: Isolates the exact faulty source lines using minimal source interventions (`run_variant`) and validates state transitions.
3. **Invariant Violation Probing**: Verifies algorithm preconditions, loop invariants, and topological invariants (e.g. graph bounds, prefix sums, binary bit operations) against problem specifications.
4. **Zero-False-Positive Assurance**: Uses sandbox executions (`run_bash` property testing) to ensure correct submissions are marked clean without false alarms.

## CLI Invocation

- **Diagnose a single submission**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\run-local.ps1 diagnose <request.json> --strategy causal --out <output_dir>
  ```
- **Inspect diagnostic output**:
  - `probe.json`: Top-level verdict, verified counterexamples, causal findings, and original line anchors.
  - `case_state.json`: Complete causal trajectory, sandbox execution logs, invariant claims, and variant comparisons.
  - `result.json`: Defect candidates and findings summary.
