"""Entry point for `python -m ai_agent`. Parses top-level CLI flags, hands off to cli.run()."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Self-fix sys.path when invoked as a script — keeps `from ai_agent.X import Y` working
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_agent.cli import run
from ai_agent.config import DEFAULT_MODELS


def _run_batch(args: argparse.Namespace) -> None:
    """Non-interactive batch mode: drive spec->write->test autonomously, write result.json."""
    from ai_agent.agents.orchestrator import run_orchestrator_turn, _build_llm
    from ai_agent.session import Session

    root = args.root.resolve()
    os.environ["AI_MCP_ALLOWED_ROOT"] = str(root)

    models = {
        "orchestrator": args.orchestrator_model,
        "spec":         args.spec_model,
        "writer":       args.writer_model,
    }
    session = Session(models=models)
    llm = _build_llm("orchestrator", session)

    # Inject goal as first user turn.
    # Prefix with BATCH_MODE so the orchestrator skips user-approval gate.
    batch_goal = (
        "[BATCH_MODE: auto-proceed through spec->write->test without asking for approval] "
        + args.goal
    )
    session.add_user(batch_goal)

    files_written: list[str] = []
    lint_ok: bool | None = None
    build_ok: bool | None = None
    sim_ok: bool | None = None
    passed = False
    retries = 0

    # Up to max_retries full orchestrator turns to reach a pass
    for attempt in range(args.max_retries):
        run_orchestrator_turn(session, llm, root)
        # Check session state after this turn
        if session.last_test is not None:
            tr = session.last_test
            retries = session.retry_count
            if tr.phase in ("lint",):
                lint_ok = False
                build_ok = None
                sim_ok = None
            elif tr.phase in ("build",):
                lint_ok = True
                build_ok = False
                sim_ok = None
            elif tr.phase in ("sim",):
                lint_ok = True
                build_ok = True
                sim_ok = False
            elif tr.phase in ("pass",):
                lint_ok = True
                build_ok = True
                sim_ok = True
                passed = True
            if passed:
                break
            # Not passed yet — feed "retry" message so orchestrator continues
            if attempt < args.max_retries - 1:
                session.add_user(
                    f"[BATCH_MODE] Tests failed (phase={tr.phase}). "
                    "Auto-retry: call dispatch_writers then run_tests again."
                )
        else:
            # No test result yet — orchestrator may still be at spec/write phase
            if attempt < args.max_retries - 1:
                session.add_user("[BATCH_MODE] Continue: proceed to write and test.")

    # Collect written files from the workdir
    for p in root.iterdir():
        if p.suffix in (".v", ".sv") and not p.name.startswith("_"):
            files_written.append(str(p))

    result = {
        "passed": passed,
        "retries": retries,
        "lint_ok": lint_ok,
        "build_ok": build_ok,
        "sim_ok": sim_ok,
        "files_written": files_written,
        "log_path": None,
    }
    args.json_out.write_text(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai_agent",
        description="AI RTL coding agent — multi-provider, REPL-based.",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Filesystem sandbox root (default: cwd)")
    parser.add_argument("--orchestrator-model", default=DEFAULT_MODELS["orchestrator"],
                        help=f"Top-level chat model (default: {DEFAULT_MODELS['orchestrator']})")
    parser.add_argument("--spec-model", default=DEFAULT_MODELS["spec"],
                        help=f"Spec-gen agent model (default: {DEFAULT_MODELS['spec']})")
    parser.add_argument("--writer-model", default=DEFAULT_MODELS["writer"],
                        help=f"RTL/TB writer model (default: {DEFAULT_MODELS['writer']})")
    parser.add_argument("--api-key", default=None,
                        help="Override key for the orchestrator's provider (testing)")
    parser.add_argument("--provider", default=None,
                        help="Preferred provider during first-run setup")
    # Batch mode flags (used by RTL_Bench runner)
    parser.add_argument("--batch", action="store_true",
                        help="Non-interactive batch mode (writes --json-out)")
    parser.add_argument("--goal", default=None,
                        help="RTL goal/spec (batch mode)")
    parser.add_argument("--module-name", default=None,
                        help="Top module name hint (batch mode)")
    parser.add_argument("--max-retries", type=int, default=5,
                        help="Max retry attempts in batch mode")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="Path to write result JSON (batch mode)")
    parser.add_argument("--no-tb", action="store_true",
                        help="Skip testbench generation (batch mode; testbench provided externally)")
    args = parser.parse_args()
    import os
    os.environ["AI_MCP_ALLOWED_ROOT"] = str(args.root.resolve())

    if args.batch:
        if not args.goal:
            print("error: --batch requires --goal", file=sys.stderr)
            sys.exit(1)
        if not args.json_out:
            print("error: --batch requires --json-out", file=sys.stderr)
            sys.exit(1)
        _run_batch(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
