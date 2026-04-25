# INTEGRATION.md — VSCode Extension Integration Guide

> **Audience:** TypeScript/JS developers building a VSCode side-panel extension. You need to wire the AI_MCP Python orchestrator into your extension's UI. You do not need to be a Python expert, but you do need to be able to run Python and add one file to the package.
>
> **Related docs:** See `README.md` for installation and `ARCHITECTURE.md` for the phase machine internals. This document covers only the integration boundary.

---

## 1. Goal

The AI_MCP Python package runs a multi-agent RTL workflow: it takes a natural-language design goal, produces a hardware spec, fans out parallel Verilog writers, runs lint/build/simulation via Verilator, and retries on failure — all driven by a phase machine inside a persistent session object. The VSCode extension needs to surface this workflow as a side panel: a chat interface where the user types goals, sees phase transitions, approves specs, and views test results. The core challenge is the integration boundary: the extension is TypeScript running in a Node.js environment; the orchestrator is Python. This document describes three options for bridging them, recommends one, and gives you everything you need to implement it.

---

## 2. Three Integration Paths

### Path A: Shell out to the CLI

The extension spawns `python -m ai_agent --root <workspace>` as a child process and pipes stdin/stdout.

**Pros:**
- Zero changes to AI_MCP.
- Simplest to prototype — a few lines of `child_process.spawn`.

**Cons:**
- `cli.py` uses `prompt_toolkit` for the REPL. `prompt_toolkit` probes whether stdin is a TTY; when it isn't, it behaves unpredictably (the smoke tests had to bypass it entirely with a raw stdin shim).
- The CLI prints ANSI escape codes for formatting. You would need to strip these before displaying text.
- No structured event stream — you are parsing free-form text.
- No way to receive tool call events, phase changes, or test results as discrete signals.
- Session is fully tied to the subprocess; no way to reset state without restarting.

**Verdict:** Prototype only. Do not ship with this approach.

---

### Path B (RECOMMENDED): Long-running Python service over stdio

Add a thin `ai_agent/service.py` module (~50 lines). The extension spawns it as a persistent subprocess. Communication is newline-delimited JSON over stdin/stdout: the extension sends request objects; the service emits event objects.

**Pros:**
- Clean, typed event stream — every message is a JSON object with a `type` field.
- Full control over the UI: phase changes, tool events, spec contents, and test results all come through as distinct event types.
- No `prompt_toolkit` dependency in the service path.
- The Python session stays alive for the duration of the workspace window; one subprocess per workspace.
- The wrapper is ~50 lines of new code; zero changes to existing AI_MCP source.

**Cons:**
- You need to add `ai_agent/service.py` (the skeleton is in Section 3a below).
- The subprocess must be managed by the extension (spawn on activation, kill on deactivation).

**Verdict:** This is what we recommend. See Section 3 for full details.

---

### Path C: Expose the orchestrator as MCP tools to Cline/Continue

Wrap `start_spec`, `dispatch_writers`, and `run_tests` as a new MCP server (similar to the existing `verilator_mcp.py`). A host extension like Cline or Continue becomes the chat UI; their LLM calls our tools.

**Pros:**
- Zero UI work — you reuse the host extension's chat panel.
- Reuses the existing MCP server pattern already present in the codebase.

**Cons:**
- The host LLM has to drive the entire phase machine. Our orchestrator's system prompt, phase gating (spec approval before write), and retry logic are no longer enforced — the host model has to replicate or guess at all of it.
- The `Spec` dataclass needs to survive a JSON round-trip across the MCP boundary on every tool call. Path objects and enum values need explicit handling.
- The host model must be capable enough to orchestrate correctly, which in practice means Opus — costs balloon relative to Path B where only the turns that need Opus use it.
- You lose the side panel UI entirely; users interact through the host extension's generic chat.

**Verdict:** Rejected for v1. Revisit if Path B proves operationally heavy or if you want zero UI investment.

---

## 3. Recommended: Path B — Service Mode

### 3a. New file to add: `ai_agent/service.py`

Create this file at `/path/to/AI_MCP/ai_agent/service.py`. It is a thin stdio wrapper around the existing orchestrator. Do not modify any other source file.

