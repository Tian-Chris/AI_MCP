"""Tool definitions and dispatcher for the AI RTL coding agent."""
import json
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import verilator_mcp and waveform_mcp from repo root
# ---------------------------------------------------------------------------
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
import verilator_mcp
import waveform_mcp

# ---------------------------------------------------------------------------
# Waveform helper resolution: prefer public wrappers, fall back to _helpers
# ---------------------------------------------------------------------------
def _waveform_fn(public_name: str, private_name: str):
    """Return the callable from waveform_mcp, preferring the public name."""
    fn = getattr(waveform_mcp, public_name, None)
    if fn is None:
        fn = getattr(waveform_mcp, private_name, None)
    if fn is None:
        raise AttributeError(f"waveform_mcp has neither {public_name!r} nor {private_name!r}")
    return fn

_wf_list_signals     = _waveform_fn("list_signals",      "_list_signals")
_wf_find_transitions = _waveform_fn("find_transitions",  "_find_transitions")
_wf_get_signal_value = _waveform_fn("get_signal_value",  "_get_signal_value")
_wf_sample_on_clock  = _waveform_fn("sample_on_clock",   "_sample_on_clock")

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool-use JSON schema format)
# ---------------------------------------------------------------------------
TOOL_DEFS: list[dict] = [
    # ---- File / shell tools ------------------------------------------------
    {
        "name": "read_file",
        "description": "Read a text file and return its contents. Truncates at ~200 KB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write text content to a file, creating parent directories as needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Path to write."},
                "content": {"type": "string", "description": "Full text content to write."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_dir",
        "description": "List the contents of a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "bash",
        "description": "Run a bash command and return combined stdout+stderr (last 8 KB). Use for ad-hoc tasks not covered by other tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "cwd":     {"type": "string", "description": "Working directory (optional, must be inside allowed root)."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)."}
            },
            "required": ["command"]
        }
    },
    # ---- Verilator tools ---------------------------------------------------
    {
        "name": "verilator_lint",
        "description": "Lint Verilog/SystemVerilog sources with verilator --lint-only. Returns ok, stdout, stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sources":    {"type": "array",  "items": {"type": "string"}, "description": "List of source file paths."},
                "top":        {"type": "string", "description": "Top module name."},
                "flags_file": {"type": "string", "description": "Optional path to a verilator flags file."},
                "cwd":        {"type": "string", "description": "Working directory for verilator (optional)."},
                "timeout":    {"type": "integer","description": "Timeout in seconds (default 60)."}
            },
            "required": ["sources", "top"]
        }
    },
    {
        "name": "verilator_build",
        "description": "Compile Verilog sources and a C++ testbench into a simulation binary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sources":    {"type": "array",  "items": {"type": "string"}, "description": "RTL source file paths."},
                "tb":         {"type": "string", "description": "Testbench source file path."},
                "top":        {"type": "string", "description": "Top module name."},
                "flags_file": {"type": "string", "description": "Optional verilator flags file."},
                "trace":      {"type": "boolean","description": "Enable VCD tracing (default true)."},
                "cwd":        {"type": "string", "description": "Working directory (optional)."},
                "timeout":    {"type": "integer","description": "Build timeout in seconds (default 180)."}
            },
            "required": ["sources", "tb", "top"]
        }
    },
    {
        "name": "verilator_run",
        "description": "Run a compiled Verilator simulation binary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary":   {"type": "string", "description": "Path to the compiled simulation binary."},
                "cwd":      {"type": "string", "description": "Working directory for the run (optional)."},
                "plusargs": {"type": "array",  "items": {"type": "string"}, "description": "Optional plusargs (without leading +)."},
                "timeout":  {"type": "integer","description": "Timeout in seconds (default 120)."}
            },
            "required": ["binary"]
        }
    },
    {
        "name": "verilator_build_and_run",
        "description": "Build and immediately run a Verilator simulation. Returns {build: ..., run: ...}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sources":       {"type": "array",  "items": {"type": "string"}, "description": "RTL source file paths."},
                "tb":            {"type": "string", "description": "Testbench source file path."},
                "top":           {"type": "string", "description": "Top module name."},
                "flags_file":    {"type": "string", "description": "Optional verilator flags file."},
                "trace":         {"type": "boolean","description": "Enable VCD tracing (default true)."},
                "cwd":           {"type": "string", "description": "Working directory (optional)."},
                "plusargs":      {"type": "array",  "items": {"type": "string"}, "description": "Optional plusargs."},
                "build_timeout": {"type": "integer","description": "Build timeout in seconds (default 180)."},
                "run_timeout":   {"type": "integer","description": "Run timeout in seconds (default 120)."}
            },
            "required": ["sources", "tb", "top"]
        }
    },
    # ---- Waveform tools ----------------------------------------------------
    {
        "name": "waveform_list_signals",
        "description": "List all signals in a VCD file with their widths. Optionally filter by scope or glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Path to the .vcd file."},
                "scope":   {"type": "string", "description": "Optional scope prefix to filter signals."},
                "pattern": {"type": "string", "description": "Optional glob pattern to filter signal names."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "waveform_find_transitions",
        "description": "Find times where a signal has rising, falling, or any edge transition.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string",  "description": "Path to the .vcd file."},
                "signal":     {"type": "string",  "description": "Full signal name."},
                "edge":       {"type": "string",  "enum": ["rising", "falling", "any"], "description": "Edge type to detect."},
                "start_time": {"type": "string",  "description": "Optional start time (e.g. '0ns')."},
                "end_time":   {"type": "string",  "description": "Optional end time."},
                "max_results":{"type": "integer", "description": "Maximum number of transitions to return (default 50)."}
            },
            "required": ["path", "signal", "edge"]
        }
    },
    {
        "name": "waveform_get_signal_value",
        "description": "Get the value of a single signal at a specific simulation time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "Path to the .vcd file."},
                "signal": {"type": "string", "description": "Full signal name."},
                "time":   {"type": "string", "description": "Time to sample (e.g. '100ns' or integer ticks)."},
                "fmt":    {"type": "string", "enum": ["hex", "bin", "dec", "signed"], "description": "Output format (default hex)."}
            },
            "required": ["path", "signal", "time"]
        }
    },
    {
        "name": "waveform_sample_on_clock",
        "description": "Sample one or more signals at each clock edge over a cycle range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string", "description": "Path to the .vcd file."},
                "signals":    {"type": "array",  "items": {"type": "string"}, "description": "Signal names to sample."},
                "clock":      {"type": "string", "description": "Clock signal name."},
                "from_cycle": {"type": "integer","description": "First clock cycle index (0-based)."},
                "to_cycle":   {"type": "integer","description": "Last clock cycle index (exclusive)."},
                "edge":       {"type": "string", "enum": ["rising", "falling"], "description": "Clock edge to sample on (default rising)."},
                "fmt":        {"type": "string", "enum": ["hex", "bin", "dec", "signed"], "description": "Output format (default hex)."}
            },
            "required": ["path", "signals", "clock", "from_cycle", "to_cycle"]
        }
    },
]

