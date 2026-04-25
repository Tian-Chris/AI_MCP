from pathlib import Path
from typing import Literal
import re
import fnmatch
from functools import lru_cache
from vcdvcd import VCDVCD
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("waveform")
ALLOWED_ROOT = Path(__file__).parent.resolve()
MAX_RESULTS_CAP = 500

_TIME_RE = re.compile(r"^(\d+)\s*(fs|ps|ns|us|ms)?$")
_UNIT_TO_PS = {"fs": 1e-3, "ps": 1.0, "ns": 1e3, "us": 1e6, "ms": 1e9}


def safe_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not (p == ALLOWED_ROOT or p.is_relative_to(ALLOWED_ROOT)):
        raise ValueError(f"path must be inside {ALLOWED_ROOT}")
    if p.suffix != ".vcd":
        raise ValueError("expected .vcd waveform file")
    if not p.exists():
        raise ValueError(f"file not found: {p}")
    return p


@lru_cache(maxsize=8)
def _load(path: str) -> VCDVCD:
    return VCDVCD(path, store_tvs=True)


def _native_unit(vcd: VCDVCD) -> str:
    return str(vcd.timescale.get("unit", "ps"))


def parse_time(t: str | int, timescale_unit: str = "ps") -> int:
    if isinstance(t, int):
        return t
    m = _TIME_RE.match(t.strip())
    if not m:
        raise ValueError(f"cannot parse time: {t!r}")
    val = int(m.group(1))
    unit = m.group(2) or timescale_unit
    native_ps = _UNIT_TO_PS.get(timescale_unit, 1.0)
    given_ps = _UNIT_TO_PS.get(unit, 1.0)
    return int(val * given_ps / native_ps)


def _parse_raw(raw: str) -> tuple[str, bool]:
    """Return (clean_bits, has_xz). Strip leading 'b' if present."""
    s = raw.lstrip("b")
    has_xz = any(c in "xzXZ" for c in s)
    return s, has_xz


def _bits_to_int(bits: str) -> int:
    return int(bits, 2)


def format_value(raw: str, width: int, fmt: str = "hex") -> str:
    bits, has_xz = _parse_raw(raw)
    if has_xz or fmt == "bin":
        return bits
    val = _bits_to_int(bits)
    if fmt == "dec":
        return str(val)
    if fmt == "signed":
        if val >= (1 << (width - 1)):
            val -= 1 << width
        return str(val)
    return hex(val)


def _value_at(tv: list, time: int) -> str | None:
    """Return the raw value at or before `time`."""
    result = None
    for t, v in tv:
        if t > time:
            break
        result = v
    return result


def _sig_size(sig) -> int:
    """Return signal width as int (vcdvcd stores it as str)."""
    return int(sig.size)


def _signal_lookup(vcd: VCDVCD, name: str):
    """Return Signal object; raise ValueError if not found."""
    try:
        return vcd[name]
    except Exception:
        avail = vcd.signals[:10]
        raise ValueError(f"signal {name!r} not found. Available: {avail!r}. Use list_signals.")


# ── Tier 1 tool helpers ────────────────────────────────────────────────────────

def _list_signals(path: str, scope: str | None = None, pattern: str | None = None) -> dict:
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    signals = []
    seen = set()
    for name in vcd.signals:
        if name in seen:
            continue
        seen.add(name)
        if scope and not name.startswith(scope + "."):
            continue
        if pattern and not fnmatch.fnmatch(name, pattern):
            continue
        sig = vcd[name]
        signals.append({"name": name, "width": _sig_size(sig)})
    return {
        "path": str(p),
        "timescale": f"1{unit}",
        "endtime": vcd.endtime,
        "signals": signals,
    }


