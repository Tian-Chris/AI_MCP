"""Entry point for `python -m ai_agent`. Parses top-level CLI flags, hands off to cli.run()."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Self-fix sys.path when invoked as a script — keeps `from ai_agent.X import Y` working
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_agent.cli import run
from ai_agent.config import DEFAULT_MODELS


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
    args = parser.parse_args()
    import os
    os.environ["AI_MCP_ALLOWED_ROOT"] = str(args.root.resolve())
    run(args)


if __name__ == "__main__":
    main()
