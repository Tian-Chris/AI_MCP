# AI_MCP Project Goals

Architectural plan for the AI_MCP RTL coding agent. Source-of-truth design doc; update when decisions change.

## Path decision

We are pursuing **two tracks** with different priority:

- **Track 1 (ACTIVE, MVP): VSCode extension** — leverage existing chat extensions (Cline / Continue) that already speak MCP. Register our Verilator + Waveform MCP servers via `.mcp.json` or extension settings. UI, auth, and key storage are handled by the host extension or VSCode SecretStorage. Fastest path to a usable tool.

- **Track 2 (DEFERRED): Custom agent framework** — Python REPL with `prompt_toolkit`, multi-provider key management via `keyring`, slash commands, `pip install` distribution. **Can be skipped while we build the VSCode MVP.** Already partially built (Phase 1 complete) — kept working for CLI users / debugging, but not the priority surface. Revivable later if we want a CLI-first product or hit the ceiling of what host extensions allow.

The **multi-agent workflow** (orchestrator → spec → writers → test) is **shared** between both tracks — it's the core IP and gets built either way. Phase 2 and Phase 3 below produce the same Python orchestrator code regardless of UI.

---

## Three core capabilities

### 1. API key / auth management

**Track 1 (VSCode MVP)**: VSCode SecretStorage handles key storage. If using Cline / Continue as host, the host extension owns its own auth UI. Nothing to build on our side beyond docs.

**Track 2 (Custom framework — DEFERRED)**: Multi-provider key management for any LLM provider (Anthropic, Moonshot/Kimi, OpenAI, DeepSeek, Gemini, xAI, etc.). Tiered resolution: CLI flag > env var > OS keychain (`keyring`) > `.env` > first-run interactive prompt. Provider routing via `litellm`. REPL slash commands: `/auth status`, `/auth login [provider]`, `/auth logout [provider]`. **Status: implemented in Phase 1.** Kept working for CLI users but not the priority surface.

### 2. Hierarchical multi-agent workflow (SHARED — both tracks)

Phase-based agent hierarchy. Same logic regardless of UI.

```
IDLE  ──user states goal──▶  SPEC
SPEC  ──spec returns─────▶  WRITE  (orchestrator confirms with user first)
WRITE ──writers finish───▶  TEST   (auto)
TEST  ──pass──▶ IDLE
TEST  ──fail──▶ orchestrator decides: re-write / re-spec / ask user
```

**Agents**:
- **Orchestrator** (default Opus 4.7): top-level chat with user, holds session state, drives phase transitions
- **Spec agent** (default Opus 4.7): sub-conversation that builds a structured `Spec`. Dispatches read-agents to gather context, asks user clarifying questions
- **Writers** (configurable, default Sonnet 4.6): two parallel writers — RTL + testbench. Same `Spec` input, write to assigned paths
- **Tester** (no LLM): function pipeline `lint → build → sim → waveform checks`. Returns structured `TestResult`

**Configurable models**: any litellm-supported provider via CLI flags (Track 2) or VSCode settings (Track 1).

**Failure routing**: sim failures escalate to the orchestrator, which decides next move based on diagnostics.

**Tool access (per-agent, for clarity + safety)**:
- Orchestrator: `enter_spec_phase`, `enter_write_phase`, `run_tests`, read-only `read_file` / `list_dir`
- Spec: `read_file`, `list_dir`, `bash` (read-only commands), `ask_user`
- Writers: `read_file`, `write_file` (sandboxed to assigned path), `list_dir`
- Tester: direct calls to Verilator + Waveform MCP tools — no LLM

### 3. User-agent conversation (UI)

**Track 1 (VSCode MVP)**: Chat extension's webview / panel. Users type into Cline or Continue's input box. Multi-turn history managed by the host. No new UI code on our side.

**Track 2 (Custom framework — DEFERRED)**: Python REPL using `prompt_toolkit` for line editing, history, multi-line input. Slash commands: `/exit`, `/reset`, `/phase`, `/help`, `/model <role> <provider/model>`, `/auth *`. In-memory `Session` holds chat + phase. **Status: implemented in Phase 1.**

---

## Track 1: VSCode extension MVP (ACTIVE)

### MVP shape (lowest effort)
- Use **Cline** or **Continue** as the chat host
- Register `verilator_mcp` + `waveform_mcp` via `.mcp.json` or extension settings
- Custom system prompt that drives the phase-based RTL workflow
- Auth via host extension's settings UI (user pastes Anthropic / Moonshot / etc. key there)

### Deeper integration (if MVP hits ceiling)
- Custom TypeScript extension wrapping our multi-agent orchestrator
- Either port the orchestrator to TS, OR run the Python backend as a JSON-RPC subprocess
- VSCode SecretStorage for key management
- Custom webview for spec editing, waveform display, etc.

### Open questions for MVP
- Cline vs Continue — which has better MCP support + system prompt control?
- Can a single system prompt drive the phase machine, or do we need stateful tool returns to enforce phase transitions?
- Does the extension need to call into our Python orchestrator (Phase 2-3 work), or can MVP ship with just MCP tools + a smart system prompt?

---

## Track 2: Custom agent framework (DEFERRED)

Goals retained from original plan; not actively prioritized while VSCode MVP ships. Already partially built; can be revived later.

### Status
- **Phase 1: COMPLETE** — REPL boots, slash commands work, multi-provider keys work, single-shot CLI preserved
- Phase 2-3: shared with Track 1 (gets built regardless)
- Phase 4-B (polish): deferred indefinitely

