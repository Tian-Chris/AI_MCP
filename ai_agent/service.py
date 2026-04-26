"""Thin JSON-line subprocess wrapper around Orchestrator for VSCodium extension (Path B)."""
import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path

from ai_agent.session import Session, Phase
from ai_agent.agents.orchestrator import run_orchestrator_turn, _build_llm
from ai_agent.config import DEFAULT_MODELS


def _emit(event: dict) -> None:
    """Write one JSON event line to stdout and flush."""
    print(json.dumps(event), flush=True)


def _spec_to_dict(spec) -> dict:
    """Convert Spec dataclass to JSON-serialisable dict (Paths -> strings)."""
    d = dataclasses.asdict(spec)
    for key in ("rtl_path", "tb_path"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    return d


def _test_to_dict(tr) -> dict:
    """Convert TestResult dataclass to JSON-serialisable dict (Paths -> strings)."""
    d = dataclasses.asdict(tr)
    if d.get("waveform_path") is not None:
        d["waveform_path"] = str(d["waveform_path"])
    return d


def main() -> None:
    # Block until init payload arrives; it has no "type" field.
    raw_init = sys.stdin.readline()
    if not raw_init:
        sys.exit(0)
    init = json.loads(raw_init)
    root = Path(init["root"]).resolve()
    os.environ["AI_MCP_ALLOWED_ROOT"] = str(root)

    models = {**DEFAULT_MODELS, **init.get("models", {})}
    session = Session(models=models)

    # _build_llm reads api key from env (ANTHROPIC_API_KEY etc.) — no interactive prompt.
    orch_llm = _build_llm("orchestrator", session)
    _emit({"type": "ready", "phase": session.phase.name})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            rtype = req.get("type")

            if rtype == "user_message":
                session.add_user(req["text"])

                def on_event(d: dict) -> None:
                    _emit({"type": "tool_event", **d})

                reply = run_orchestrator_turn(session, orch_llm, root, on_event=on_event)
                _emit({
                    "type":      "assistant_message",
                    "text":      reply,
                    "phase":     session.phase.name,
                    "spec":      _spec_to_dict(session.spec) if session.spec else None,
                    "last_test": _test_to_dict(session.last_test) if session.last_test else None,
                })

            elif rtype == "reset":
                session.reset()
                _emit({"type": "reset_ok", "phase": session.phase.name})

            elif rtype == "set_model":
                session.models[req["role"]] = req["model"]
                if req["role"] == "orchestrator":
                    orch_llm = _build_llm("orchestrator", session)
                _emit({"type": "model_set"})

            else:
                _emit({"type": "error", "message": f"unknown request type: {req!r}"})

        except Exception:
            _emit({"type": "error", "message": traceback.format_exc()})


if __name__ == "__main__":
    main()
