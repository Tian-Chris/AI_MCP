// 4-bit synchronous up-counter with synchronous active-high reset
module counter4 (
  input        clk,
  input        rst,
  output reg [3:0] count
);

  // Synchronous reset and increment logic
  always @(posedge clk) begin
    if (rst)
      count <= 4'b0000;
    else
      count <= count + 4'b0001;
  end

endmodule
