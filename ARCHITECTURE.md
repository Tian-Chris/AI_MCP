# AI_MCP Architecture

> Read the README first. This document covers internal design: data flow, agent contracts,
> extension points, and trade-offs. It assumes you will integrate this system into a VSCode
> extension and need to know what you can change vs. what will break the loop.

---

## 1. Overview

AI_MCP is a REPL-based coding agent that converts a natural-language hardware description
into verified Verilog. The orchestrator owns the entire user conversation from first message
to final pass/fail report. It never writes code itself; instead it dispatches three
specialized sub-agents — spec, writers, and tester — as structured tool calls within the
same Python process.

The session-level phase machine (IDLE → SPEC → WRITE → TEST) is enforced exclusively by
the orchestrator's system prompt and by the tool-gating rules embedded in that prompt.
There are no hard state checks in Python that block a tool call from the wrong phase. This
is deliberate: the orchestrator LLM is the only agent with conversation context, so
prompt-level enforcement is cheaper to evolve than code-level guards. A VSCode extension
can inject additional context or reroute tool calls without touching session state.

---

## 2. Phase Machine

```
               user states goal
  ┌──────────┐ ──────────────────▶ ┌──────────┐
  │   IDLE   │                     │   SPEC   │
  │          │ ◀────── /reset ───── │          │
  └──────────┘                     └─────┬────┘
       ▲                                 │ user approves spec
       │                                 ▼
       │                           ┌──────────┐
       │  pass / max_retries       │  WRITE   │
       │  ◀──────────────────────  │          │
       │                           └─────┬────┘
       │                                 │ writers return (automatic, no user gate)
       │                                 ▼
       │           retry          ┌──────────┐
       │    ┌──────────────────── │   TEST   │
       │    │  (ask user first;   │          │
       │    └──────────────────▶  │ cap = 3) │
       │                          └─────┬────┘
       │   abort / max_retries_reached  │
       └────────────────────────────────┘
```

**IDLE** — Orchestrator greets the user and waits for a goal. Valid tool calls: `read_file`,
`list_dir`. The prompt indicator shows `[idle]>`. No spec or test state is set.

**SPEC** — `start_spec` is in-flight or has just returned. The spec sub-agent runs a bounded
sub-conversation (up to 8 inner turns), may call `read_file`/`list_dir`, then commits via
`finalize_spec`. The orchestrator receives the result as a JSON dict, presents it to the
user, and waits for approval. Valid tool calls: `read_file`, `list_dir`, `start_spec`.

**WRITE** — `dispatch_writers` is in-flight. Two LLM workers run concurrently in a
`ThreadPoolExecutor`. Each calls `submit_code` once and writes a `.v` file to disk.
No user interaction while this phase is active. After writers return, the orchestrator
immediately calls `run_tests` without asking the user.

**TEST** — `run_tests` is in-flight or has just returned. The tester runs
lint → build → sim sequentially; stops at first failure. On pass: phase resets to IDLE.
On failure: orchestrator summarizes the error and asks the user what to do (retry writers,
revise spec, or abort). Retry budget is 3; on the third failure the tool returns
`max_retries_reached: true` and forces `phase = IDLE`. The orchestrator must stop
calling tools at that point.

---

## 3. The Four Agents

### 3.1 Orchestrator

| Field          | Value |
|----------------|-------|
| File           | `ai_agent/agents/orchestrator.py` |
| System prompt  | `ai_agent/prompts/orchestrator.md` |
| Default model  | `anthropic/claude-opus-4-7` |
| Role key       | `"orchestrator"` in `session.models` |

```python
run_orchestrator_turn(
    session: Session,
    llm: LLMClient,
    root: Path,
    max_inner_turns: int = 20,
    on_event: Optional[Callable[[dict], None]] = None,
) -> str
```

Owns the user-facing conversation. Loops up to `max_inner_turns` times: calls the LLM,
dispatches any tool calls via the closure-based handler map, appends results to
`session.messages`, then loops again until `stop_reason == "end_turn"` (no pending tool
calls). Returns the final assistant text. Mutates `session` in-place.

The `on_event` hook fires for every tool call, tool result, and usage record. It currently
receives dicts with `type` in `{"tool_call", "tool_result", "usage"}`. This is the primary
injection point for a VSCode extension to stream progress without touching the return path.

**5 tools exposed to the orchestrator**: `start_spec`, `dispatch_writers`, `run_tests`,
`read_file`, `list_dir`.

---

### 3.2 Spec Agent

