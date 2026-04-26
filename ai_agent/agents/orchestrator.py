"""Top-level orchestrator. Drives the phase machine via tool calls."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Callable, Optional

from ai_agent._display import console as _console
from ai_agent.session import Session, Phase
from ai_agent.llm import LLMClient
from ai_agent.agents import spec, writers, tester
from ai_agent import tools as agent_tools

_VERBOSE = os.environ.get("AI_MCP_VERBOSE", "").strip().lower() in ("1", "true", "yes")


# 5 tools total: 3 phase-transition + 2 read-only file
ORCHESTRATOR_TOOL_DEFS = [
    {
        "name": "start_spec",
        "description": "Hand the user's stated goal to the Spec agent. Returns a structured Spec dict (module_name, ports, behavior, clock, reset, rtl_path, tb_path, test_strategy). Call this once the user has clearly stated what they want to build. Optionally pass rtl_path/tb_path if the user specified them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal":     {"type": "string", "description": "Natural-language goal from the user"},
                "rtl_path": {"type": "string", "description": "Optional explicit RTL output path"},
                "tb_path":  {"type": "string", "description": "Optional explicit testbench output path"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "dispatch_writers",
        "description": "Fan out RTL writer + testbench writer in parallel. Writes Verilog files at spec.rtl_path and spec.tb_path. Returns the absolute file paths written. Call AFTER the user has approved the spec returned from start_spec.",
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object", "description": "The Spec dict returned from start_spec (pass it back verbatim)"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run lint -> build -> simulate on the written files. Returns {phase, passed, stdout, stderr} where phase is 'lint'|'build'|'sim'|'pass'. Call IMMEDIATELY after dispatch_writers returns — no user gate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object", "description": "Same Spec dict used for dispatch_writers"},
            },
            "required": ["spec"],
        },
    },
]


def _build_llm(role: str, session: Session) -> LLMClient:
    """Build an LLMClient for a given role using session.models[role]."""
    from ai_agent import config as cfg
    model = session.models[role]
    provider = cfg.provider_from_model(model)
    resolved = cfg.resolve_api_key(provider)
    api_key = resolved[0] if resolved else None
    return LLMClient(model=model, api_key=api_key)


def _load_system_prompt() -> str:
    p = Path(__file__).resolve().parent.parent / "prompts" / "orchestrator.md"
    return p.read_text()


def _build_tools_and_handlers(session: Session, root: Path):
    """Returns (tool_defs, handlers) where handlers is dict[name -> callable(input_dict) -> str]."""

    def h_start_spec(args: dict) -> str:
        session.retry_count = 0
        session.set_phase(Phase.SPEC)
        llm_spec = _build_llm("spec", session)
        result = spec.run_spec(
            goal=args["goal"],
            llm=llm_spec,
            root=root,
            rtl_path=args.get("rtl_path"),
            tb_path=args.get("tb_path"),
        )
        session.spec = result
        return json.dumps(result)

    def h_dispatch_writers(args: dict) -> str:
        session.retry_count = 0
        session.set_phase(Phase.WRITE)
        llm_writer = _build_llm("writer", session)
        paths = writers.write_both(spec=args["spec"], llm=llm_writer, root=root)
        return json.dumps({"rtl": str(paths["rtl"]), "tb": str(paths["tb"])})

    def h_run_tests(args: dict) -> str:
        session.set_phase(Phase.TEST)
        session.retry_count += 1
        result = tester.run_tests(args["spec"], root)
        session.last_test = result
        stdout_tail = (result.stdout or "")[-1500:]
        stderr_tail = (result.stderr or "")[-1500:]
        session.tests = list(result.tests)
        if session.tests:
            from ai_agent._display import show_test_dashboard
            show_test_dashboard(session.tests)
        if result.passed:
            session.reset_retry()
            session.set_phase(Phase.IDLE)
            return json.dumps({
                "phase":  result.phase,
                "passed": True,
                "stdout": stdout_tail,
                "stderr": stderr_tail,
            })
        if session.retry_count >= 3:
            session.set_phase(Phase.IDLE)
            return json.dumps({
                "phase":              result.phase,
                "passed":             False,
                "stdout":             stdout_tail,
                "stderr":             stderr_tail,
                "max_retries_reached": True,
                "message":            "Max retries (3) reached. Escalating to user.",
            })
        # Truncate logs to keep context manageable
        return json.dumps({
            "phase":  result.phase,
            "passed": result.passed,
            "stdout": stdout_tail,
            "stderr": stderr_tail,
        })

    def h_read_file(args: dict) -> str:
        return agent_tools.execute_tool("read_file", args, root)

    def h_list_dir(args: dict) -> str:
        return agent_tools.execute_tool("list_dir", args, root)

    handlers = {
        "start_spec":       h_start_spec,
        "dispatch_writers": h_dispatch_writers,
        "run_tests":        h_run_tests,
        "read_file":        h_read_file,
        "list_dir":         h_list_dir,
    }

    # Pull read_file and list_dir defs from tools.TOOL_DEFS by name
    read_file_def = next((t for t in agent_tools.TOOL_DEFS if t["name"] == "read_file"), None)
    list_dir_def  = next((t for t in agent_tools.TOOL_DEFS if t["name"] == "list_dir"),  None)
    tool_defs = list(ORCHESTRATOR_TOOL_DEFS)
    if read_file_def: tool_defs.append(read_file_def)
    if list_dir_def:  tool_defs.append(list_dir_def)

    return tool_defs, handlers


def _run_orchestrator_inner(
    session: Session,
    llm: LLMClient,
    system: str,
    tool_defs: list,
    handlers: dict,
    max_inner_turns: int,
    on_event: Optional[Callable[[dict], None]],
    status,
) -> str:
    """Inner loop — drives the orchestrator until end_turn or max_inner_turns."""
    for _ in range(max_inner_turns):
        if status:
            status.update("[dim]Thinking...[/dim]")
        response = llm.call(system, session.messages, tool_defs, max_tokens=8000)

        # Build OpenAI-style tool_calls list for the assistant message
        tc_list = []
        for tc in response.tool_calls:
            tc_list.append({
                "id":   tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
            })
        session.add_assistant(text=response.text, tool_calls=tc_list or None)

        if on_event:
            on_event({
                "type":       "usage",
                "input":      response.usage.input_tokens,
                "output":     response.usage.output_tokens,
                "cache_read": response.usage.cache_read,
            })

        if response.stop_reason == "end_turn" or not response.tool_calls:
            return response.text or ""

        # Execute tool calls
        for tc in response.tool_calls:
            if on_event:
                on_event({"type": "tool_call", "name": tc.name, "input": tc.input})
            if status:
                status.update(f"[dim]Running {tc.name}...[/dim]")
            try:
                result_text = handlers[tc.name](tc.input)
                ok = True
            except KeyError:
                result_text = f"ERROR: unknown tool {tc.name!r}"
                ok = False
            except Exception as e:
                result_text = f"ERROR: {type(e).__name__}: {e}"
                ok = False
            session.add_tool_result(tc.id, result_text)
            if on_event:
                on_event({"type": "tool_result", "name": tc.name, "ok": ok})

    return "(hit max_inner_turns without end_turn)"


def run_orchestrator_turn(
    session: Session,
    llm: LLMClient,
    root: Path,
    max_inner_turns: int = 20,
    on_event: Optional[Callable[[dict], None]] = None,
) -> str:
    """Run the orchestrator loop until end_turn or max_inner_turns. Mutates session.
    Returns the final assistant text."""
    system = _load_system_prompt()
    tool_defs, handlers = _build_tools_and_handlers(session, root)
    if _VERBOSE:
        return _run_orchestrator_inner(session, llm, system, tool_defs, handlers, max_inner_turns, on_event, status=None)
    with _console.status("[dim]Thinking...[/dim]", spinner="dots") as status:
        return _run_orchestrator_inner(session, llm, system, tool_defs, handlers, max_inner_turns, on_event, status=status)
