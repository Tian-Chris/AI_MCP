// Testbench for the counter module
module tb_counter;

reg clk;
reg rst;
wire [3:0] count;

// Instantiate the DUT
counter dut (
    .clk   (clk),
    .rst   (rst),
    .count (count)
);

// Clock generation: period = 10 time units, toggle every 5
initial clk = 0;
always #5 clk = ~clk;

// Reset and stimulus
initial begin
    $dumpfile("wave.vcd");
    $dumpvars(0, tb_counter);

    rst = 1;          // Assert reset
    #20;
    rst = 0;          // Deassert reset after 20 time units

    #180;             // Run for remaining time (200 total)
    $finish;
end

// Display count on each posedge clk after reset is deasserted
always @(posedge clk) begin
    if (!rst)
        $display("Time=%0t  count=%0d", $time, count);
end

endmodule