| Field          | Value |
|----------------|-------|
| File           | `ai_agent/agents/spec.py` |
| System prompt  | `ai_agent/prompts/spec.md` |
| Default model  | `anthropic/claude-opus-4-7` |
| Role key       | `"spec"` in `session.models` |

```python
run_spec(
    goal: str,
    llm: LLMClient,
    root: Path,
    rtl_path: str | None = None,
    tb_path: str | None = None,
    max_inner_turns: int = 8,
) -> dict[str, Any]
```

Runs a self-contained sub-conversation (its own local `messages` list, not shared with the
orchestrator). Can call `read_file` and `list_dir` to inspect existing code. Must call
`finalize_spec` exactly once; that call terminates the loop and returns the spec dict.
If the agent reaches `end_turn` without calling `finalize_spec`, raises `RuntimeError`.
If it exceeds `max_inner_turns`, raises `RuntimeError`.

Returns a JSON-serializable dict with keys:
`module_name`, `ports` (list of `{name, direction, width}`), `behavior`, `clock`, `reset`,
`rtl_path`, `tb_path`, `test_strategy`.

---

### 3.3 Writers

| Field          | Value |
|----------------|-------|
| File           | `ai_agent/agents/writers.py` |
| RTL prompt     | `ai_agent/prompts/rtl_writer.md` |
| TB prompt      | `ai_agent/prompts/tb_writer.md` |
| Default model  | `anthropic/claude-sonnet-4-6` |
| Role key       | `"writer"` in `session.models` |

```python
write_both(
    spec: dict[str, Any],
    llm: LLMClient,
    root: Path,
) -> {"rtl": Path, "tb": Path}
```

Submits `_write_rtl` and `_write_tb` to a `ThreadPoolExecutor(max_workers=2)`. Both workers
are given the same `LLMClient` instance and call it with a single-shot conversation
(user message containing the Spec JSON). Each worker calls `submit_code(code: str)` once.
If the LLM doesn't issue that tool call on the first attempt, the worker appends a nudge
message and retries once. On second failure, raises `RuntimeError`.

Both workers strip accidental markdown fences from the submitted code before writing.
Files are written to `spec["rtl_path"]` and `spec["tb_path"]`; relative paths are resolved
against `root`.

---

### 3.4 Tester

| Field          | Value |
|----------------|-------|
| File           | `ai_agent/agents/tester.py` |
| LLM            | None — deterministic pipeline only |

```python
run_tests(
    spec: dict[str, Any],
    root: Path,
) -> TestResult
```

Three sequential stages. Stops and returns on the first failure.

1. `verilator_lint(sources=[rtl, tb], top="tb_{module_name}", cwd=root)` — calls
   `verilator --lint-only`. Returns `TestResult(phase="lint", ...)` on failure.
2. `verilator_build(tb=tb_path, sources=[rtl], top="tb_{module_name}", trace=True, cwd=root)`
   — compiles to `obj_dir/Vtb_{module_name}`. Returns `TestResult(phase="build", ...)`
   on failure. Extracts `binary_path` from the build result.
3. `verilator_run(binary=binary_path, cwd=root)` — executes the binary. Returns
   `TestResult(phase="sim", ...)` on failure.

On full pass: returns `TestResult(phase="pass", passed=True, ...)` with
`waveform_path=root/"wave.vcd"` if the VCD file exists.

---

## 4. Data Flow

```
User text
    │
    ▼
session.add_user(text)
    │
    ▼
run_orchestrator_turn(session, llm, root)
    │
    ├── llm.call(system, session.messages, tool_defs, max_tokens=8000)
    │       │
    │       ▼
    │   Response {text, tool_calls, stop_reason, usage}
    │       │
    │   [stop_reason == "end_turn"] ──────────────────────▶ return response.text
    │       │
    │   [tool_calls present]
    │       │
    │       ▼
    │   session.add_assistant(text, tool_calls)
    │       │
    │       ▼
    │   for each tool_call:
    │     handlers[tc.name](tc.input)  ← closure captures session, root
    │         │
    │         ├── h_start_spec  → spec.run_spec(...)  → session.spec = result
    │         ├── h_dispatch_writers → writers.write_both(...)
    │         ├── h_run_tests → tester.run_tests(...)  → session.last_test = result
    │         ├── h_read_file  → agent_tools.execute_tool(...)
    │         └── h_list_dir   → agent_tools.execute_tool(...)
    │         │
    │         ▼
    │     session.add_tool_result(tc.id, result_json)
    │         │
    │         └──── on_event({type:"tool_result", ...})
    │
    └── loop back to llm.call(...)
```