The service reads newline-delimited JSON requests from stdin and writes newline-delimited JSON events to stdout. The very first line it reads is the init payload; after that it enters a request loop.

```python
# ai_agent/service.py
import json
import sys
import traceback
from pathlib import Path

from ai_agent.session import Session, Phase
from ai_agent.agents.orchestrator import run_orchestrator_turn, _build_llm


def emit(event: dict) -> None:
    """Write one JSON event to stdout and flush immediately."""
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main() -> None:
    # First line is the init payload — block until it arrives.
    init = json.loads(sys.stdin.readline())
    root = Path(init["root"]).resolve()

    import os
    os.environ["AI_MCP_ALLOWED_ROOT"] = str(root)

    session = Session(
        messages=[],
        phase=Phase.IDLE,
        models=init.get("models", {
            "orchestrator": "anthropic/claude-opus-4-7",
            "spec":         "anthropic/claude-opus-4-7",
            "writer":       "anthropic/claude-sonnet-4-6",
        }),
    )
    orch_llm = _build_llm("orchestrator", session)
    emit({"type": "ready", "phase": session.phase.name})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)

            if req.get("type") == "user_message":
                session.add_user(req["text"])

                def on_event(d: dict) -> None:
                    emit({"type": "tool_event", **d})

                reply = run_orchestrator_turn(
                    session, orch_llm, root, on_event=on_event,
                )
                emit({
                    "type":      "assistant_message",
                    "text":      reply,
                    "phase":     session.phase.name,
                    "spec":      session.spec,
                    "last_test": (session.last_test.__dict__
                                  if session.last_test else None),
                })

            elif req.get("type") == "reset":
                session.reset()
                emit({"type": "reset_ok", "phase": session.phase.name})

            elif req.get("type") == "set_model":
                session.models[req["role"]] = req["model"]
                if req["role"] == "orchestrator":
                    orch_llm = _build_llm("orchestrator", session)
                emit({"type": "model_set"})

            else:
                emit({"type": "error", "message": f"unknown request type: {req!r}"})

        except Exception:
            emit({"type": "error", "message": traceback.format_exc()})


if __name__ == "__main__":
    main()
```

**Notes on the skeleton:**
- `_build_llm` is a module-level function in `ai_agent/agents/orchestrator.py`. It resolves the API key from the environment (or keyring/.env) and constructs an `LLMClient`. The extension injects `ANTHROPIC_API_KEY` (or equivalent) into the subprocess environment before spawn (see Section 4).
- `session.spec` is a `Spec` dataclass or `None`. Dataclasses are not directly JSON-serializable. If you want to emit the spec as a dict, call `dataclasses.asdict(session.spec)` instead of `session.spec` directly. Similarly `session.last_test` is a `TestResult` dataclass — the skeleton uses `.__dict__`; `dataclasses.asdict()` is cleaner if you have Path objects inside.
- The `on_event` callable receives exactly one `dict` argument. The dict always has a `type` key. Current types emitted by the orchestrator: `"tool_call"`, `"tool_result"`, `"usage"`.
- Phase names from the service use `.name` (uppercase enum member names: `IDLE`, `SPEC`, `WRITE`, `TEST`). The Phase enum values are lowercase (`"idle"`, `"spec"`, etc.). Pick one convention and be consistent in the extension.

---

### 3b. TypeScript side — spawning and driving the service

Install no extra packages. Use Node's built-in `child_process` and `readline`.

