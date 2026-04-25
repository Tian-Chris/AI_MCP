# STA script for OpenSTA
read_liberty scripts/NangateOpenCellLibrary_typical.lib
read_verilog scripts/mapped.v
link_design diffusion_transformer_top

# Create clock (adjust period as needed)
create_clock -name clk -period 10.0 {clk}
set_input_delay -clock clk 1 [all_inputs]

# Run setup/hold timing reports
report_checks -path_delay min_max