def _get_signal_value(path: str, signal: str, time: str, fmt: str = "hex") -> dict:
    p = safe_path(path)
    vcd = _load(str(p))
    sig = _signal_lookup(vcd, signal)
    t = parse_time(time, _native_unit(vcd))
    raw = _value_at(sig.tv, t)
    if raw is None:
        raw = "x"
    width = _sig_size(sig)
    value = format_value(raw, width, fmt)
    return {"signal": signal, "time": t, "value": value, "raw": raw, "width": width}


def _get_signals_at(path: str, signals: list[str], time: str, fmt: str = "hex") -> dict:
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    t = parse_time(time, unit)
    values = []
    missing = []
    for name in signals:
        try:
            sig = _signal_lookup(vcd, name)
        except ValueError:
            missing.append(name)
            continue
        raw = _value_at(sig.tv, t)
        if raw is None:
            raw = "x"
        w = _sig_size(sig)
        values.append({"signal": name, "value": format_value(raw, w, fmt), "raw": raw, "width": w})
    return {"time": t, "values": values, "missing": missing}


def _find_transitions(
    path: str,
    signal: str,
    edge: Literal["rising", "falling", "any"] = "rising",
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int = 50,
) -> dict:
    p = safe_path(path)
    vcd = _load(str(p))
    sig = _signal_lookup(vcd, signal)
    unit = _native_unit(vcd)
    if _sig_size(sig) != 1 and edge in ("rising", "falling"):
        raise ValueError(
            "'rising'/'falling' edges only meaningful for 1-bit signals; "
            "use edge='any' for multi-bit transitions"
        )
    t_start = parse_time(start_time, unit) if start_time else 0
    t_end = parse_time(end_time, unit) if end_time else vcd.endtime
    cap = min(max_results, MAX_RESULTS_CAP)
    times = []
    truncated = False
    tv = sig.tv
    prev_v = None
    for t, v in tv:
        if t < t_start:
            prev_v = v
            continue
        if t > t_end:
            break
        if prev_v is not None:
            pb, _ = _parse_raw(prev_v)
            cb, _ = _parse_raw(v)
            match = False
            if edge == "any":
                match = pb != cb
            elif _sig_size(sig) == 1:
                if edge == "rising":
                    match = pb == "0" and cb == "1"
                elif edge == "falling":
                    match = pb == "1" and cb == "0"
            if match:
                if len(times) >= cap:
                    truncated = True
                    break
                times.append(t)
        prev_v = v
    return {"signal": signal, "edge": edge, "times": times, "truncated": truncated}


def _resolve_sigs(vcd: VCDVCD, expr: str) -> dict:
    """Return {name: Signal} for all VCD signal names found in expr (longest first)."""
    known = list(vcd.signals)
    sigs = {}
    for name in sorted(known, key=len, reverse=True):
        if name in expr:
            try:
                sigs[name] = _signal_lookup(vcd, name)
            except ValueError:
                pass
    if not sigs:
        raise ValueError("no recognized signals found in expression")
    return sigs


def _merged_times(sigs: dict, t_start: int, t_end: int) -> list[int]:
    """Return sorted deduplicated time grid across all signals within [t_start, t_end]."""
    all_times: set[int] = set()
    for sig in sigs.values():
        for t, _ in sig.tv:
            if t_start <= t <= t_end:
                all_times.add(t)
    return sorted(all_times)


def _eval_expr_at(sigs: dict, expr: str, t: int) -> tuple[bool | None, dict]:
    """Evaluate boolean expr at time t. Returns (result_or_None_if_xz, {sig: int_val})."""
    ns = {}
    for name, sig in sigs.items():
        raw = _value_at(sig.tv, t)
        if raw is None:
            return None, {}
        bits, has_xz = _parse_raw(raw)
        if has_xz:
            return None, {}
        ns[name] = _bits_to_int(bits)

    eval_expr = expr
    for name in sorted(ns.keys(), key=len, reverse=True):
        eval_expr = eval_expr.replace(name, str(ns[name]))
    eval_expr = re.sub(r"&&", " and ", eval_expr)
    eval_expr = re.sub(r"\|\|", " or ", eval_expr)
    eval_expr = re.sub(r"(?<!=)!(?!=)", " not ", eval_expr)

    if not re.match(r"^[\w\s\.\(\)\&\|\^\~\!\=\<\>\+\-\*\/\%]+$", eval_expr):
        raise ValueError(f"unsafe expression after substitution: {eval_expr!r}")

    try:
        result = eval(eval_expr, {"__builtins__": {}})
    except Exception:
        return None, {}
    return bool(result), ns