```typescript
// src/aiMcpService.ts
import * as cp from "child_process";
import * as readline from "readline";
import * as vscode from "vscode";

export type ServiceEvent =
    | { type: "ready";             phase: string }
    | { type: "assistant_message"; text: string; phase: string; spec: Record<string, unknown> | null; last_test: Record<string, unknown> | null }
    | { type: "tool_event";        [key: string]: unknown }
    | { type: "reset_ok";          phase: string }
    | { type: "model_set" }
    | { type: "error";             message: string };

export class AiMcpService {
    private proc: cp.ChildProcess;
    private rl: readline.Interface;
    private handlers = new Map<string, Array<(e: ServiceEvent) => void>>();

    constructor(
        repoPath: string,           // path to the AI_MCP checkout
        workspaceRoot: string,      // the user's project folder
        env: NodeJS.ProcessEnv,     // must include ANTHROPIC_API_KEY (or equivalent)
        models?: Record<string, string>,
    ) {
        this.proc = cp.spawn(
            "python",
            ["-m", "ai_agent.service"],
            {
                cwd: repoPath,
                env,
                stdio: ["pipe", "pipe", "pipe"],
            }
        );

        // Log stderr from the Python process for debugging.
        this.proc.stderr?.on("data", (chunk: Buffer) => {
            console.error("[ai_mcp stderr]", chunk.toString());
        });

        this.proc.on("exit", (code) => {
            console.warn(`[ai_mcp] service exited with code ${code}`);
        });

        this.rl = readline.createInterface({ input: this.proc.stdout! });
        this.rl.on("line", (line) => {
            let evt: ServiceEvent;
            try {
                evt = JSON.parse(line);
            } catch {
                console.error("[ai_mcp] unparseable stdout line:", line);
                return;
            }
            this.dispatch(evt);
        });

        // Send init payload — this is the first thing the service reads.
        this.send({
            root: workspaceRoot,
            ...(models ? { models } : {}),
        });
    }

    /** Send any JSON request to the service. */
    send(req: object): void {
        this.proc.stdin!.write(JSON.stringify(req) + "\n");
    }

    /** Register a listener for a specific event type. Returns a disposable. */
    on(type: ServiceEvent["type"], fn: (e: ServiceEvent) => void): vscode.Disposable {
        if (!this.handlers.has(type)) {
            this.handlers.set(type, []);
        }
        this.handlers.get(type)!.push(fn);
        return new vscode.Disposable(() => {
            const arr = this.handlers.get(type) ?? [];
            const idx = arr.indexOf(fn);
            if (idx !== -1) arr.splice(idx, 1);
        });
    }

    sendUserMessage(text: string): void {
        this.send({ type: "user_message", text });
    }

    reset(): void {
        this.send({ type: "reset" });
    }

    setModel(role: "orchestrator" | "spec" | "writer", model: string): void {
        this.send({ type: "set_model", role, model });
    }

    dispose(): void {
        this.rl.close();
        this.proc.stdin?.end();
        this.proc.kill();
    }

    private dispatch(evt: ServiceEvent): void {
        const arr = this.handlers.get(evt.type) ?? [];
        for (const fn of arr) {
            try { fn(evt); } catch (e) { console.error("[ai_mcp] handler error", e); }
        }
    }
}
```

**Usage from your WebviewView provider:**

```typescript
// src/extension.ts (activate)
import * as vscode from "vscode";
import { AiMcpService } from "./aiMcpService";

let service: AiMcpService | undefined;

export async function activate(context: vscode.ExtensionContext) {
    const secrets = context.secrets;
    const repoPath  = vscode.workspace.getConfiguration("aiMcp").get<string>("repoPath")!;
    const wsRoot    = vscode.workspace.workspaceFolders?.[0].uri.fsPath ?? "/tmp";

    const key = await resolveApiKey(secrets);
    const env = { ...process.env, ANTHROPIC_API_KEY: key };

    service = new AiMcpService(repoPath, wsRoot, env);

    service.on("ready", () => {
        // Enable the send button in the webview
        panel.webview.postMessage({ command: "ready" });
    });

    service.on("assistant_message", (e) => {
        if (e.type !== "assistant_message") return;
        panel.webview.postMessage({ command: "message", text: e.text, phase: e.phase });
        if (e.spec) {
            panel.webview.postMessage({ command: "showSpec", spec: e.spec });
        }
        if (e.last_test && !(e.last_test as any).passed) {
            panel.webview.postMessage({ command: "showTestFailure", result: e.last_test });
        }
    });

    service.on("tool_event", (e) => {
        if (e.type !== "tool_event") return;
        // Update status bar: "writing RTL...", "linting...", etc.
        const label = (e as any).name ?? "working";
        vscode.window.setStatusBarMessage(`ai_mcp: ${label}`, 3000);
    });

    service.on("error", (e) => {
        if (e.type !== "error") return;
        vscode.window.showErrorMessage(`AI MCP error: ${e.message}`);
    });

    context.subscriptions.push(new vscode.Disposable(() => service?.dispose()));
}
```