**Cross-agent data passing:**

| From | To | How |
|------|----|-----|
| `h_start_spec` return value | `session.spec` | Handler assigns `session.spec = result` (dict) |
| `session.spec` | `dispatch_writers` | Orchestrator passes spec dict as tool call argument |
| same spec dict | `run_tests` | Orchestrator passes it again verbatim |
| `run_tests` result | `session.last_test` | Handler assigns `session.last_test = result` |

The spec dict is the canonical handoff artifact. It round-trips through JSON (tool call
arguments are JSON strings) so it is always a plain Python dict by the time any handler
receives it — not a `Spec` dataclass instance.

---

## 5. Key Data Structures

These are defined verbatim in `ai_agent/session.py`.

```python
class Phase(Enum):
    IDLE  = "idle"
    SPEC  = "spec"
    WRITE = "write"
    TEST  = "test"
```

Created at import time. `session.phase` is the canonical value. The REPL prompt uses
`session.phase.value` as its prefix string.

---

```python
@dataclass
class Port:
    name: str
    direction: Literal["input", "output", "inout"]
    width: int = 1   # 1 = scalar, >1 = bus width
```

Created by the spec agent (as a dict, not this dataclass — the dataclass is a type
reference). Never mutated after spec finalization.

---

```python
@dataclass
class Spec:
    """Frozen handoff between Spec agent and Writer agents."""
    module_name: str
    ports: list[Port] = field(default_factory=list)
    behavior: str = ""
    clock: str = "clk"
    reset: Optional[str] = "rst"
    rtl_path: Optional[Path] = None
    tb_path: Optional[Path] = None
    test_strategy: str = ""
```

Exists as a documentation type only. At runtime, the spec always lives as a plain dict
(returned by `run_spec`, stored in `session.spec`, passed as tool arguments). The dict keys
match the dataclass field names.

---

```python
@dataclass
class TestResult:
    phase: Literal["lint", "build", "sim", "waveform", "pass"]
    passed: bool
    stdout: str = ""
    stderr: str = ""
    waveform_path: Optional[Path] = None
```

Created by `tester.run_tests`. Stored in `session.last_test`. The `phase` field identifies
which pipeline stage produced this result. `"waveform"` is defined here but not yet
produced by the current tester pipeline.

---

```python
@dataclass
class Session:
    """In-memory state for one REPL session. Lost on /exit."""
    messages: list[dict] = field(default_factory=list)
    phase: Phase = Phase.IDLE
    models: dict[str, str] = field(default_factory=dict)
    spec: Optional[Spec] = None
    last_test: Optional[TestResult] = None
    retry_count: int = 0
```

The single shared mutable object for a session. All methods are:

| Method | Effect |
|--------|--------|
| `add_user(text)` | Appends `{"role":"user","content":text}` |
| `add_assistant(text, tool_calls)` | Appends assistant turn in OpenAI format |
| `add_tool_result(id, content)` | Appends `{"role":"tool","tool_call_id":id,"content":content}` |
| `reset()` | Clears messages, phase, spec, last_test, retry_count; preserves models |
| `reset_retry()` | Sets `retry_count = 0` |
| `set_phase(phase)` | Updates `self.phase` |

`Session` has no persistence. For a VSCode extension, you would create one `Session` per
workspace document or per user conversation context, hold it in memory, and recreate on
extension reload.

---

## 6. Tool Dispatch via Closures

`_build_tools_and_handlers(session, root)` in `agents/orchestrator.py` returns:

```python
(tool_defs: list[dict], handlers: dict[str, Callable[[dict], str]])
```

Each handler is a closure over `session` and `root`. Because they capture the same `session`
object reference, they can:

- Read and write `session.phase`, `session.spec`, `session.last_test`, `session.retry_count`
- Call `session.set_phase(...)`, `session.reset_retry()`
- Do all of this without any global state or explicit parameter threading

This is rebuilt on every call to `run_orchestrator_turn`. The rebuild is cheap (closure
creation only) and ensures the handlers always reference the live session. There is no
caching between turns.

`_build_llm(role, session)` is called inside handlers that spawn sub-agents:

```python
def _build_llm(role: str, session: Session) -> LLMClient:
    model = session.models[role]
    provider = cfg.provider_from_model(model)
    resolved = cfg.resolve_api_key(provider)
    api_key = resolved[0] if resolved else None
    return LLMClient(model=model, api_key=api_key)
```