def _find_condition(
    path: str,
    expr: str,
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int = 50,
) -> dict:
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    t_start = parse_time(start_time, unit) if start_time else 0
    t_end = parse_time(end_time, unit) if end_time else vcd.endtime
    cap = min(max_results, MAX_RESULTS_CAP)

    sigs = _resolve_sigs(vcd, expr)
    sorted_times = _merged_times(sigs, t_start, t_end)

    times = []
    truncated = False

    for t in sorted_times:
        result, _ = _eval_expr_at(sigs, expr, t)
        if result is None:
            continue
        if result:
            if len(times) >= cap:
                truncated = True
                break
            times.append(t)

    return {"expr": expr, "times": times, "truncated": truncated}


# ── Tier 2 tool helpers ────────────────────────────────────────────────────────

def _get_clock_edges(vcd: VCDVCD, path_str: str, clock: str, edge: str, t_start: int, t_end: int) -> list[int]:
    """Return sorted list of clock edge times within [t_start, t_end]."""
    transitions = _find_transitions(path_str, clock, edge, max_results=MAX_RESULTS_CAP)
    return [t for t in transitions["times"] if t_start <= t <= t_end]


def _check_invariant(
    path: str,
    expr: str,
    window: tuple[str, str] | None = None,
) -> dict:
    """Check that a boolean expression holds at every time step in the merged grid."""
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    t_start = parse_time(window[0], unit) if window else 0
    t_end = parse_time(window[1], unit) if window else vcd.endtime

    sigs = _resolve_sigs(vcd, expr)
    sorted_times = _merged_times(sigs, t_start, t_end)

    checked = 0
    skipped_unknown = 0
    first_violation = None

    for t in sorted_times:
        result, ns = _eval_expr_at(sigs, expr, t)
        if result is None:
            skipped_unknown += 1
            continue
        checked += 1
        if not result:
            first_violation = {
                "time": t,
                "values": {name: ns[name] for name in ns},
            }
            break

    return {
        "expr": expr,
        "ok": first_violation is None,
        "checked_points": checked,
        "skipped_unknown": skipped_unknown,
        "first_violation": first_violation,
    }


def _check_sequence(
    path: str,
    antecedent: str,
    consequent: str,
    within_cycles: int,
    clock: str,
    edge: str = "rising",
    window: tuple[str, str] | None = None,
) -> dict:
    """SVA-style antecedent |-> ##[0:within_cycles] consequent check on clock edges."""
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    t_start = parse_time(window[0], unit) if window else 0
    t_end = parse_time(window[1], unit) if window else vcd.endtime

    ant_sigs = _resolve_sigs(vcd, antecedent)
    con_sigs = _resolve_sigs(vcd, consequent)
    clock_times = _get_clock_edges(vcd, str(p), clock, edge, t_start, t_end)

    checked = 0
    passed = 0
    failures = []
    truncated = False

    for idx, t in enumerate(clock_times):
        ant_result, _ = _eval_expr_at(ant_sigs, antecedent, t)
        if ant_result is None or not ant_result:
            continue
        checked += 1
        # Check consequent in [t, clock_times[idx + within_cycles]] if available
        end_idx = idx + within_cycles
        window_times = clock_times[idx: end_idx + 1]
        satisfied = False
        for wt in window_times:
            con_result, _ = _eval_expr_at(con_sigs, consequent, wt)
            if con_result:
                satisfied = True
                break
        if satisfied:
            passed += 1
        else:
            if len(failures) < MAX_RESULTS_CAP:
                t1 = window_times[0] if window_times else t
                t2 = window_times[-1] if window_times else t
                failures.append({
                    "trigger_time": t,
                    "trigger_cycle": idx,
                    "checked_window": [t1, t2],
                })
            else:
                truncated = True

    return {
        "ok": len(failures) == 0,
        "checked": checked,
        "passed": passed,
        "failures": failures,
        "truncated": truncated,
    }


