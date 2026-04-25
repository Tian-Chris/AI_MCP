#!/usr/bin/env python3
"""AI RTL coding agent — drives any LLM provider (via --model provider/model) against the
Verilator/waveform MCP tools. Single-shot CLI preserved; core loop exposed as run_turn() for
future REPL use."""
import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

# Ensure repo root is on sys.path so `from ai_agent.*` works regardless of cwd or PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_agent.llm import LLMClient
from ai_agent.session import Session
from ai_agent.system_prompt import SYSTEM_PROMPT
from ai_agent.tools import TOOL_DEFS, execute_tool


def run_turn(
    session: Session,
    llm: LLMClient,
    tools: list[dict],          # Anthropic-style TOOL_DEFS — llm.py handles conversion
    system: str,                 # SYSTEM_PROMPT
    root: Path,                  # for tool sandbox via execute_tool
    max_inner_turns: int = 30,
    on_event: Optional[Callable[[dict], None]] = None,
) -> str:
    """Run the agentic loop until end_turn or max_inner_turns. Returns the final assistant text.
    The session is mutated in place — new messages are appended.
    on_event (optional) gets called for observability: {"type": "tool_call", "name", "input"} or
    {"type": "tool_result", "name", "ok": bool} etc."""
    for _ in range(max_inner_turns):
        response = llm.call(system, session.messages, tools, max_tokens=8000)

        # Build the OpenAI-style tool_calls list for the assistant message
        tc_list = []
        for tc in response.tool_calls:
            tc_list.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
            })
        session.add_assistant(text=response.text, tool_calls=tc_list or None)

        if on_event:
            on_event({
                "type": "usage",
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
                "cache_read": response.usage.cache_read,
            })

        if response.stop_reason == "end_turn" or not response.tool_calls:
            return response.text or ""

        # Execute tool calls
        for tc in response.tool_calls:
            if on_event:
                on_event({"type": "tool_call", "name": tc.name, "input": tc.input})
            result_text = execute_tool(tc.name, tc.input, root)
            session.add_tool_result(tc.id, result_text)
            if on_event:
                on_event({"type": "tool_result", "name": tc.name, "ok": True})

    # fell through max_inner_turns
    return "(hit max_inner_turns without end_turn)"


def _cli():
    parser = argparse.ArgumentParser(description="AI RTL coding agent")
    parser.add_argument("task")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--model", default=None, help="provider/model, e.g. anthropic/claude-haiku-4-5-20251001")
    args = parser.parse_args()

    from ai_agent.config import resolve_api_key, DEFAULT_MODELS, provider_from_model

    model = args.model or "anthropic/claude-haiku-4-5-20251001"  # preserve current single-shot default
    provider = provider_from_model(model)
    resolved = resolve_api_key(provider)
    if resolved is None:
        print(f"No API key found for {provider}. Run `python -m ai_agent` for first-run setup.", file=sys.stderr)
        sys.exit(1)
    api_key, _src = resolved

    llm = LLMClient(model=model, api_key=api_key)
    session = Session(models={"orchestrator": model})
    session.add_user(args.task)

    def event(e):
        if e["type"] == "tool_call":
            print(f"  → {e['name']}({json.dumps(e['input'])[:80]})", file=sys.stderr)

    text = run_turn(session, llm, TOOL_DEFS, SYSTEM_PROMPT, args.root, args.max_turns, on_event=event)
    print(text)


if __name__ == "__main__":
    _cli()