The webview posts messages back to the extension host (the normal VSCode webview messaging pattern). When the user types a message:

```typescript
// In your WebviewViewProvider.resolveWebviewView:
panel.webview.onDidReceiveMessage((msg) => {
    if (msg.command === "sendMessage") {
        service?.sendUserMessage(msg.text);
    } else if (msg.command === "approveSpec") {
        service?.sendUserMessage("yes proceed");
    } else if (msg.command === "reset") {
        service?.reset();
    }
});
```

---

### 3c. Event and request contract

Every message in both directions is a single line of JSON terminated by `\n`. Never send partial JSON or multi-line JSON objects.

#### Requests (extension → service)

| When | `type` | Required fields | Optional fields |
|---|---|---|---|
| First line (init) | *(no type field)* | `root: string` | `models: {orchestrator, spec, writer}` |
| User sends a message | `"user_message"` | `text: string` | — |
| User resets session | `"reset"` | — | — |
| User changes model | `"set_model"` | `role: "orchestrator" \| "spec" \| "writer"`, `model: string` | — |

The init payload has no `type` field — it is identified by being the first line.

#### Events (service → extension)

| `type` | Payload fields | When emitted |
|---|---|---|
| `"ready"` | `phase: "IDLE"` | After init, before entering the request loop |
| `"assistant_message"` | `text: string`, `phase: string`, `spec: dict \| null`, `last_test: dict \| null` | After the orchestrator produces a final reply text |
| `"tool_event"` | `type: "tool_event"`, plus any keys from the inner event dict (always has an inner `type` key) | For each tool call start, tool result, and token usage update during a turn |
| `"reset_ok"` | `phase: "IDLE"` | After a successful reset |
| `"model_set"` | — | After the model for a role is updated |
| `"error"` | `message: string` (Python traceback or error string) | On any unhandled exception |

**`tool_event` inner types** (the `type` key inside the forwarded dict):

| Inner `type` | Additional fields | Meaning |
|---|---|---|
| `"tool_call"` | `name: string`, `input: dict` | Orchestrator is about to execute a tool |
| `"tool_result"` | `name: string`, `ok: boolean` | Tool execution finished |
| `"usage"` | `input: int`, `output: int`, `cache_read: int` | Token usage for the preceding LLM call |

**Phase values** returned in `phase` fields: `"IDLE"`, `"SPEC"`, `"WRITE"`, `"TEST"` (uppercase, matching Python enum member names).

**`last_test` shape** (when non-null):

```
{
    phase: "lint" | "build" | "sim" | "waveform" | "pass",
    passed: boolean,
    stdout: string,
    stderr: string,
    waveform_path: string | null
}
```

**`spec` shape** (when non-null):

```
{
    module_name: string,
    ports: Array<{ name: string, direction: "input"|"output"|"inout", width: number }>,
    behavior: string,
    clock: string,
    reset: string | null,
    rtl_path: string,
    tb_path: string,
    test_strategy: string
}
```

---

## 4. API Key Handling in VSCode

The Python service resolves API keys in this order: environment variable → OS keychain → `.env` file. The cleanest approach from the extension side is to inject the key as an environment variable on the spawned subprocess.

**Option 1 — Lazy (user manages keys externally):**

```typescript
const env = {
    ...process.env,
    // process.env already contains ANTHROPIC_API_KEY if the user set it in their shell
};
const svc = new AiMcpService(repoPath, wsRoot, env);
```

This works if the user sets `ANTHROPIC_API_KEY` in their shell before launching VSCode. Simple, but bad UX — users forget, and VSCode doesn't always inherit shell env on macOS.

**Option 2 — Recommended: VSCode SecretStorage:**