def _check_response(
    path: str,
    trigger: str,
    response: str,
    min_cycles: int,
    max_cycles: int,
    clock: str,
    edge: str = "rising",
    window: tuple[str, str] | None = None,
) -> dict:
    """Check every rising edge of trigger is followed by rising edge of response within [min_cycles, max_cycles]."""
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    t_start = parse_time(window[0], unit) if window else 0
    t_end = parse_time(window[1], unit) if window else vcd.endtime

    trig_sigs = _resolve_sigs(vcd, trigger)
    resp_sigs = _resolve_sigs(vcd, response)
    clock_times = _get_clock_edges(vcd, str(p), clock, edge, t_start, t_end)

    # Evaluate trigger and response at each clock edge
    trig_vals: list[bool | None] = []
    resp_vals: list[bool | None] = []
    for t in clock_times:
        tv, _ = _eval_expr_at(trig_sigs, trigger, t)
        rv, _ = _eval_expr_at(resp_sigs, response, t)
        trig_vals.append(bool(tv) if tv is not None else None)
        resp_vals.append(bool(rv) if rv is not None else None)

    events = 0
    responded = 0
    failures = []

    for idx in range(1, len(clock_times)):
        prev_tv = trig_vals[idx - 1]
        curr_tv = trig_vals[idx]
        # Rising edge of trigger expression
        if prev_tv is not None and curr_tv is not None and not prev_tv and curr_tv:
            events += 1
            trigger_time = clock_times[idx]
            trigger_cycle = idx
            deadline_cycle = idx + max_cycles
            # Find if response rises within [min_cycles, max_cycles] after this trigger
            found_cycle = None
            for ci in range(idx + min_cycles, min(idx + max_cycles + 1, len(clock_times))):
                prev_rv = resp_vals[ci - 1] if ci > 0 else None
                curr_rv = resp_vals[ci]
                if prev_rv is not None and curr_rv is not None and not prev_rv and curr_rv:
                    found_cycle = ci
                    break
            if found_cycle is not None:
                responded += 1
            else:
                failures.append({
                    "trigger_time": trigger_time,
                    "trigger_cycle": trigger_cycle,
                    "deadline_cycle": deadline_cycle,
                    "actual_response_cycle": found_cycle,
                })

    return {
        "ok": len(failures) == 0,
        "events": events,
        "responded": responded,
        "failures": failures,
    }


def _check_stable(
    path: str,
    signal: str,
    while_expr: str,
    clock: str,
    edge: str = "rising",
    window: tuple[str, str] | None = None,
) -> dict:
    """Check that signal is stable (constant) across every contiguous run where while_expr is true."""
    p = safe_path(path)
    vcd = _load(str(p))
    unit = _native_unit(vcd)
    t_start = parse_time(window[0], unit) if window else 0
    t_end = parse_time(window[1], unit) if window else vcd.endtime

    sig = _signal_lookup(vcd, signal)
    cond_sigs = _resolve_sigs(vcd, while_expr)
    clock_times = _get_clock_edges(vcd, str(p), clock, edge, t_start, t_end)

    runs_checked = 0
    violations = []
    in_run = False
    run_start_time = None
    expected_raw = None

    for t in clock_times:
        cond_result, _ = _eval_expr_at(cond_sigs, while_expr, t)
        cond_true = bool(cond_result) if cond_result is not None else False

        if cond_true and not in_run:
            # Start of a new run
            in_run = True
            run_start_time = t
            expected_raw = _value_at(sig.tv, t)
            runs_checked += 1
        elif cond_true and in_run:
            # Continuing run — check signal matches expected
            current_raw = _value_at(sig.tv, t)
            if current_raw != expected_raw:
                violations.append({
                    "start_time": run_start_time,
                    "violation_time": t,
                    "expected": expected_raw,
                    "got": current_raw,
                })
        elif not cond_true and in_run:
            # End of run
            in_run = False
            run_start_time = None
            expected_raw = None

    return {
        "ok": len(violations) == 0,
        "runs_checked": runs_checked,
        "violations": violations,
    }