# ---------------------------------------------------------------------------
# Path validation helper
# ---------------------------------------------------------------------------
_MAX_READ_BYTES = 200 * 1024   # 200 KB
_MAX_OUTPUT_BYTES = 8 * 1024   # 8 KB tail for bash


def _validate_path(path_str: str, allowed_root: Path) -> Path:
    """Resolve path and ensure it is inside allowed_root."""
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = allowed_root / p
    p = p.resolve()
    if not str(p).startswith(str(allowed_root.resolve())):
        raise ValueError(f"path {p} is outside allowed root {allowed_root}")
    return p


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------

def _handle_read_file(tool_input: dict, allowed_root: Path) -> str:
    p = _validate_path(tool_input["path"], allowed_root)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {p}")
    data = p.read_bytes()
    if len(data) > _MAX_READ_BYTES:
        text = data[:_MAX_READ_BYTES].decode("utf-8", errors="replace")
        return text + f"\n\n[TRUNCATED: file is {len(data)} bytes; showing first {_MAX_READ_BYTES}]"
    return data.decode("utf-8", errors="replace")


def _handle_write_file(tool_input: dict, allowed_root: Path) -> str:
    p = _validate_path(tool_input["path"], allowed_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = tool_input["content"]
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content.encode('utf-8'))} bytes to {p}"