```typescript
async function resolveApiKey(secrets: vscode.SecretStorage): Promise<string> {
    let key = await secrets.get("anthropic_api_key");
    if (!key) {
        const entered = await vscode.window.showInputBox({
            prompt: "Enter your Anthropic API key",
            password: true,
            ignoreFocusOut: true,
            placeHolder: "sk-ant-...",
        });
        if (!entered) {
            throw new Error("No API key provided. Extension cannot start.");
        }
        await secrets.store("anthropic_api_key", entered);
        key = entered;
    }
    return key;
}

// In activate():
const key = await resolveApiKey(context.secrets);
const env = { ...process.env, ANTHROPIC_API_KEY: key };
const svc = new AiMcpService(repoPath, wsRoot, env);
```

`SecretStorage` persists across VSCode sessions on the user's machine, encrypted by the OS keychain. The key survives restarts without the user re-entering it.

**Multi-provider support:** The Python config module maps provider names to env var names: `ANTHROPIC_API_KEY`, `MOONSHOT_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`. If you want to support provider switching, store each key separately in `SecretStorage` under its provider name and inject all of them into the subprocess env.

**The Python side requires no changes.** `config.resolve_api_key()` already checks `os.environ` first, so injecting into the subprocess env is sufficient.

---

## 5. UI Mapping Suggestions

Map service events to panel components:

| Event | UI element | Notes |
|---|---|---|
| `ready` | Enable chat input; show "IDLE" phase badge | Disable input while service is starting |
| `assistant_message.phase` | Side panel header badge: `IDLE / SPEC / WRITE / TEST` | Color-code: gray/blue/yellow/green |
| `assistant_message.text` | Chat bubble (assistant side) | Render as markdown |
| `assistant_message.spec` | Spec card (collapsible): module name, port table, behavior summary | Show only when phase transitions out of SPEC; include an "Approve" button |
| "Approve" button click | Send `user_message: "yes proceed"` | The orchestrator's system prompt gates `dispatch_writers` on user approval — do not auto-approve |
| `assistant_message.last_test` when `passed: false` | Diagnostics panel with collapsible stderr | Red badge on side panel icon; "Max retries reached" label if retry_count hit 3 |
| `assistant_message.last_test` when `passed: true` | Green badge; "Tests passed" chip | Optionally show stdout tail |
| `tool_event` with inner type `"tool_call"` | Status bar pulse: `ai_mcp: writing RTL...` | Use `name` field for the label |
| `tool_event` with inner type `"usage"` | Running cost estimate (optional) | Default models are Opus; ~$0.10 per 4-bit counter end-to-end |
| `error` | `vscode.window.showErrorMessage` | Also log the full traceback to an output channel |

**Spec approval flow specifically:** When the orchestrator finishes the SPEC phase it returns an `assistant_message` with `phase: "SPEC"` (or the transition back toward `"IDLE"` is blocked until the user approves). The `spec` field of the event contains the full spec dict. Render it as a card. The user either clicks "Approve" (which sends `"yes proceed"`) or clicks "Reject / Revise" (which sends a message with their feedback). Never auto-send the approval — the system prompt is designed around explicit human gating.

---

## 6. Streaming Considerations

Currently `run_orchestrator_turn` is a blocking call that returns the final reply text only after the full LLM response is received. The `on_event` hook fires at tool call boundaries (before and after each tool execution) but does not produce token-by-token output.

This is enough for status bar updates (you know when a tool starts and ends) but not for streaming the assistant's reply text word-by-word into the chat bubble.

**If you want token streaming** (strongly recommended for chat UX — responses from Opus can take 10–30 seconds):

1. Add `stream=True` to the `litellm.completion()` call in `ai_agent/llm.py`.
2. Accumulate the streamed chunks and emit them as `tool_event` events with a new inner type (e.g., `"text_delta"` with a `delta: string` field).
3. On the extension side, append deltas to the current assistant bubble as they arrive.

This requires changes to `llm.py` and `orchestrator.py`. It is left as a follow-up (there is a TODO in `llm.py` for this). Flag it as a known gap in the v1 integration.

---

## 7. Known Limitations and Gotchas

**No persistent session across subprocess restarts.**
If the user closes the side panel or VSCode exits, the Python process is killed and the `Session` object is gone. On re-open, the session starts from `Phase.IDLE` with an empty message history. Persistence (serializing `session.messages` to disk and reloading) is a v2 feature. For v1, inform the user that closing the panel resets their session.

