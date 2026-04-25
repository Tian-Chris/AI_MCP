.PHONY: lint sim wave clean

# Default target
all: sim

lint:
	verilator --lint-only -f verilator.f counter.v tb_counter.v --top-module tb_counter

sim:
	verilator --binary -f verilator.f counter.v tb_counter.v --top-module tb_counter
	./obj_dir/Vtb_counter

wave:
	gtkwave wave.vcd

clean:
	rm -rf obj_dir wave.vcd
