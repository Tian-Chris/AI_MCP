# AI_MCP

AI_MCP is a multi-agent RTL coding assistant. It drives Verilog projects from a goal in plain
English to verified, simulated code via a phase machine of cooperating LLM agents.

You describe what you want ("design a 4-bit counter with synchronous reset"), and the system
handles spec elaboration, RTL writing, Verilator linting, C++ testbench compilation, and
simulation — iterating automatically on failures until the design passes or hits a retry ceiling.

---

## Why this exists

RTL development is rule-bound, repetitive, and benefits enormously from agentic loops with
verification feedback. Manually shelling out to Verilator, fixing lint errors, re-running
simulations, and inspecting waveforms is mechanical work that a well-structured LLM pipeline
can absorb. This project gives the user (or a downstream VSCode extension) a self-contained
Python orchestrator that handles spec → write → lint → simulate without ever leaving the REPL.

---

## Architecture at a glance

```
User
 │
 └─► REPL (cli.py)
       │
       └─► Orchestrator agent  [anthropic/claude-opus-4-7]
             │
             ├─► Spec agent     [anthropic/claude-opus-4-7]
             │     └─ elaborates goal → Spec dataclass (ports, params, description)
             │
             ├─► Writers (parallel ThreadPool)  [anthropic/claude-sonnet-4-6]
             │     ├─ RTL writer  → <module>.v
             │     └─ TB writer   → tb_<module>.v
             │
             └─► Tester agent   [no LLM — pure tool calls]
                   ├─ verilator_lint
                   ├─ verilator_build
                   └─ verilator_run  →  TestResult(passed, stdout, stderr)

Phase machine:
  IDLE ──start_spec──► SPEC ──(approved)──► WRITE ──run_tests──► TEST
   ▲                                                               │
   └───────────────────────────────────────────────────── passed ─┘
                                          ◄── failed, <3 retries: ask user
```

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full data-flow diagram and module breakdown.

---

## Quick install

1. **Clone and enter the repo**

   ```bash
   git clone <repo-url> AI_MCP
   cd AI_MCP
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv .venv && source .venv/bin/activate
   ```

3. **Install Python dependencies**

   ```bash
   pip install -r requirements.txt
   ```

   > Note: there is no `pyproject.toml` yet — direct `pip install -r requirements.txt` is the
   > canonical path. A proper package build is planned.

4. **Install Verilator**

   ```bash
   # macOS
   brew install verilator

   # Debian/Ubuntu
   sudo apt install verilator
   ```

