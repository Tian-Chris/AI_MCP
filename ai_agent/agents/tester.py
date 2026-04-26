"""Tester pipeline: lint -> build -> run. No LLM."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# verilator_mcp lives at repo root
_repo_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_repo_root))
import verilator_mcp  # noqa: E402

from ai_agent.session import TestResult, TestStatus  # noqa: E402


def _parse_test_markers(stdout: str) -> list[TestStatus]:
    """Parse $display markers in sim stdout into per-test statuses.

    Recognized markers:
      TESTS: name1,name2,name3       -> declares the full test set (statuses default to pending)
      TEST_PASS: <name>              -> marks <name> as verified
      TEST_FAIL: <name>: <reason>    -> marks <name> as failed with reason
    """
    declared: list[str] = []
    statuses: dict[str, TestStatus] = {}

    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("TESTS:"):
            payload = line[len("TESTS:"):].strip()
            for n in payload.split(","):
                name = n.strip()
                if name and name not in statuses:
                    declared.append(name)
                    statuses[name] = TestStatus(name=name, status="pending")
        elif line.startswith("TEST_PASS:"):
            name = line[len("TEST_PASS:"):].strip()
            if name:
                if name not in statuses:
                    declared.append(name)
                    statuses[name] = TestStatus(name=name)
                statuses[name].status = "verified"
                statuses[name].reason = ""
        elif line.startswith("TEST_FAIL:"):
            payload = line[len("TEST_FAIL:"):].strip()
            if ":" in payload:
                name, reason = payload.split(":", 1)
                name = name.strip()
                reason = reason.strip()
            else:
                name, reason = payload.strip(), ""
            if name:
                if name not in statuses:
                    declared.append(name)
                    statuses[name] = TestStatus(name=name)
                statuses[name].status = "failed"
                statuses[name].reason = reason

    return [statuses[n] for n in declared]


def _resolve(path_str: str, root: Path) -> str:
    """Return absolute string path, resolving relative paths against root."""
    p = Path(path_str)
    if not p.is_absolute():
        p = root / p
    return str(p.resolve())


def run_tests(spec: dict[str, Any], root: Path) -> TestResult:
    """Run the lint/build/sim pipeline on a spec.

    spec is a dict (JSON-roundtripped from the Spec dataclass) with at least:
      - module_name: str
      - rtl_path: str (absolute or relative to root)
      - tb_path: str (absolute or relative to root)

    Returns TestResult with phase = which step failed, or 'pass' if all green.
    Captures stdout/stderr from each step into the returned TestResult.
    """
    module_name: str = spec["module_name"]
    rtl_path: str = _resolve(spec["rtl_path"], root)
    tb_path: str = _resolve(spec["tb_path"], root)
    tb_top: str = f"tb_{module_name}"
    cwd: str = str(root)

    # ------------------------------------------------------------------
    # Lint stage
    # ------------------------------------------------------------------
    try:
        lint_result = verilator_mcp.verilator_lint(
            sources=[rtl_path, tb_path],
            top=tb_top,
            cwd=cwd,
        )
    except Exception as e:
        return TestResult(phase="lint", passed=False, stderr=str(e))

    if not lint_result.get("ok"):
        return TestResult(
            phase="lint",
            passed=False,
            stdout=lint_result.get("stdout", ""),
            stderr=lint_result.get("stderr", ""),
        )

    # ------------------------------------------------------------------
    # Build stage
    # ------------------------------------------------------------------
    try:
        build_result = verilator_mcp.verilator_build(
            sources=[rtl_path],
            tb=tb_path,
            top=tb_top,
            trace=True,
            cwd=cwd,
        )
    except Exception as e:
        return TestResult(phase="build", passed=False, stderr=str(e))

    if not build_result.get("ok"):
        return TestResult(
            phase="build",
            passed=False,
            stdout=build_result.get("stdout", ""),
            stderr=build_result.get("stderr", ""),
        )

    binary_path: str | None = build_result.get("binary_path")
    if not binary_path:
        return TestResult(
            phase="build",
            passed=False,
            stderr="Build succeeded but binary_path is missing.",
        )

    # ------------------------------------------------------------------
    # Sim (run) stage
    # ------------------------------------------------------------------
    try:
        run_result = verilator_mcp.verilator_run(
            binary=binary_path,
            cwd=cwd,
        )
    except Exception as e:
        return TestResult(phase="sim", passed=False, stderr=str(e))

    if not run_result.get("ok"):
        sim_stdout = run_result.get("stdout", "")
        parsed = _parse_test_markers(sim_stdout)
        return TestResult(
            phase="sim",
            passed=False,
            stdout=sim_stdout,
            stderr=run_result.get("stderr", ""),
            tests=parsed,
        )

    # ------------------------------------------------------------------
    # All green
    # ------------------------------------------------------------------
    waveform = root / "wave.vcd"
    success_stdout = run_result.get("stdout", "")
    parsed = _parse_test_markers(success_stdout)
    any_failed = any(t.status == "failed" for t in parsed)
    return TestResult(
        phase="sim" if any_failed else "pass",
        passed=not any_failed,
        stdout=success_stdout,
        stderr="",
        waveform_path=waveform if waveform.exists() else None,
        tests=parsed,
    )