This constructs a fresh `LLMClient` per sub-agent dispatch, pulling the model string from
`session.models[role]` (which the `/model` command can update live) and resolving the API
key on demand. If the user changes `session.models["spec"]` mid-session, the next
`start_spec` call picks up the new model automatically.

---

## 7. LLM Abstraction (`ai_agent/llm.py`)

```python
class LLMClient:
    def __init__(self, model: str, api_key: Optional[str] = None): ...
    def call(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> Response: ...
```

`model` is always a `"provider/model-name"` string, e.g. `"anthropic/claude-opus-4-7"`.
Internally uses `litellm.completion(model=self.model, ...)`.

**Tool definition conversion.** Tool defs are authored in Anthropic-style throughout the
codebase (`{name, description, input_schema}`). `LLMClient.call` converts them to OpenAI
function-calling format before handing to litellm:

```python
{"type": "function", "function": {"name": ..., "description": ..., "parameters": input_schema}}
```

**Prompt caching (Anthropic models only).** When `self.model.startswith("anthropic/")`:

1. System message is wrapped as a `content` list with `"cache_control": {"type":"ephemeral"}`.
2. The last tool definition gets `"cache_control": {"type":"ephemeral"}`.
3. The most recent user message (scanning backwards through `full_messages`) gets
   `"cache_control": {"type":"ephemeral"}` on its last content block.

Cache control is applied to copies so the caller's data structures are not mutated.
Anthropic's ephemeral cache has a 5-minute TTL. Cache hit stats appear in `response.usage`.

**`Response` dataclass:**

```python
@dataclass
class Response:
    text: Optional[str]           # None when only tool calls are present
    tool_calls: list[ToolCall]    # list of ToolCall(id, name, input)
    stop_reason: str              # "end_turn" | "tool_use" | "max_tokens" | "stop_sequence"
    usage: Usage                  # input_tokens, output_tokens, cache_read, cache_creation
    raw: Any                      # raw litellm response object
```

**Stop reason normalization.** litellm returns OpenAI-style finish reasons
(`"stop"`, `"tool_calls"`, `"length"`). These are mapped to Anthropic-style before being
stored in `Response.stop_reason`:

```python
_FINISH_REASON_MAP = {
    "stop":       "end_turn",
    "tool_calls": "tool_use",
    "length":     "max_tokens",
}
```

---

## 8. Verilator MCP (`verilator_mcp.py`)

A `FastMCP` server named `"verilator"`. Can be run standalone (`python verilator_mcp.py`)
or imported directly — the tester imports it directly at module load time.

**4 tools:**

| Tool | Key parameters | Returns |
|------|----------------|---------|
| `verilator_lint` | `sources: list[str]`, `top: str`, `cwd?`, `timeout=60` | `{ok, stdout, stderr, returncode, ...}` |
| `verilator_build` | `sources: list[str]`, `tb: str`, `top: str`, `trace=True`, `cwd?`, `timeout=180` | `{ok, ..., binary_path}` |
| `verilator_run` | `binary: str`, `cwd?`, `plusargs?`, `timeout=120` | `{ok, stdout, stderr, returncode, ...}` |
| `verilator_build_and_run` | all of the above | `{build: {...}, run: {...}}` |

**Path sandboxing.** Every path argument passes through `safe_path()`:

```python
def safe_path(path: str, must_exist: bool = False) -> Path:
    allowed = Path(os.environ.get("AI_MCP_ALLOWED_ROOT", str(_DEFAULT_ROOT))).resolve()
    ...
```

The env var is re-read on every call, so it takes effect immediately when
`python -m ai_agent` sets `os.environ["AI_MCP_ALLOWED_ROOT"]` before entering the REPL.
Paths outside `allowed` raise `ValueError`. This is the only security boundary between the
LLM-generated file paths and the local filesystem.

`verilator_build` writes output to `{cwd}/obj_dir/V{top}`. The `binary_path` field in the
return dict is set to that path if the file exists after build, or `None` if the build
failed silently. `run_tests` checks for a `None` binary_path and returns a build-phase
failure in that case.

**`waveform_mcp.py`** is a separate FastMCP server with 10+ tools for VCD introspection
(`list_signals`, `find_transitions`, `get_signal_value`, `sample_on_clock`, etc.). It is
imported by `ai_agent/tools.py` and available to the agent as tool definitions, but is not
part of the automated test pipeline (see section 13).

---

## 9. Retry Guard