5. **Set up an API key** — see the [API Keys](#api-keys) section below.

---

## API keys

Six LLM providers are supported: `anthropic`, `moonshot` (Kimi), `openai`, `deepseek`,
`gemini`, `xai`.

**Resolution order (first match wins):**

1. CLI flag: `--api-key` / `--provider`
2. Environment variable (e.g. `ANTHROPIC_API_KEY`)
3. OS keychain via `keyring`
4. `.env` file in the repo root
5. First-run interactive prompt (stores result in the OS keychain)

**Quickest path — export before running:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Recommended path — let the wizard store it once:**

```bash
python -m ai_agent   # follow the first-run prompt; key goes into your OS keychain
```

**Slash commands inside the REPL:**

```
/auth status                  # show which providers have stored keys
/auth login [provider]        # add or replace a key for a provider
/auth logout <provider>       # remove a stored key
```

---

## Quickstart REPL

```bash
python -m ai_agent --root .
```

`--root` is the sandbox boundary for all file and Verilator operations. The entry point
automatically exports `AI_MCP_ALLOWED_ROOT` to that path so the Verilator MCP server honours
the same boundary.

Example session:

```
ai_mcp> design a 4-bit counter with synchronous reset

[SPEC] Elaborating...
  module: counter
  ports:  clk, rst, count[3:0]
  params: WIDTH=4

Approve spec and write code? [y/n] y

[WRITE] Generating RTL and testbench (parallel)...
  wrote: counter.v
  wrote: tb_counter.v

[TEST] Running lint + build + sim...
  lint:   OK
  build:  OK
  sim:    OK

PASS — returning to IDLE.

ai_mcp> /phase
Current phase: IDLE

ai_mcp> /exit
```

---

## Configuration

```
python -m ai_agent [--root PATH]
                   [--orchestrator-model PROVIDER/MODEL]
                   [--spec-model PROVIDER/MODEL]
                   [--writer-model PROVIDER/MODEL]
                   [--api-key STR]
                   [--provider STR]
```

| Flag | Default | Description |
|---|---|---|
| `--root` | cwd | Sandbox root for file and Verilator operations |
| `--orchestrator-model` | `anthropic/claude-opus-4-7` | Model for the orchestrator agent |
| `--spec-model` | `anthropic/claude-opus-4-7` | Model for spec elaboration |
| `--writer-model` | `anthropic/claude-sonnet-4-6` | Model for RTL and TB writers |
| `--api-key` | — | API key (overrides all other resolution methods) |
| `--provider` | — | Provider name (paired with `--api-key`) |

**Default models:**

| Role | Model |
|---|---|
| orchestrator | `anthropic/claude-opus-4-7` |
| spec | `anthropic/claude-opus-4-7` |
| writer | `anthropic/claude-sonnet-4-6` |

Any [litellm](https://docs.litellm.ai/docs/providers)-supported model string works, e.g.
`moonshot/kimi-k2-0905-preview`, `openai/gpt-4o`, `deepseek/deepseek-chat`.

**Additional REPL slash commands:**

```
/help                   list all commands
/reset                  clear session and return to IDLE
/phase                  show current phase
/model <role> <p/m>     set provider or model for a role at runtime
/exit  /quit            exit the REPL
```

---

## Project layout

```
AI_MCP/
├── ai_agent/
│   ├── __main__.py          # entry point; sets AI_MCP_ALLOWED_ROOT
│   ├── cli.py               # prompt_toolkit REPL + slash commands
│   ├── llm.py               # LLMClient (litellm, Anthropic prompt caching)
│   ├── config.py            # multi-provider key resolution
│   ├── session.py           # Phase enum, Spec/Port/TestResult/Session dataclasses
│   ├── tools.py             # 12 tool definitions + dispatcher
│   ├── agents/
│   │   ├── orchestrator.py
│   │   ├── spec.py
│   │   ├── writers.py       # parallel ThreadPool for RTL + TB
│   │   └── tester.py        # lint + build + sim; no LLM
│   └── prompts/             # system prompt markdown files per agent
├── verilator_mcp.py         # standalone Verilator MCP server (4 tools)
├── waveform_mcp.py          # standalone waveform/VCD MCP server (11 tools)
├── demo/                    # 4-bit counter RTL + testbench; produces wave.vcd
├── requirements.txt
├── .env.example
└── .mcp.json                # MCP server registration for compatible clients
```

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full file map and data-flow details.

---

## Status

| Phase | Description | State |
|---|---|---|
| Phase 1 | REPL + multi-provider key management | done |
| Phase 2 | Multi-agent skeleton (phase machine, dataclasses) | done |
| Phase 3 | Real LLM agents + retry guard + caching | done — E2E green for 4-bit counter |
| Phase 4-A | VSCode extension integration | in progress (see INTEGRATION.md) |

---

## Further reading

- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design, data flow, module reference
- [INTEGRATION.md](./INTEGRATION.md) — VSCode extension integration guide (start here for Phase 4-A)
- [goal.md](./goal.md) — full project plan and phase breakdown

---

## Origin

This project was built incrementally: the Verilator demo and Makefile came first; the two MCP
servers (`verilator_mcp.py` and `waveform_mcp.py`) were added next to wrap Verilator and VCD
parsing behind a clean tool interface; the multi-agent `ai_agent/` package was built last,
evolving from a single-agent loop into the current phase-machine architecture. The demo in
`demo/` (4-bit counter + testbench) remains the canonical end-to-end smoke test.
