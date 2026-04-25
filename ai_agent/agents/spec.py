"""Spec generation agent. LLM-driven sub-conversation that produces a Spec dict."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_agent.llm import LLMClient
from ai_agent import tools as agent_tools


# ---------------------------------------------------------------------------
# Tool definitions for the spec sub-agent
# ---------------------------------------------------------------------------

_FINALIZE_SPEC_TOOL: dict = {
    "name": "finalize_spec",
    "description": (
        "Commit the completed hardware spec. Call this exactly once when ready. "
        "This ends the spec agent's turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "module_name": {
                "type": "string",
                "description": "Verilog module name (snake_case).",
            },
            "ports": {
                "type": "array",
                "description": "List of port descriptors.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":      {"type": "string"},
                        "direction": {"type": "string", "enum": ["input", "output", "inout"]},
                        "width":     {"type": "integer", "description": "Bit width (1 = scalar)."},
                    },
                    "required": ["name", "direction", "width"],
                },
            },
            "behavior": {
                "type": "string",
                "description": "1–2 sentence natural-language description of what the module does.",
            },
            "clock": {
                "type": "string",
                "description": "Name of the clock port (e.g. 'clk').",
            },
            "reset": {
                "type": "string",
                "description": "Name of the reset port (e.g. 'rst').",
            },
            "test_strategy": {
                "type": "string",
                "description": "1–2 sentence description of what the testbench must verify.",
            },
        },
        "required": ["module_name", "ports", "behavior", "clock", "reset", "test_strategy"],
    },
}

_READ_FILE_DEF = next(
    t for t in agent_tools.TOOL_DEFS if t["name"] == "read_file"
)
_LIST_DIR_DEF = next(
    t for t in agent_tools.TOOL_DEFS if t["name"] == "list_dir"
)

_SPEC_TOOL_DEFS: list[dict] = [_READ_FILE_DEF, _LIST_DIR_DEF, _FINALIZE_SPEC_TOOL]


# ---------------------------------------------------------------------------
# System prompt loader
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    p = Path(__file__).resolve().parent.parent / "prompts" / "spec.md"
    return p.read_text()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_spec(
    goal: str,
    llm: LLMClient,
    root: Path,
    rtl_path: str | None = None,
    tb_path: str | None = None,
    max_inner_turns: int = 8,
) -> dict[str, Any]:
    """Run an LLM-driven spec sub-conversation and return a Spec-shaped dict.

    The agent may call read_file / list_dir to inspect existing code, then
    must call finalize_spec exactly once to commit the spec.

    Returns a JSON-serializable dict matching the Spec dataclass shape
    (see ai_agent/session.py).
    """
    system = _load_system_prompt()

    initial_user_msg = (
        f"Project root: {root}\n"
        f"User goal: {goal}\n"
        f"Default rtl_path: {rtl_path or '<not specified>'}\n"
        f"Default tb_path: {tb_path or '<not specified>'}\n\n"
        "Produce the spec by calling finalize_spec when ready. "
        "Use read_file/list_dir if you need to inspect existing code first."
    )

    messages: list[dict] = [{"role": "user", "content": initial_user_msg}]

    for turn in range(max_inner_turns):
        response = llm.call(system, messages, _SPEC_TOOL_DEFS, max_tokens=4096)

        # Build OpenAI-style tool_calls list for the assistant message
        tc_list = []
        for tc in response.tool_calls:
            tc_list.append({
                "id":   tc.id,
                "type": "function",
                "function": {
                    "name":      tc.name,
                    "arguments": json.dumps(tc.input),
                },
            })

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": response.text}
        if tc_list:
            assistant_msg["tool_calls"] = tc_list
        messages.append(assistant_msg)

        # end_turn with no tool calls — agent finished without calling finalize_spec
        if response.stop_reason == "end_turn" or not response.tool_calls:
            raise RuntimeError(
                "spec agent ended without calling finalize_spec "
                f"(stop_reason={response.stop_reason!r}, "
                f"text={response.text!r})"
            )

        # Process each tool call
        for tc in response.tool_calls:
            if tc.name == "finalize_spec":
                # Assemble and return the final spec dict
                args = tc.input
                module_name = args["module_name"]

                resolved_rtl = rtl_path or str(root / f"{module_name}.v")
                resolved_tb  = tb_path  or str(root / f"tb_{module_name}.v")

                return {
                    "module_name":    module_name,
                    "ports":          args["ports"],
                    "behavior":       args["behavior"],
                    "clock":          args["clock"],
                    "reset":          args["reset"],
                    "rtl_path":       resolved_rtl,
                    "tb_path":        resolved_tb,
                    "test_strategy":  args["test_strategy"],
                }

            elif tc.name in ("read_file", "list_dir"):
                result_text = agent_tools.execute_tool(tc.name, tc.input, root)
            else:
                result_text = json.dumps(
                    {"is_error": True, "content": f"unknown tool: {tc.name!r}"}
                )

            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result_text,
            })

    raise RuntimeError(
        f"spec agent exceeded max_inner_turns={max_inner_turns} without calling finalize_spec"
    )