def _diff_waveforms(
    path: str,
    other_vcd: str,
    signals: list[str] | None = None,
    window: tuple[str, str] | None = None,
    max_results: int = 50,
) -> dict:
    """Compare two VCDs and report per-signal divergences."""
    p = safe_path(path)
    p2 = safe_path(other_vcd)
    vcd1 = _load(str(p))
    vcd2 = _load(str(p2))
    unit1 = _native_unit(vcd1)
    unit2 = _native_unit(vcd2)

    t_start = parse_time(window[0], unit1) if window else 0
    t_end = parse_time(window[1], unit1) if window else vcd1.endtime

    set1 = set(vcd1.signals)
    set2 = set(vcd2.signals)

    if signals is None:
        compared = sorted(set1 & set2)
    else:
        compared = signals

    missing_in_self = [s for s in compared if s not in set1]
    missing_in_other = [s for s in compared if s not in set2]
    active = [s for s in compared if s in set1 and s in set2]

    # Timescale normalization factor (convert vcd2 times to vcd1 units)
    ps1 = _UNIT_TO_PS.get(unit1, 1.0)
    ps2 = _UNIT_TO_PS.get(unit2, 1.0)
    scale = ps2 / ps1  # multiply vcd2 times by this to get vcd1-unit times

    cap = min(max_results, MAX_RESULTS_CAP)
    divergences = []
    first_divergence = None
    truncated = False

    for sig_name in active:
        sig1 = _signal_lookup(vcd1, sig_name)
        sig2 = _signal_lookup(vcd2, sig_name)

        # Collect all times in window for this signal (in vcd1 units)
        all_times: set[int] = set()
        for t, _ in sig1.tv:
            if t_start <= t <= t_end:
                all_times.add(t)
        for t, _ in sig2.tv:
            t_norm = int(t * scale)
            if t_start <= t_norm <= t_end:
                all_times.add(t_norm)

        for t in sorted(all_times):
            v1 = _value_at(sig1.tv, t)
            # For vcd2, convert t back to vcd2 units
            t2 = int(t / scale) if scale != 0 else t
            v2 = _value_at(sig2.tv, t2)
            if v1 != v2:
                entry = {"time": t, "signal": sig_name, "self": v1, "other": v2}
                if first_divergence is None:
                    first_divergence = entry
                if len(divergences) < cap:
                    divergences.append(entry)
                else:
                    truncated = True
                break  # only first divergence per signal

    return {
        "path": str(p),
        "other": str(p2),
        "compared_signals": active,
        "first_divergence": first_divergence,
        "divergences": divergences,
        "missing_in_self": missing_in_self,
        "missing_in_other": missing_in_other,
        "truncated": truncated,
    }


def _sample_on_clock(
    path: str,
    signals: list[str],
    clock: str,
    from_cycle: int,
    to_cycle: int,
    edge: Literal["rising", "falling"] = "rising",
    fmt: str = "hex",
) -> dict:
    p = safe_path(path)
    vcd = _load(str(p))
    transitions = _find_transitions(str(p), clock, edge, max_results=MAX_RESULTS_CAP)
    clock_times = transitions["times"][from_cycle:to_cycle]

    missing = []
    sig_map = {}
    for name in signals:
        try:
            sig_map[name] = _signal_lookup(vcd, name)
        except ValueError:
            missing.append(name)

    samples = []
    for i, t in enumerate(clock_times):
        vals = {}
        for name, sig in sig_map.items():
            raw = _value_at(sig.tv, t)
            if raw is None:
                raw = "x"
            vals[name] = format_value(raw, _sig_size(sig), fmt)
        samples.append({"cycle": from_cycle + i, "time": t, "values": vals})

    return {
        "clock": clock,
        "from_cycle": from_cycle,
        "to_cycle": to_cycle,
        "samples": samples,
        "missing": missing,
    }