def _handle_list_dir(tool_input: dict, allowed_root: Path) -> str:
    p = _validate_path(tool_input["path"], allowed_root)
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {p}")
    entries = sorted(os.listdir(p))
    return "\n".join(entries) if entries else "(empty)"


def _handle_bash(tool_input: dict, allowed_root: Path) -> str:
    command = tool_input["command"]
    timeout = int(tool_input.get("timeout", 60))
    cwd_str = tool_input.get("cwd")
    if cwd_str:
        cwd_path = _validate_path(cwd_str, allowed_root)
        cwd = str(cwd_path)
    else:
        cwd = str(allowed_root)
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        combined = (e.stdout or "") + (e.stderr or "") + f"\n[TIMED OUT after {timeout}s]"
    # Return last 8 KB
    if len(combined) > _MAX_OUTPUT_BYTES:
        combined = "[...truncated...]\n" + combined[-_MAX_OUTPUT_BYTES:]
    return combined


def _handle_verilator_lint(tool_input: dict, allowed_root: Path) -> str:
    result = verilator_mcp.verilator_lint(**tool_input)
    return json.dumps(result, indent=2)


def _handle_verilator_build(tool_input: dict, allowed_root: Path) -> str:
    result = verilator_mcp.verilator_build(**tool_input)
    return json.dumps(result, indent=2)


def _handle_verilator_run(tool_input: dict, allowed_root: Path) -> str:
    result = verilator_mcp.verilator_run(**tool_input)
    return json.dumps(result, indent=2)


def _handle_verilator_build_and_run(tool_input: dict, allowed_root: Path) -> str:
    result = verilator_mcp.verilator_build_and_run(**tool_input)
    return json.dumps(result, indent=2)


def _handle_waveform_list_signals(tool_input: dict, allowed_root: Path) -> str:
    result = _wf_list_signals(**tool_input)
    return json.dumps(result, indent=2)


def _handle_waveform_find_transitions(tool_input: dict, allowed_root: Path) -> str:
    result = _wf_find_transitions(**tool_input)
    return json.dumps(result, indent=2)


def _handle_waveform_get_signal_value(tool_input: dict, allowed_root: Path) -> str:
    result = _wf_get_signal_value(**tool_input)
    return json.dumps(result, indent=2)


def _handle_waveform_sample_on_clock(tool_input: dict, allowed_root: Path) -> str:
    result = _wf_sample_on_clock(**tool_input)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
_HANDLERS = {
    "read_file":                _handle_read_file,
    "write_file":               _handle_write_file,
    "list_dir":                 _handle_list_dir,
    "bash":                     _handle_bash,
    "verilator_lint":           _handle_verilator_lint,
    "verilator_build":          _handle_verilator_build,
    "verilator_run":            _handle_verilator_run,
    "verilator_build_and_run":  _handle_verilator_build_and_run,
    "waveform_list_signals":    _handle_waveform_list_signals,
    "waveform_find_transitions":_handle_waveform_find_transitions,
    "waveform_get_signal_value":_handle_waveform_get_signal_value,
    "waveform_sample_on_clock": _handle_waveform_sample_on_clock,
}


def execute_tool(name: str, tool_input: dict, allowed_root: Path) -> str:
    """Dispatch a tool call by name, returning a string result.

    On error, returns a JSON error object so the model can recover.
    """
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name!r}. Available: {list(_HANDLERS)}")
        return handler(tool_input, allowed_root)
    except Exception as exc:
        return json.dumps({"is_error": True, "content": str(exc)})