### Files already on disk (Phase 1 output)
- `ai_agent/__main__.py` — entry point (`python -m ai_agent`)
- `ai_agent/cli.py` — REPL with `prompt_toolkit`, slash commands
- `ai_agent/config.py` — multi-provider key resolution
- `ai_agent/llm.py` — `litellm` wrapper
- `ai_agent/session.py` — `Session`, `Phase`, `Spec`, `TestResult`
- `ai_agent/agent.py` — refactored to per-turn callable

### Deferred polish (skip while MVP ships)
- `pyproject.toml` for `pip install -e .` → `ai-agent` console script
- README rewrite for the REPL flow
- Mocked-LLM smoke tests for the agent pipeline

---

## Target file layout (Python core, used by both tracks)

```
ai_agent/
  __main__.py           # python -m ai_agent  (Track 2 only)
  cli.py                # REPL                 (Track 2 only)
  config.py             # multi-provider keys  (Track 2 only)
  session.py            # Session, Phase, Spec, TestResult   ← shared
  llm.py                # litellm wrapper                     ← shared
  agents/
    orchestrator.py     # phase decisions                     ← shared
    spec.py             # spec sub-conversation               ← shared
    writers.py          # parallel RTL + TB writers           ← shared
    tester.py           # lint → build → sim → waveform       ← shared
  prompts/
    orchestrator.md
    spec.md
    writer_rtl.md
    writer_tb.md
  tools.py              # 12 tools, used by all agents        ← shared

verilator_mcp/          # MCP server — Track 1 MVP uses directly
waveform_mcp/           # MCP server — Track 1 MVP uses directly
```

The MCP servers (`verilator_mcp/`, `waveform_mcp/`) are usable directly by any MCP-aware client (Cline, Continue, Claude Code). They ARE the Track 1 MVP — no wrapper needed.

## Handoff data structures (shared)

```python
@dataclass
class Spec:
    module_name: str
    ports: list[Port]          # name, dir, width
    behavior: str              # natural-language description
    clock: str
    reset: str | None
    rtl_path: Path
    tb_path: Path
    test_strategy: str         # what TB should verify

@dataclass
class TestResult:
    phase: Literal["lint", "build", "sim", "waveform", "pass"]
    passed: bool
    stdout: str
    stderr: str
    waveform_path: Path | None
```

## Build phases

### Phase 1 — REPL + multi-provider key UX (Track 2) ✅ DONE
- `cli.py` REPL with `prompt_toolkit`
- `config.py` multi-provider key resolution + first-run prompt
- `llm.py` `litellm` wrapper
- `keyring` + `.env` integration
- Slash commands wired
- `agent.py` refactored to per-turn callable
- Single-shot `python -m ai_agent.agent ...` preserved

### Phase 2 — Multi-agent skeleton (SHARED) ✅ COMPLETE
- Split orchestrator out of existing agent code
- Add `Session` + `Phase` state machine wiring (Phase enum already in session.py)
- Stub `spec.py`, `writers.py`, `tester.py` (echo versions that wire end-to-end)
- E2E smoke test: user msg → orchestrator → stubbed sub-agents → response

### Phase 3 — Multi-agent real logic (SHARED) ✅ COMPLETE
- Real `run_spec`: Opus, bounded sub-conversation, `finalize_spec` tool
- Real `write_both`: parallel Sonnet RTL + TB writers via `ThreadPoolExecutor`
- Retry guard: capped at 3 attempts
- litellm prompt caching working (`cache_read` >2700 tokens on subsequent turns)
- `AI_MCP_ALLOWED_ROOT` env var making Verilator paths configurable
- E2E green: 4-bit counter passes lint + build + sim first try

### Phase 4-A — VSCode integration (Track 1) ← CURRENT PRIORITY
- Documentation prepared for partner handoff: `README.md`, `ARCHITECTURE.md`, `INTEGRATION.md`
- Path A vs B vs C tradeoff analysis complete — see `INTEGRATION.md`; recommendation: Path B (Python service-mode subprocess + JSON event protocol)
- Pick host: Cline vs Continue (decision required)
- Configure MCP server registration
- Author system prompt that drives phase-based RTL flow
- Internal demo: design a 4-bit counter end-to-end through the extension

### Phase 4-B — Custom framework polish (Track 2, DEFERRED)
- README rewrite, smoke tests, `pyproject.toml`

## Decisions locked

| Decision | Choice |
|---|---|
| MVP UI | VSCode extension (Cline / Continue host) |
| Custom REPL | Built (Phase 1 done), DEFERRED as primary surface |
| API key storage (Track 1) | VSCode SecretStorage / host extension settings |
| API key storage (Track 2) | Multi-provider, tiered (CLI > env > keychain > .env > prompt) |
| LLM router (Track 2) | `litellm` |
| Persistent session | In-memory only (v1 for both tracks) |
| Writer model | Configurable, default Sonnet 4.6 |
| Test execution | Orchestrator auto-runs after writers finish |
| Failure routing | Sim failures escalate to orchestrator |
| Verilator allowed-root | Configurable via `AI_MCP_ALLOWED_ROOT` env var (autoset by `python -m ai_agent` based on `--root`) |

## Open questions (current)

- **Track 1 MVP host**: Cline vs Continue — needs side-by-side comparison of MCP support, system prompt control, multi-step tool-call handling
- **Phase machine in MVP**: can we drive it from a system prompt alone, or does Track 1 need to call into the Track 2 orchestrator code (running it as a subprocess or HTTP service)?
- **Auth in Track 1**: when using a host extension, does the user re-paste their key per-extension, or does the host expose its key to MCP servers? (probably no — MCP servers usually get their own env via `.mcp.json`)
- **Path A vs B vs C for VSCode integration** — see `INTEGRATION.md` for tradeoff analysis. Recommendation: Path B (Python service-mode subprocess + JSON event protocol).
