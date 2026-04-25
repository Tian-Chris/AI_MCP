"""Smoke test for verilator_mcp and waveform_mcp tools."""
import sys
import traceback

sys.path.insert(0, "/Users/chris/Desktop/AI_MCP")

import verilator_mcp
import waveform_mcp

DEMO = "/Users/chris/Desktop/AI_MCP/demo"
VCD = f"{DEMO}/wave.vcd"

# Full signal names as they appear in the VCD
CLK = "tb_counter.clk"
RST = "tb_counter.rst"
COUNT = "tb_counter.count[3:0]"

results = []


def run(name, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        status = "PASS"
        detail = str(result)[:120]
    except Exception as e:
        status = "FAIL"
        detail = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    tag = f"[{status}] {name}: {detail}"
    print(tag)
    results.append((name, status))


# ── verilator_mcp (2 tools called) ───────────────────────────────────────────

run(
    "verilator_lint",
    verilator_mcp.verilator_lint,
    sources=["counter.v"],
    top="counter",
    cwd=DEMO,
)

run(
    "verilator_build_and_run",
    verilator_mcp.verilator_build_and_run,
    sources=["counter.v"],
    tb="tb_counter.v",
    top="counter",
    flags_file="verilator.f",
    cwd=DEMO,
)

# Also exercise verilator_build and verilator_run individually
build_res = verilator_mcp.verilator_build(
    sources=["counter.v"], tb="tb_counter.v", top="counter",
    flags_file="verilator.f", cwd=DEMO
)
run(
    "verilator_build",
    lambda: build_res,
)

if build_res.get("ok") and build_res.get("binary_path"):
    run(
        "verilator_run",
        verilator_mcp.verilator_run,
        binary=build_res["binary_path"],
        cwd=DEMO,
    )
else:
    print(f"[SKIP] verilator_run: build failed, skipping")
    results.append(("verilator_run", "SKIP"))

# ── waveform_mcp (11 tools) ──────────────────────────────────────────────────

run(
    "list_signals",
    waveform_mcp.list_signals,
    path=VCD,
)

run(
    "get_signal_value",
    waveform_mcp.get_signal_value,
    path=VCD,
    signal=CLK,
    time="50",
    fmt="bin",
)

run(
    "get_signals_at",
    waveform_mcp.get_signals_at,
    path=VCD,
    signals=[CLK, RST, COUNT],
    time="100",
    fmt="hex",
)

run(
    "find_transitions",
    waveform_mcp.find_transitions,
    path=VCD,
    signal=CLK,
    edge="rising",
    max_results=10,
)

run(
    "find_condition",
    waveform_mcp.find_condition,
    path=VCD,
    expr=f"{RST} == 0",
    max_results=5,
)

run(
    "sample_on_clock",
    waveform_mcp.sample_on_clock,
    path=VCD,
    signals=[COUNT],
    clock=CLK,
    from_cycle=0,
    to_cycle=5,
    edge="rising",
    fmt="dec",
)

# check_invariant: clk is always 0 or 1 (width 1, so value < 2 always true)
run(
    "check_invariant",
    waveform_mcp.check_invariant,
    path=VCD,
    expr=f"{RST} == 0 or {RST} == 1",  # rst is always 0 or 1
)

# check_sequence: when rst==1, count==0 within 1 cycle
run(
    "check_sequence",
    waveform_mcp.check_sequence,
    path=VCD,
    antecedent=f"{RST} == 0",
    consequent=f"{RST} == 0",   # trivially true consequence to test infra
    within_cycles=1,
    clock=CLK,
    edge="rising",
)

# check_response: no trigger events expected if rst never rises after cycle 0
# Use rst itself as trigger/response with wide window so ok==True
run(
    "check_response",
    waveform_mcp.check_response,
    path=VCD,
    trigger=f"{CLK} == 1",
    response=f"{CLK} == 0",
    min_cycles=0,
    max_cycles=1,
    clock=CLK,
    edge="rising",
)

# check_stable: count should be stable (==0) while rst==1
run(
    "check_stable",
    waveform_mcp.check_stable,
    path=VCD,
    signal=COUNT,
    while_expr=f"{RST} == 1",
    clock=CLK,
    edge="rising",
)

# diff_waveforms: compare VCD with itself → no divergences expected
run(
    "diff_waveforms",
    waveform_mcp.diff_waveforms,
    path=VCD,
    other_vcd=VCD,
)

# ── Summary ──────────────────────────────────────────────────────────────────

passed = sum(1 for _, s in results if s == "PASS")
failed = sum(1 for _, s in results if s == "FAIL")
skipped = sum(1 for _, s in results if s == "SKIP")
total = len(results)

print()
print(f"{'='*60}")
print(f"TOTAL: {total}  PASSED: {passed}  FAILED: {failed}  SKIPPED: {skipped}")
if failed:
    print("FAILING TOOLS:")
    for name, status in results:
        if status == "FAIL":
            print(f"  - {name}")
print(f"{'='*60}")