# ── MCP tool registrations ────────────────────────────────────────────────────

@mcp.tool()
def list_signals(path: str, scope: str | None = None, pattern: str | None = None) -> dict:
    """List signals in a VCD file with optional scope/pattern filter."""
    return _list_signals(path, scope, pattern)


@mcp.tool()
def get_signal_value(path: str, signal: str, time: str, fmt: str = "hex") -> dict:
    """Get the value of a single signal at a given time."""
    return _get_signal_value(path, signal, time, fmt)


@mcp.tool()
def get_signals_at(path: str, signals: list[str], time: str, fmt: str = "hex") -> dict:
    """Get values of multiple signals at a given time."""
    return _get_signals_at(path, signals, time, fmt)


@mcp.tool()
def find_transitions(
    path: str,
    signal: str,
    edge: Literal["rising", "falling", "any"] = "rising",
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int = 50,
) -> dict:
    """Find times where a signal transitions (rising/falling/any edge)."""
    return _find_transitions(path, signal, edge, start_time, end_time, max_results)


@mcp.tool()
def find_condition(
    path: str,
    expr: str,
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int = 50,
) -> dict:
    """Find times where a boolean expression over signals evaluates true."""
    return _find_condition(path, expr, start_time, end_time, max_results)


@mcp.tool()
def sample_on_clock(
    path: str,
    signals: list[str],
    clock: str,
    from_cycle: int,
    to_cycle: int,
    edge: Literal["rising", "falling"] = "rising",
    fmt: str = "hex",
) -> dict:
    """Sample signal values at each clock edge over a cycle range."""
    return _sample_on_clock(path, signals, clock, from_cycle, to_cycle, edge, fmt)


@mcp.tool()
def check_invariant(path: str, expr: str, window: tuple[str, str] | None = None) -> dict:
    """Check that a boolean expression holds at every time step in the merged signal grid."""
    return _check_invariant(path, expr, window)


@mcp.tool()
def check_sequence(
    path: str,
    antecedent: str,
    consequent: str,
    within_cycles: int,
    clock: str,
    edge: str = "rising",
    window: tuple[str, str] | None = None,
) -> dict:
    """SVA-style antecedent |-> ##[0:within_cycles] consequent check on clock edges."""
    return _check_sequence(path, antecedent, consequent, within_cycles, clock, edge, window)


@mcp.tool()
def check_response(
    path: str,
    trigger: str,
    response: str,
    min_cycles: int,
    max_cycles: int,
    clock: str,
    edge: str = "rising",
    window: tuple[str, str] | None = None,
) -> dict:
    """Check every rising-edge of trigger is followed by rising-edge of response within [min_cycles, max_cycles]."""
    return _check_response(path, trigger, response, min_cycles, max_cycles, clock, edge, window)


@mcp.tool()
def check_stable(
    path: str,
    signal: str,
    while_expr: str,
    clock: str,
    edge: str = "rising",
    window: tuple[str, str] | None = None,
) -> dict:
    """Check that signal is constant across every contiguous run of clock edges where while_expr holds."""
    return _check_stable(path, signal, while_expr, clock, edge, window)


@mcp.tool()
def diff_waveforms(
    path: str,
    other_vcd: str,
    signals: list[str] | None = None,
    window: tuple[str, str] | None = None,
    max_results: int = 50,
) -> dict:
    """Compare two VCDs and report the first divergence per signal."""
    return _diff_waveforms(path, other_vcd, signals, window, max_results)


if __name__ == "__main__":
    mcp.run()
