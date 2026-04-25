// Testbench for counter4: 4-bit synchronous up-counter with reset
module tb_counter4;

  // DUT port declarations
  reg  clk;
  reg  rst;
  wire [3:0] count;

  // Instantiate DUT
  counter4 dut (
    .clk(clk),
    .rst(rst),
    .count(count)
  );

  // Clock generation: 10 ns period
  always #5 clk = ~clk;

  // Integer for loop index and expected value tracking
  integer i;
  reg [3:0] expected;

  initial begin
    // Waveform dump
    $dumpfile("tb_counter4.vcd");
    $dumpvars(0, tb_counter4);

    // Initialize signals
    clk = 0;
    rst = 0;
    expected = 0;

    // ---------------------------------------------------------
    // Phase 1: Apply synchronous reset for 2+ clock cycles
    //          and verify count is cleared to 0
    // ---------------------------------------------------------
    rst = 1;
    @(posedge clk); #1; // sample after first rising edge with rst=1
    if (count !== 4'd0) begin
      $display("FAIL: reset cycle 1 expected 0 got %0d", count);
      $finish;
    end

    @(posedge clk); #1; // sample after second rising edge with rst=1
    if (count !== 4'd0) begin
      $display("FAIL: reset cycle 2 expected 0 got %0d", count);
      $finish;
    end

    // ---------------------------------------------------------
    // Phase 2: Deassert reset and verify count increments
    //          by 1 each cycle for a full 0-15 sequence
    // ---------------------------------------------------------
    rst = 0;
    expected = 4'd0;

    for (i = 0; i < 16; i = i + 1) begin
      @(posedge clk); #1;
      expected = expected + 4'd1; // expect count to have incremented
      if (count !== expected) begin
        $display("FAIL: increment step %0d expected %0d got %0d", i, expected, count);
        $finish;
      end
    end

    // ---------------------------------------------------------
    // Phase 3: Verify wraparound from 15 back to 0
    //          (already exercised in loop above: after 16 steps
    //           expected wraps to 0; add one extra explicit check)
    // ---------------------------------------------------------
    // At this point count should be 0 after the 16th increment (wraparound)
    if (count !== 4'd0) begin
      $display("FAIL: wraparound expected 0 got %0d", count);
      $finish;
    end

    // ---------------------------------------------------------
    // Phase 4: Re-assert reset mid-count to confirm sync clear
    // ---------------------------------------------------------
    // Let it count up a few cycles
    @(posedge clk); #1; // count = 1
    @(posedge clk); #1; // count = 2
    @(posedge clk); #1; // count = 3
    rst = 1;
    @(posedge clk); #1; // should reset to 0
    if (count !== 4'd0) begin
      $display("FAIL: mid-count reset expected 0 got %0d", count);
      $finish;
    end
    rst = 0;

    $display("PASS");
    $finish;
  end

endmodule
