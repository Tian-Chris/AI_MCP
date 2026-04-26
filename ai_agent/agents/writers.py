"""Parallel RTL + testbench writers driven by LLM tool calls."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION
from pathlib import Path
from typing import Any

from ai_agent.llm import LLMClient
from ai_agent._display import show_file_write

# Tool definition shared by both writers
_SUBMIT_CODE_TOOL: dict[str, Any] = {
    "name": "submit_code",
    "description": "Submit the generated Verilog source code.",
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The complete Verilog source code, raw — no markdown fences.",
            }
        },
        "required": ["code"],
    },
}


def _strip_fences(code: str) -> str:
    """Remove accidental markdown code fences (```verilog ... ``` or ``` ... ```)."""
    # Match optional language tag after opening fence
    pattern = re.compile(r"^```[a-zA-Z]*\n(.*?)^```\s*$", re.DOTALL | re.MULTILINE)
    match = pattern.search(code)
    if match:
        return match.group(1)
    return code


def _load_prompt(filename: str) -> str:
    """Load a system prompt from the prompts/ directory next to this package."""
    prompts_dir = Path(__file__).parent.parent / "prompts"
    return (prompts_dir / filename).read_text(encoding="utf-8")


def _call_with_retry(
    llm: LLMClient,
    system: str,
    messages: list[dict],
    kind: str,
) -> str:
    """Call the LLM, expecting a submit_code tool call. Retry once if not received."""
    response = llm.call(
        system=system,
        messages=messages,
        tools=[_SUBMIT_CODE_TOOL],
        max_tokens=8000,
    )

    # Check for submit_code tool call in first response
    for tc in response.tool_calls:
        if tc.name == "submit_code":
            return tc.input["code"]

    # No tool call — retry once with a nudge
    nudge_messages = list(messages)
    # Append the assistant's text response (if any) and a user nudge
    if response.text:
        nudge_messages.append({"role": "assistant", "content": response.text})
    nudge_messages.append(
        {
            "role": "user",
            "content": "You must call submit_code with the Verilog source. Please do so now.",
        }
    )

    retry_response = llm.call(
        system=system,
        messages=nudge_messages,
        tools=[_SUBMIT_CODE_TOOL],
        max_tokens=8000,
    )

    for tc in retry_response.tool_calls:
        if tc.name == "submit_code":
            return tc.input["code"]

    raise RuntimeError(f"{kind} writer didn't submit code")


def _write_rtl(spec: dict[str, Any], llm: LLMClient, root: Path) -> Path:
    """Generate RTL via LLM and write to the path specified in spec."""
    system = _load_prompt("rtl_writer.md")
    messages = [
        {
            "role": "user",
            "content": (
                f"Spec:\n{json.dumps(spec, indent=2)}\n\n"
                "Write the RTL now and submit via the submit_code tool."
            ),
        }
    ]

    code = _call_with_retry(llm, system, messages, kind="RTL")
    code = _strip_fences(code)

    rtl_path = Path(spec["rtl_path"])
    if not rtl_path.is_absolute():
        rtl_path = root / rtl_path

    rtl_path.parent.mkdir(parents=True, exist_ok=True)
    _old = rtl_path.read_text(encoding="utf-8") if rtl_path.exists() else None
    rtl_path.write_text(code, encoding="utf-8")
    show_file_write(rtl_path, _old, code)
    return rtl_path


def _write_tb(spec: dict[str, Any], llm: LLMClient, root: Path) -> Path:
    """Generate testbench via LLM and write to the path specified in spec."""
    system = _load_prompt("tb_writer.md")
    messages = [
        {
            "role": "user",
            "content": (
                f"Spec:\n{json.dumps(spec, indent=2)}\n\n"
                "Write the TB now and submit via the submit_code tool."
            ),
        }
    ]

    code = _call_with_retry(llm, system, messages, kind="TB")
    code = _strip_fences(code)

    tb_path = Path(spec["tb_path"])
    if not tb_path.is_absolute():
        tb_path = root / tb_path

    tb_path.parent.mkdir(parents=True, exist_ok=True)
    _old = tb_path.read_text(encoding="utf-8") if tb_path.exists() else None
    tb_path.write_text(code, encoding="utf-8")
    show_file_write(tb_path, _old, code)
    return tb_path


def write_both(spec: dict[str, Any], llm: LLMClient, root: Path) -> dict[str, Path]:
    """Write LLM-generated RTL and testbench files concurrently.

    Submits both writer jobs to a ThreadPoolExecutor(max_workers=2), waits for
    both to complete, and returns {'rtl': Path, 'tb': Path}.

    Raises RuntimeError (or any exception from the writers) if either job fails.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        rtl_future = executor.submit(_write_rtl, spec, llm, root)
        tb_future = executor.submit(_write_tb, spec, llm, root)

        done, not_done = wait(
            [rtl_future, tb_future],
            return_when=FIRST_EXCEPTION,
        )

        # Cancel any pending futures (no-op if already done)
        for f in not_done:
            f.cancel()

        # Re-raise exceptions from completed futures
        for f in done:
            exc = f.exception()
            if exc is not None:
                raise exc

        # Wait for the remaining future if it wasn't cancelled
        if not_done:
            done2, _ = wait(not_done)
            for f in done2:
                exc = f.exception()
                if exc is not None:
                    raise exc

    return {"rtl": rtl_future.result(), "tb": tb_future.result()}
