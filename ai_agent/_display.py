"""Shared display helpers: console singleton, file-diff rendering, gtkwave waveform display."""
from __future__ import annotations

import difflib
import shutil
import subprocess
from pathlib import Path

from rich.columns import Columns
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel

console = Console()


def _verilog_lexer(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".v", ".sv", ".vh", ".svh"):
        return "verilog"
    if suffix in (".py",):
        return "python"
    return "text"


def show_file_write(path: Path, old_content: str | None, new_content: str) -> None:
    """Print a diff for a file write to the shared console.

    old_content=None means the file did not exist before (new file).
    """
    rel = path
    try:
        rel = path.relative_to(Path.cwd())
    except ValueError:
        pass

    if old_content is None:
        header = f"[bold green]Created[/bold green] {rel}"
        body = Syntax(new_content, _verilog_lexer(path), line_numbers=True, word_wrap=False)
        console.print(Panel(body, title=header, border_style="green", padding=(0, 1)))
        return

    if old_content == new_content:
        console.print(f"[dim]No change to {rel}[/dim]")
        return

    diff_lines = list(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(rel),
            tofile=str(rel),
            n=3,
        )
    )
    diff_text = "".join(diff_lines).rstrip()
    header = f"[bold yellow]Modified[/bold yellow] {rel}"
    body = Syntax(diff_text, "diff", line_numbers=False, word_wrap=False)
    console.print(Panel(body, title=header, border_style="yellow", padding=(0, 1)))


def show_test_dashboard(tests: list) -> None:
    """Render per-test status as a row of small panels.

    Each test gets a small panel:
      - VERIFIED: green border, green text
      - FAILED:   red border, red text + reason
      - PENDING:  dim border, dim text
    """
    if not tests:
        return

    panels = []
    for t in tests:
        status = getattr(t, "status", "pending")
        name = getattr(t, "name", "?")
        reason = getattr(t, "reason", "")
        if status == "verified":
            body = "[bold green]VERIFIED[/bold green]"
            border = "green"
        elif status == "failed":
            body = "[bold red]FAILED[/bold red]"
            if reason:
                body += f"\n[red]{reason}[/red]"
            border = "red"
        else:
            body = "[dim]PENDING[/dim]"
            border = "grey50"
        panels.append(Panel(body, title=name, border_style=border, padding=(0, 1), expand=False))

    total = len(tests)
    verified = sum(1 for t in tests if getattr(t, "status", "") == "verified")
    failed = sum(1 for t in tests if getattr(t, "status", "") == "failed")
    pending = total - verified - failed

    summary = f"[bold]Tests[/bold]  [green]{verified} verified[/green]  [red]{failed} failed[/red]  [dim]{pending} pending[/dim]   ({verified}/{total})"
    console.print(summary)
    console.print(Columns(panels, equal=False, expand=False, padding=(0, 1)))


def show_waveform(vcd_path: Path, *, wires: str | None = None, length: int | None = None,
                  start: str | None = None, end: str | None = None, zoom: int = 12) -> bool:
    """Display a VCD file in a native waveform viewer. Returns True if launched.

    Tries Surfer first (modern, native macOS), then gtkwave. Optional kwargs are
    accepted for API stability but ignored — both viewers are interactive, so
    signal/range selection happens in the GUI.

    On macOS we launch via `open -a <App>` to bypass any broken CLI shims (e.g.
    Homebrew gtkwave's Perl wrapper that needs Switch.pm). On Linux we exec the
    binary directly, detached.
    """
    import sys

    if not vcd_path.exists():
        console.print(f"[red]Waveform not found:[/red] {vcd_path}")
        return False

    is_macos = sys.platform == "darwin"

    surfer_app = Path("/Applications/Surfer.app") if is_macos else None
    surfer_bin = shutil.which("surfer")
    gtkwave_app = Path("/Applications/gtkwave.app") if is_macos else None
    gtkwave_bin = shutil.which("gtkwave")

    surfer_available = (is_macos and surfer_app is not None and surfer_app.exists()) or surfer_bin is not None
    gtkwave_available = (is_macos and gtkwave_app is not None and gtkwave_app.exists()) or gtkwave_bin is not None

    if surfer_available:
        viewer = "Surfer"
        if is_macos and surfer_app is not None and surfer_app.exists():
            cmd = ["open", "-a", "Surfer", str(vcd_path)]
            launch_mode = "macos_open"
        else:
            cmd = [surfer_bin, str(vcd_path)]
            launch_mode = "detached"
    elif gtkwave_available:
        viewer = "gtkwave"
        if is_macos and gtkwave_app is not None and gtkwave_app.exists():
            cmd = ["open", "-a", "gtkwave", str(vcd_path)]
            launch_mode = "macos_open"
        else:
            cmd = [gtkwave_bin, str(vcd_path)]
            launch_mode = "detached"
    else:
        console.print("[yellow]No waveform viewer installed.[/yellow] Install one of:")
        console.print("  Surfer (recommended on macOS): [cyan]https://surfer-project.org[/cyan]")
        console.print("  GTKWave official DMG:          [cyan]https://gtkwave.sourceforge.net[/cyan]")
        return False

    console.print(f"[cyan]wave[/cyan] {vcd_path.name} → {viewer}")
    try:
        if launch_mode == "macos_open":
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                console.print(f"[red]{viewer} launcher exited with code {result.returncode}[/red]")
                if err:
                    console.print(f"[red]{err}[/red]")
                return False
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except subprocess.TimeoutExpired:
        return True
    except OSError as e:
        console.print(f"[red]Failed to launch {viewer}:[/red] {e}")
        return False
    return True