`session.retry_count` counts test runs since the last success or spec/writer reset.

```
h_start_spec called        →  retry_count = 0
h_dispatch_writers called  →  retry_count = 0
h_run_tests called         →  retry_count += 1
  result.passed == True    →  retry_count = 0, phase = IDLE
  retry_count >= 3         →  phase = IDLE, return {max_retries_reached: true}
  retry_count < 3          →  phase = TEST, return normal failure dict
```

When `max_retries_reached: true` is in the tool result, the orchestrator's system prompt
instructs it to stop calling tools entirely and report to the user. There is no Python-level
enforcement of this; the stop is prompt-driven. The counter resets to zero only when a new
spec run or writer run begins (not on user `/reset` alone — but `/reset` calls
`session.reset()` which zeroes `retry_count`).

---

## 10. Configuration and API Keys (`ai_agent/config.py`)

**Default models:**

```python
DEFAULT_MODELS = {
    "orchestrator": "anthropic/claude-opus-4-7",
    "spec":         "anthropic/claude-opus-4-7",
    "writer":       "anthropic/claude-sonnet-4-6",
}
```

**Key resolution order** (`resolve_api_key(provider, cli_override=None) -> (key, source) | None`):

1. `cli_override` argument (testing only, passed via `--api-key` flag)
2. Environment variable `{PROVIDER}_API_KEY` (e.g. `ANTHROPIC_API_KEY`)
3. OS keyring (`keyring.get_password("ai_mcp", provider)`)
4. `.env` file at repo root (parsed with `python-dotenv`)
5. Returns `None` if nothing found

Supported providers: `anthropic`, `moonshot`, `openai`, `deepseek`, `gemini`, `xai`.

**Key validation** (`validate_key(provider, key) -> (ok, msg)`): makes a real 1-token
litellm call using a known cheap model for that provider (e.g. `claude-haiku-4-5-20251001`
for Anthropic). Used during first-run setup and `/auth login`.

**`first_run_prompt()`**: interactive terminal wizard. Presents a numbered provider list,
reads the key with `getpass` (hidden input), validates it, asks where to store it
(OS keychain or `.env`), and stores it. Called automatically on first launch if no key is
found for the orchestrator's provider.

**REPL slash commands for auth:**

| Command | Effect |
|---------|--------|
| `/auth status` | Lists all providers with stored keys and their storage location |
| `/auth login [provider]` | Runs `first_run_prompt()` interactively |
| `/auth logout <provider>` | Removes key from keychain and `.env` after confirmation prompt |

**Other REPL slash commands:** `/help`, `/exit`, `/quit`, `/reset`, `/phase`,
`/model <role> <provider/model>`.

---

## 11. Design Decisions and Trade-offs

- **Single shared history; phase is a session field.** All agent results land back in the
  orchestrator's message history as tool results. The user always talks to one agent.
  Simpler debugging (one message log), simpler session serialization if you add persistence.
  Cost: the orchestrator context grows with each sub-agent round-trip.

- **Tool defs authored in Anthropic format; converted to OpenAI on the wire.** Source code
  matches Anthropic's documentation. litellm handles the actual provider routing. Adding a
  new provider requires only a new entry in `config.PROVIDERS` and `_VALIDATE_MODEL`.

- **Spec is a dict at runtime, not a dataclass.** Tool call arguments are JSON strings.
  The spec round-trips through `json.dumps` / `json.loads` on every tool call boundary.
  The `Spec` dataclass in `session.py` is a type reference for documentation; it is never
  instantiated by production code paths.

- **Writers use `ThreadPoolExecutor`, not async.** litellm's sync API is simpler and both
  workers are I/O-bound (waiting on remote LLM HTTP calls). No event loop required; the
  `run_orchestrator_turn` function is fully synchronous. A VSCode extension running in a
  worker thread can call it without needing an asyncio bridge.

- **Tester has no LLM.** Verilator is deterministic. LLM involvement would add latency and
  cost without changing the pass/fail signal. All LLM-driven repair happens at the
  orchestrator level, not inside the tester.

- **Caching is opportunistic.** `cache_control: ephemeral` is applied to the system
  message, last user message, and last tool definition on every Anthropic call. There is no
  explicit cache management logic. Cache savings accumulate automatically when prompts
  stabilize between turns (common in SPEC and TEST phases where the system prompt is
  unchanged).