**`AI_MCP_ALLOWED_ROOT` is process-level.**
The file tools (`read_file`, `list_dir`) enforce that all file access stays under `AI_MCP_ALLOWED_ROOT`. This env var is set once per process at init time. If the user has multiple workspace folders open in the same VSCode window, you must spawn a separate Python subprocess for each root — do not attempt to share one service across roots.

**Verilator must be on PATH.**
The tester calls `verilator` directly via subprocess. If it is not on the user's PATH, `run_tests` will fail with a cryptic error. On startup, run `which verilator` (or `where verilator` on Windows) and show a notification if it is missing: "Verilator not found. Install it and ensure it is on your PATH before running tests."

```typescript
import { execSync } from "child_process";
function checkVerilator(): boolean {
    try {
        execSync("which verilator", { stdio: "ignore" });
        return true;
    } catch {
        return false;
    }
}
```

**Spec round-trips as JSON.**
The `spec` dict that comes back in `assistant_message` is JSON-serializable, but Path objects inside it (like `rtl_path`, `tb_path`) are serialized as strings. Keep them as strings on the TypeScript side. Do not try to pass a TypeScript `Uri` object back through the service — always send string paths.

**Do not auto-approve the spec.**
The orchestrator's system prompt (in `ai_agent/prompts/orchestrator.md`) explicitly states that `dispatch_writers` must not be called until the user approves the spec. If the extension automatically sends "yes proceed", it bypasses the human review step that is the whole point of the SPEC phase. Always require a user action (button click or typed message) to approve.

**Retry cap is 3.**
`h_run_tests` in the orchestrator increments `session.retry_count` on each test failure and checks `>= 3`. When the cap is hit, the tool result includes `"max_retries_reached": true` and the orchestrator escalates to the user. The service emits this as a normal `assistant_message`. Surface it clearly: red badge, "Max retries reached — the agent needs your help" label, and show the last `last_test.stderr` in a collapsible block.

**Cache control is Anthropic-specific.**
`llm.py` adds `cache_control: {type: "ephemeral"}` to the system prompt and last user message for Anthropic models only. The Anthropic prompt cache TTL is 5 minutes. If the user pauses for longer than 5 minutes mid-flow, the next call is a full cache miss. This is a cost issue, not a correctness issue — no action needed from the extension.

**Model string format.**
Model strings are `"provider/model-name"`, e.g., `"anthropic/claude-opus-4-7"`, `"anthropic/claude-sonnet-4-6"`. When letting users switch models via `set_model`, validate the format before sending. Unknown providers will cause a key resolution failure in the Python service (emitted as an `error` event).

