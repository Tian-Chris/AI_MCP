"""REPL entry point for the AI_MCP agent — prompt_toolkit-based interactive shell."""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich import box

from ai_agent.agents.orchestrator import run_orchestrator_turn
from ai_agent._display import show_waveform, console as _console
from .session import Session, Phase
from .llm import LLMClient
from . import config

# ---------------------------------------------------------------------------
# Verbose mode — set AI_MCP_VERBOSE=1/true/yes to enable diagnostic prints
# ---------------------------------------------------------------------------

_VERBOSE = os.environ.get("AI_MCP_VERBOSE", "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Event printer (stderr so it doesn't pollute assistant text on stdout)
# ---------------------------------------------------------------------------

def _print_event(event: dict) -> None:
    if not _VERBOSE:
        return
    t = event.get("type")
    if t == "tool_call":
        raw = json.dumps(event.get("input", {}))
        trimmed = raw[:80] + ("..." if len(raw) > 80 else "")
        print(f"  -> {event['name']}({trimmed})", file=sys.stderr)
    elif t == "usage":
        print(
            f"  [tokens: in={event.get('input',0)} "
            f"out={event.get('output',0)} "
            f"cache={event.get('cache_read',0)}]",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Key resolution helpers
# ---------------------------------------------------------------------------

def _resolve_key_for_provider(provider: str, cli_override: str | None) -> tuple[str, str]:
    """Return (key, source), prompting interactively if needed."""
    result = config.resolve_api_key(provider, cli_override=cli_override)
    if result is not None:
        return result
    # No key for this provider — check if any providers are stored
    stored = config.list_stored_providers()
    if not stored:
        # Full first-run flow
        found_provider, key = config.first_run_prompt()
        if found_provider != provider:
            # User may have picked a different provider; try to resolve again
            result2 = config.resolve_api_key(provider)
            if result2:
                return result2
            # Return what first_run gave us even if provider differs
            return (key, "interactive")
        result3 = config.resolve_api_key(provider)
        if result3:
            return result3
        return (key, "interactive")
    else:
        # Other providers exist but not this one — focused prompt
        print(f"No API key found for provider '{provider}'.")
        print(f"Stored providers: {', '.join(s['provider'] for s in stored)}")
        print(f"Please add a key for '{provider}'.")
        _, key = config.first_run_prompt()
        result4 = config.resolve_api_key(provider)
        if result4:
            return result4
        return (key, "interactive")


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

HELP_TEXT = """\
Available commands:
  /help                     Show this help
  /exit  /quit              Exit the REPL
  /reset                    Clear session history and return to idle phase
  /phase                    Show current phase
  /model <role> <p/model>   Update model for role (orchestrator|spec|writer)
  /auth status              List all stored provider keys
  /auth login [provider]    Add/replace a key (full wizard if no provider given)
  /auth logout <provider>   Remove stored key for provider (prompts confirmation)
  /wave [path]              Show the last (or given) .vcd waveform in gtkwave
"""


def handle_slash(
    text: str,
    session: Session,
    llm_container: list,   # mutable box: [LLMClient]; index 0 is current llm
    args: argparse.Namespace,
) -> bool:
    """Handle a slash command. Returns True to keep looping, False to exit."""
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/exit", "/quit"):
        print("bye")
        return False

    elif cmd == "/help":
        print(HELP_TEXT, end="")

    elif cmd == "/reset":
        session.reset()
        print("session cleared")

    elif cmd == "/phase":
        print(f"current phase: {session.phase.value}")

    elif cmd == "/model":
        if len(parts) < 3:
            print("usage: /model <role> <provider/model>", file=sys.stderr)
            return True
        role, new_model = parts[1], parts[2]
        valid_roles = {"orchestrator", "spec", "writer"}
        if role not in valid_roles:
            print(f"unknown role '{role}'; valid: {', '.join(sorted(valid_roles))}", file=sys.stderr)
            return True
        old_model = session.models.get(role, "(none)")
        session.models[role] = new_model
        print(f"/model {role}: {old_model} -> {new_model}")
        if role == "orchestrator":
            new_provider = config.provider_from_model(new_model)
            try:
                key, src = _resolve_key_for_provider(new_provider, cli_override=None)
            except Exception as e:
                print(f"Failed to resolve key for {new_provider}: {e}", file=sys.stderr)
                return True
            llm_container[0] = LLMClient(model=new_model, api_key=key)
            print(f"  LLM rebuilt for {new_provider} (key from {src})")

    elif cmd == "/auth":
        if len(parts) < 2:
            print("usage: /auth <status|login|logout>", file=sys.stderr)
            return True
        sub = parts[1].lower()
        if sub == "status":
            stored = config.list_stored_providers()
            if not stored:
                print("No API keys stored.")
            else:
                print(f"{'provider':<16} source")
                print("-" * 28)
                for entry in stored:
                    print(f"{entry['provider']:<16} {entry['source']}")
        elif sub == "login":
            if len(parts) >= 3:
                # focused: just run the wizard and let user store
                print(f"Adding key for provider '{parts[2]}' — launching setup wizard...")
            config.first_run_prompt()
        elif sub == "logout":
            if len(parts) < 3:
                print("usage: /auth logout <provider>", file=sys.stderr)
                return True
            provider = parts[2]
            answer = input(f"Remove key(s) for '{provider}'? [y/N] ").strip().lower()
            if answer == "y":
                cleared = config.remove_api_key(provider)
                if cleared:
                    print(f"Cleared from: {', '.join(cleared)}")
                else:
                    print(f"No stored keys found for '{provider}'.")
            else:
                print("Aborted.")
        else:
            print(f"unknown /auth sub-command '{sub}'; use status|login|logout", file=sys.stderr)

    elif cmd == "/wave":
        target: Path | None = None
        if len(parts) > 1:
            target = Path(parts[1]).expanduser().resolve()
        elif session.last_test is not None and getattr(session.last_test, "waveform_path", None) is not None:
            target = session.last_test.waveform_path
        if target is None:
            _console.print("[yellow]No waveform available. Run a sim first or pass a path: /wave path/to.vcd[/yellow]")
        else:
            show_waveform(target)

    else:
        print(f"unknown command '{cmd}', /help for list", file=sys.stderr)

    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _short_model(model: str) -> str:
    """Strip 'anthropic/claude-' prefix for compact display."""
    prefix = "anthropic/claude-"
    if model.startswith(prefix):
        return model[len(prefix):]
    return model


def run(args: argparse.Namespace) -> None:
    """Main REPL entry point. args: orchestrator_model, spec_model, writer_model,
    root (Path), api_key (str|None), provider (str|None)."""

    # Resolve orchestrator key
    orchestrator_model: str = args.orchestrator_model
    provider = config.provider_from_model(orchestrator_model)
    try:
        key, src = _resolve_key_for_provider(provider, cli_override=getattr(args, "api_key", None))
    except Exception as e:
        print(f"Key resolution failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Build LLM and session — use a mutable container so /model can swap the client
    llm = LLMClient(model=orchestrator_model, api_key=key)
    llm_container: list = [llm]

    session = Session(
        models={
            "orchestrator": orchestrator_model,
            "spec": args.spec_model,
            "writer": args.writer_model,
        }
    )

    root: Path = getattr(args, "root", Path.cwd())

    # Build cwd line — collapse $HOME to ~
    cwd = Path.cwd()
    try:
        cwd_str = "~/" + str(cwd.relative_to(Path.home()))
    except ValueError:
        cwd_str = str(cwd)

    info = Text()
    info.append(f"orchestrator : {_short_model(session.models['orchestrator'])}\n")
    info.append(f"spec         : {_short_model(session.models['spec'])}\n")
    info.append(f"writer       : {_short_model(session.models['writer'])}\n")
    info.append(f"cwd          : {cwd_str}\n\n")
    info.append("type /help for commands, /exit to quit")

    panel = Panel(
        info,
        title="[bold]AI_MCP — RTL agent[/bold]",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    _console.print(panel)

    # Prompt-toolkit session for readline-like history
    ps: PromptSession = PromptSession(history=InMemoryHistory())

    while True:
        orch_short = _short_model(session.models.get("orchestrator", orchestrator_model))
        try:
            text = ps.prompt(f"[{session.phase.value} | {orch_short}]> ")
        except EOFError:
            print("\nbye")
            return
        except KeyboardInterrupt:
            print("\n^C  (interrupted — type /exit to quit)")
            continue

        text = text.strip()
        if not text:
            continue

        if text.startswith("/"):
            keep = handle_slash(text, session, llm_container, args)
            if not keep:
                return
            continue

        session.add_user(text)
        try:
            reply = run_orchestrator_turn(session, llm_container[0], root, on_event=_print_event)
            if reply:
                _console.print(Markdown(reply))
        except KeyboardInterrupt:
            print("\n^C  (interrupted — type /exit to quit)")
        except Exception as e:
            if _VERBOSE:
                traceback.print_exc(file=sys.stderr)
            else:
                print(f"error: {e}", file=sys.stderr)