- **`on_event` hook exists but does not stream.** `run_orchestrator_turn` returns only after
  the full turn completes. The `on_event` callback fires synchronously mid-turn for tool
  calls and usage stats, which is sufficient for a sidebar progress display. Chunk-level
  streaming would require switching to `litellm.acompletion` and making
  `run_orchestrator_turn` async.

- **`safe_path` reads the env var dynamically.** The env var is set by `__main__.py` before
  the REPL starts. Because `safe_path` re-reads `os.environ` on every call, a future
  integration (e.g. opening a different workspace) could change the allowed root without
  re-importing the module.

---

## 12. File Map

| File | Lines | Description |
|------|-------|-------------|
| `ai_agent/__init__.py` | 0 | Package marker |
| `ai_agent/__main__.py` | 39 | CLI entry point; parses args, sets `AI_MCP_ALLOWED_ROOT`, calls `cli.run` |
| `ai_agent/cli.py` | 237 | REPL loop (prompt_toolkit), slash command handler, key resolution on startup |
| `ai_agent/session.py` | 79 | `Phase`, `Port`, `Spec`, `TestResult`, `Session` dataclasses |
| `ai_agent/llm.py` | 157 | `LLMClient`, `Response`, `ToolCall`, `Usage`; litellm wrapper with cache control |
| `ai_agent/config.py` | 183 | API key resolution, storage, validation, first-run wizard, default model map |
| `ai_agent/tools.py` | 359 | Full tool definition list (Anthropic schema) + dispatcher for file/shell/Verilator/waveform tools |
| `ai_agent/agent.py` | 101 | Legacy single-agent loop (predates multi-agent refactor; not used by current REPL) |
| `ai_agent/system_prompt.py` | 46 | Legacy system prompt loader (used by `agent.py`; not used by current agents) |
| `ai_agent/agents/__init__.py` | 0 | Package marker |
| `ai_agent/agents/orchestrator.py` | 203 | Phase machine driver; `run_orchestrator_turn`; closure-based tool handlers |
| `ai_agent/agents/spec.py` | 179 | Spec sub-agent; bounded sub-conversation; `run_spec` returns Spec dict |
| `ai_agent/agents/writers.py` | 175 | Parallel RTL + TB writers; `write_both`; `_call_with_retry` nudge loop |
| `ai_agent/agents/tester.py` | 120 | Lint → build → sim pipeline; `run_tests`; no LLM |
| `ai_agent/prompts/orchestrator.md` | 50 | Orchestrator system prompt; phase machine rules; tool usage policy |
| `ai_agent/prompts/spec.md` | 26 | Spec agent system prompt; port rules; finalize_spec discipline |
| `ai_agent/prompts/rtl_writer.md` | 23 | RTL writer system prompt; Verilog-2005 style rules |
| `ai_agent/prompts/tb_writer.md` | 32 | TB writer system prompt; self-checking testbench rules |
| `verilator_mcp.py` | 105 | FastMCP server; 4 Verilator tools; `safe_path` sandboxing |
| `waveform_mcp.py` | 776 | FastMCP server; VCD introspection tools; `lru_cache` on VCD parse |
| `.env.example` | — | Template for API key env vars |
| `requirements.txt` | — | Python dependencies |
| `Makefile` | — | Convenience targets for running the agent and Verilator examples |

---

## 13. What Is Not Here Yet

- **Persistent sessions.** `Session` is in-memory only. Closing the REPL loses all history,
  the spec, and the last test result. There is no serialization path.

- **Waveform-driven feedback in the test loop.** `waveform_mcp.py` is fully implemented and
  imported by `tools.py`, but `tester.run_tests` does not call any waveform tools after a
  simulation run. The `TestResult.waveform_path` field is set to `wave.vcd` if it exists,
  but neither the tester nor the orchestrator currently reads waveform data to generate
  targeted repair prompts.

- **Mocked-LLM unit tests.** There are no `pytest` fixtures that substitute a
  deterministic fake for `LLMClient`. Integration tests require live API keys.

- **`pyproject.toml` / installable package.** There is no `pip install ai_mcp` path. The
  package is run directly from the repo with `python -m ai_agent`.

- **Streaming output.** `run_orchestrator_turn` accumulates the full turn before returning.
  The `on_event` hook surfaces tool calls and usage mid-turn, but not token-by-token
  assistant text. Streaming requires an async rewrite of `LLMClient.call` using
  `litellm.acompletion` with `stream=True`.

- **`agent.py` / `system_prompt.py` cleanup.** These files predate the multi-agent
  architecture and are not called by any current code path. They can be removed without
  breaking the REPL.