**stderr from the subprocess is not structured.**
The Python service writes tool call summaries and token counts to `sys.stderr` (inherited from `cli.py`'s `_print_event`). In the service mode, `on_event` redirects these to stdout as JSON. However, uncaught Python warnings and import-time messages still go to stderr. Route the subprocess stderr to a VSCode output channel (not to the user directly).

---

## 8. Testing the Integration Before Wiring Up the UI

Before writing any TypeScript, verify that the service responds correctly from the command line.

```bash
# Step 1: Make sure you are in the AI_MCP repo with the venv active.
cd /path/to/AI_MCP
source .venv/bin/activate

# Step 2: Set your API key.
export ANTHROPIC_API_KEY="sk-ant-..."

# Step 3: Create the service.py file (see Section 3a) then run it.
python -m ai_agent.service
```

The process starts and blocks waiting for input. Paste the init line (one line, then press Enter):

```
{"root": "/tmp/p3"}
```

You should immediately see:

```json
{"type": "ready", "phase": "IDLE"}
```

Now send a user message:

```
{"type": "user_message", "text": "design a 4-bit counter"}
```

Watch for a stream of `tool_event` lines followed by:

```json
{"type": "assistant_message", "text": "...", "phase": "SPEC", "spec": {...}, "last_test": null}
```

Test a reset:

```
{"type": "reset"}
```

Expected response:

```json
{"type": "reset_ok", "phase": "IDLE"}
```

Test an error case (malformed request):

```
{"type": "unknown_thing"}
```

Expected:

```json
{"type": "error", "message": "unknown request type: {'type': 'unknown_thing'}"}
```

**Automating the smoke test:**

```bash
# Feed lines via a here-doc through Python's stdin, capture stdout, check for "ready"
python -m ai_agent.service <<'EOF' | head -1 | python -c "import sys,json; d=json.load(sys.stdin); assert d['type']=='ready', d"
{"root": "/tmp/p3"}
EOF
echo "Service startup: OK"
```

---

## 9. FAQ

**Q: Why not just use Cline or Continue as the UI?**
A: They do not have our phase machine, spec gating, or retry logic. Path C above is possible, but the host LLM would have to replicate all of that from scratch — and it would cost more because the host needs to be a capable model for every interaction, not just when we actually need Opus. We considered it and rejected it for v1.

**Q: Can we port the orchestrator to TypeScript?**
A: In principle, yes. `litellm` has partial TypeScript equivalents (e.g., `litellm-node`) and the tool dispatch logic is mostly JSON manipulation. But the orchestrator, spec agent, writer agents, and tester together are roughly 1,000 lines of carefully-validated Python, plus prompt files. Re-validating them in TypeScript is a significant project. Recommendation: keep Python, expose via the service.

**Q: How do we update the orchestrator's behavior?**
A: Edit `ai_agent/prompts/orchestrator.md`. Restart the service subprocess (the prompt is loaded at turn time from disk, but the subprocess must be restarted for the new version to take effect since `_load_system_prompt()` is called each turn — actually it re-reads each call, so a restart is not strictly required mid-session, but it is safer to restart). No Python code change needed.

**Q: Can we add extension-side tools — for example, "open file in editor"?**
A: Yes. The cleanest approach: add a new tool definition to `ORCHESTRATOR_TOOL_DEFS` in `ai_agent/agents/orchestrator.py` and a corresponding handler in `_build_tools_and_handlers`. The handler emits a `tool_event` via `on_event` with a custom inner type (e.g., `"open_file"`). The extension listens for that inner type in `tool_event` events and calls `vscode.workspace.openTextDocument` / `vscode.window.showTextDocument`. This keeps extension-side actions in TypeScript while keeping the tool call in Python.

**Q: Does waveform analysis run by default?**
A: Not yet. `waveform_mcp.py` exists and exports tools for querying `.vcd` files, but it is not wired into the tester agent. The `TestResult` dataclass has a `waveform_path` field for future use. Wire it in as a `waveform` phase in the tester if you want post-simulation signal inspection.

**Q: What model strings are valid?**
A: Model strings follow the pattern `"provider/model-name"`. Supported providers: `anthropic`, `moonshot`, `openai`, `deepseek`, `gemini`, `xai`. Default models: `orchestrator` and `spec` both use `"anthropic/claude-opus-4-7"`; `writer` uses `"anthropic/claude-sonnet-4-6"`. The underlying routing goes through `litellm`, so any model litellm supports under those providers should work — but only the defaults are tested.

**Q: What happens if the user closes the panel and reopens it?**
A: The old subprocess is killed (via `dispose()`). On re-open, a new subprocess is spawned and the session starts fresh from `Phase.IDLE`. The user's previous conversation history is lost. This is a known limitation — see Section 7.

---

## 10. Roadmap (handoff checklist)

- [ ] Add `ai_agent/service.py` using the skeleton in Section 3a
- [ ] Wire VSCode `SecretStorage` for API key management (Section 4)
- [ ] Build side panel `WebviewViewProvider`: chat bubbles + phase badge + spec card + diagnostics panel
- [ ] Spawn and manage the service subprocess per workspace folder (one process per `WorkspaceFolder`)
- [ ] Handle subprocess exit / crash — show error and offer a "Restart service" button
- [ ] Check for Verilator on PATH at activation time; show install instructions if missing
- [ ] Add token streaming (requires changes to `llm.py` + `orchestrator.py`) — flag as v1.1
- [ ] Surface running cost estimate from `tool_event` usage events
- [ ] Add waveform inspection in TEST phase using `waveform_mcp.py` tools
- [ ] Persistent session storage: serialize `session.messages` to disk on subprocess exit, restore on next spawn — flag as v2
- [ ] Multi-provider key management in `SecretStorage` if supporting model switching beyond Anthropic
