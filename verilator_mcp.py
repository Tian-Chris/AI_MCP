from pathlib import Path
import os
import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("verilator")
_DEFAULT_ROOT = Path(__file__).parent.resolve()
ALLOWED_ROOT = Path(os.environ.get("AI_MCP_ALLOWED_ROOT", str(_DEFAULT_ROOT))).resolve()


def safe_path(path: str, must_exist: bool = False) -> Path:
    allowed = Path(os.environ.get("AI_MCP_ALLOWED_ROOT", str(_DEFAULT_ROOT))).resolve()
    p = Path(path).expanduser().resolve()
    if not (p == allowed or p.is_relative_to(allowed)):
        raise ValueError(f"path must be inside allowed root: {allowed}")
    if must_exist and not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")
    return p


def resolve_under(p: str, base: Path) -> Path:
    pp = Path(p).expanduser()
    if not pp.is_absolute():
        pp = base / pp
    return pp.resolve()


def run_cmd(args: list[str], cwd: Path, timeout: int) -> dict:
    try:
        proc = subprocess.run(args, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "returncode": None, "stdout": (e.stdout or "")[-4000:],
                "stderr": (e.stderr or "")[-4000:], "timed_out": True,
                "cmd": " ".join(args)}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:],
            "timed_out": False, "cmd": " ".join(args)}


@mcp.tool()
def verilator_lint(sources: list[str], top: str, flags_file: str | None = None,
                   cwd: str | None = None, timeout: int = 60) -> dict:
    """Lint Verilog/SystemVerilog sources with verilator --lint-only."""
    work_dir = safe_path(cwd) if cwd else ALLOWED_ROOT
    validated_sources = [str(safe_path(str(resolve_under(s, work_dir)), must_exist=True)) for s in sources]
    validated_flags = str(safe_path(str(resolve_under(flags_file, work_dir)), must_exist=True)) if flags_file else None
    args = ["verilator", "--lint-only"]
    if validated_flags:
        args += ["-f", validated_flags]
    args += validated_sources + ["--top-module", top]
    return run_cmd(args, work_dir, timeout)


@mcp.tool()
def verilator_build(sources: list[str], tb: str, top: str,
                    flags_file: str | None = None, trace: bool = True,
                    cwd: str | None = None, timeout: int = 180) -> dict:
    """Compile Verilog/SystemVerilog sources and a testbench into a binary with verilator --binary."""
    work_dir = safe_path(cwd) if cwd else ALLOWED_ROOT
    validated_sources = [str(safe_path(str(resolve_under(s, work_dir)), must_exist=True)) for s in sources]
    validated_tb = str(safe_path(str(resolve_under(tb, work_dir)), must_exist=True))
    validated_flags = str(safe_path(str(resolve_under(flags_file, work_dir)), must_exist=True)) if flags_file else None
    args = ["verilator", "--binary"]
    if validated_flags:
        args += ["-f", validated_flags]
    if trace:
        args += ["--trace"]
    args += [validated_tb] + validated_sources + ["--top-module", top]
    result = run_cmd(args, work_dir, timeout)
    binary_path = work_dir / "obj_dir" / f"V{top}"
    result["binary_path"] = str(binary_path) if binary_path.exists() else None
    return result


@mcp.tool()
def verilator_run(binary: str, cwd: str | None = None,
                  plusargs: list[str] | None = None, timeout: int = 120) -> dict:
    """Run a compiled Verilator simulation binary with optional plusargs."""
    work_dir = safe_path(cwd) if cwd else ALLOWED_ROOT
    validated_binary = safe_path(str(resolve_under(binary, work_dir)), must_exist=True)
    args = [str(validated_binary)]
    if plusargs:
        args += [f"+{a}" for a in plusargs]
    return run_cmd(args, work_dir, timeout)


@mcp.tool()
def verilator_build_and_run(sources: list[str], tb: str, top: str,
                            flags_file: str | None = None, trace: bool = True,
                            cwd: str | None = None, plusargs: list[str] | None = None,
                            build_timeout: int = 180, run_timeout: int = 120) -> dict:
    """Build and immediately run a Verilator simulation, returning both build and run results."""
    build_result = verilator_build(sources=sources, tb=tb, top=top,
                                   flags_file=flags_file, trace=trace,
                                   cwd=cwd, timeout=build_timeout)
    run_result = None
    if build_result["ok"] and build_result.get("binary_path"):
        run_result = verilator_run(binary=build_result["binary_path"],
                                   cwd=cwd, plusargs=plusargs, timeout=run_timeout)
    return {"build": build_result, "run": run_result}


if __name__ == "__main__":
    mcp.run()
