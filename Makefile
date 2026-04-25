.PHONY: all lint lint-all sim wave synth show sta clean help cosim cosim-matmul cosim-softmax cosim-layernorm cosim-activation cosim-residual_add cosim-fp-primitives cosim-fp16-mult cosim-fp32-add cosim-fp32-to-fp16 cosim-fp16-add cosim-fp16-compare cosim-clean sim-small test-small test-top test-matmul1k test-1k test-1k-debug test-decode test-multi test-token test-golden test-golden-24 test-golden-1k test-golden-multi test-golden-token test-golden-token-24 test-pytorch test-diag

STA_TIME ?= 10.0

ifeq ($(TARGET),)
    ifeq ($(wildcard scripts/.last_target),)
    else
        TARGET := $(shell cat scripts/.last_target)
    endif
endif

SAVE_TARGET := $(shell echo $(TARGET) > scripts/.last_target)

RTL_DIR = rtl
TB_DIR  = tb

RTL_ALL = \
    $(RTL_DIR)/bram_controller.v \
    $(RTL_DIR)/mac_unit.v \
    $(RTL_DIR)/fp16_mult.v \
    $(RTL_DIR)/fp32_add.v \
    $(RTL_DIR)/fp32_to_fp16.v \
    $(RTL_DIR)/fp16_add.v \
    $(RTL_DIR)/fp16_compare.v \
    $(RTL_DIR)/fp_mac_unit.v \
    $(RTL_DIR)/agu.v \
    $(RTL_DIR)/matmul_engine.v \
    $(RTL_DIR)/mem_arbiter.v \
    $(RTL_DIR)/tiling_engine.v \
    $(RTL_DIR)/softmax.v \
    $(RTL_DIR)/layernorm.v \
    $(RTL_DIR)/activation.v \
    $(RTL_DIR)/residual_add.v \
    $(RTL_DIR)/quant_layer.v \
    $(RTL_DIR)/host_interface.v \
    $(RTL_DIR)/positional_embedding.v \
    $(RTL_DIR)/fsm_controller.v \
    $(RTL_DIR)/sim_hbm.v \
    $(RTL_DIR)/debug_writer.v \
    $(RTL_DIR)/uram_accum_buf.v \
    $(RTL_DIR)/tile_loader.v \
    $(RTL_DIR)/uram_flush.v \
    $(RTL_DIR)/act_dma.v \
    $(RTL_DIR)/uram_nm_adapter.v \
    $(RTL_DIR)/uram_prefetch_buf.v \
    $(RTL_DIR)/hbm_prefetch.v \
    $(RTL_DIR)/top_level.v

ifeq ($(TARGET),top_level)
    TOP := diffusion_transformer_top
    SRC := $(RTL_ALL)
    SIM := tb_top
    TB  := $(TB_DIR)/tb_top.v
endif

ifeq ($(TARGET),bram_controller)
    TOP := bram_controller
    SRC := $(RTL_DIR)/bram_controller.v
    SIM := tb_bram_controller
    TB  := $(TB_DIR)/tb_bram_controller.v
endif

ifeq ($(TARGET),agu)
    TOP := agu
    SRC := $(RTL_DIR)/agu.v
    SIM := tb_agu
    TB  := $(TB_DIR)/tb_agu.v
endif

ifeq ($(TARGET),matmul_engine)
    TOP := matmul_controller
    SRC := $(RTL_DIR)/mac_unit.v $(RTL_DIR)/agu.v $(RTL_DIR)/matmul_engine.v \
           $(RTL_DIR)/tile_loader.v $(RTL_DIR)/sim_hbm_port.v $(RTL_DIR)/uram_accum_buf.v
    SIM := tb_matmul
    TB  := $(TB_DIR)/tb_matmul.v
endif

ifeq ($(TARGET),softmax)
    TOP := softmax
    SRC := $(RTL_DIR)/softmax.v
    SIM := tb_softmax
    TB  := $(TB_DIR)/tb_softmax.v
endif

ifeq ($(TARGET),layernorm)
    TOP := layernorm
    SRC := $(RTL_DIR)/layernorm.v
    SIM := tb_layernorm
    TB  := $(TB_DIR)/tb_layernorm.v
endif

ifeq ($(TARGET),activation)
    TOP := activation_unit
    SRC := $(RTL_DIR)/activation.v
    SIM := tb_activation
    TB  := $(TB_DIR)/tb_activation.v
endif

ifeq ($(TARGET),residual_add)
    TOP := residual_add
    SRC := $(RTL_DIR)/residual_add.v
    SIM := tb_residual_add
    TB  := $(TB_DIR)/tb_residual_add.v
endif


ifeq ($(TARGET),host_interface)
    TOP := host_interface
    SRC := $(RTL_DIR)/host_interface.v
    SIM := tb_host_interface
    TB  := $(TB_DIR)/tb_host_interface.v
endif

ifeq ($(TARGET),fsm_controller)
    TOP := fsm_controller
    SRC := $(RTL_DIR)/fsm_controller.v
    SIM := tb_fsm_controller
    TB  := $(TB_DIR)/tb_fsm_controller.v
endif

ifeq ($(TARGET),uram_accum_buf)
    TOP := uram_accum_buf
    SRC := $(RTL_DIR)/uram_accum_buf.v
    SIM := tb_uram_accum_buf
    TB  := $(TB_DIR)/tb_uram_accum_buf.v
endif

ifeq ($(TARGET),sim_hbm_port)
    TOP := sim_hbm_port
    SRC := $(RTL_DIR)/sim_hbm_port.v
    SIM := tb_sim_hbm_port
    TB  := $(TB_DIR)/tb_sim_hbm_port.v
endif

ifeq ($(TARGET),tile_loader)
    TOP := tile_loader
    SRC := $(RTL_DIR)/tile_loader.v $(RTL_DIR)/sim_hbm_port.v
    SIM := tb_tile_loader
    TB  := $(TB_DIR)/tb_tile_loader.v
endif

ifeq ($(TARGET),uram_flush)
    TOP := uram_flush
    SRC := $(RTL_DIR)/uram_flush.v $(RTL_DIR)/uram_accum_buf.v $(RTL_DIR)/sim_hbm_port.v
    SIM := tb_uram_flush
    TB  := $(TB_DIR)/tb_uram_flush.v
endif

ifeq ($(TARGET),act_dma)
    TOP := act_dma
    SRC := $(RTL_DIR)/act_dma.v $(RTL_DIR)/sim_hbm_port.v
    SIM := tb_act_dma
    TB  := $(TB_DIR)/tb_act_dma.v
endif

ifeq ($(TARGET),uram_nm_adapter)
    TOP := uram_nm_adapter
    SRC := $(RTL_DIR)/uram_nm_adapter.v
    SIM := tb_uram_nm_adapter
    TB  := $(TB_DIR)/tb_uram_nm_adapter.v
endif

lint:
	verilator --lint-only -f scripts/verilator.f $(SRC)

lint-all:
	verilator --lint-only -f scripts/verilator.f $(TB) $(SRC)

sim:
	verilator --binary -f scripts/verilator.f \
		$(TB) $(SRC) \
		--top-module $(SIM)
	./obj_dir/V$(SIM)

wave:
	gtkwave scripts/wave.vcd

synth:
	yosys -p "\
	read_liberty -lib scripts/NangateOpenCellLibrary_typical.lib; \
	read_verilog -sv $(SRC); \
	synth -top $(TOP); \
	dfflibmap -liberty scripts/NangateOpenCellLibrary_typical.lib; \
	abc -liberty scripts/NangateOpenCellLibrary_typical.lib; \
	stat; \
	write_verilog scripts/mapped.v"

show:
	yosys -p "\
	read_liberty -lib scripts/NangateOpenCellLibrary_typical.lib; \
	read_verilog -sv $(SRC); \
	hierarchy -top $(TOP); \
	flatten; \
	proc; \
	opt; \
	show;"

show-synth:
	yosys -p "\
	read_liberty -lib scripts/NangateOpenCellLibrary_typical.lib; \
	read_verilog -sv $(SRC); \
	synth -top $(TOP); \
	dfflibmap -liberty scripts/NangateOpenCellLibrary_typical.lib; \
	abc -liberty scripts/NangateOpenCellLibrary_typical.lib; \
	hierarchy -top $(TOP); \
	flatten; \
	proc; \
	opt; \
	show; \
	stat; \
	write_verilog scripts/mapped.v"

sta:
	@{ \
		echo "read_liberty scripts/NangateOpenCellLibrary_typical.lib"; \
		echo "read_verilog scripts/mapped.v"; \
		echo "link_design $(TOP)"; \
		echo "create_clock -name clk -period $(STA_TIME) {clk}"; \
		echo "set_input_delay -clock clk 1 [all_inputs]"; \
		echo "report_checks -path_delay min_max"; \
	} | sta

clean:
	rm -rf obj_dir *.vcd scripts/mapped.v scripts/wave.vcd

help:
	@echo "Usage: make [target] TARGET=<module> [VAR=value]"
	@echo ""
	@echo "Targets: lint lint-all sim wave synth show show-synth sta clean all"
	@echo "         cosim cosim-{matmul,softmax,layernorm,activation,residual_add}"
	@echo "         test-small sim-small test-1k test-decode test-multi test-token"
	@echo "         test-golden test-pytorch (see verify/README.md for details)"
	@echo ""
	@echo "TARGETs: top_level bram_controller agu matmul_engine softmax"
	@echo "         layernorm activation residual_add host_interface fsm_controller"
	@echo ""
	@echo "Variables: TARGET (remembered), STA_TIME (default: 10.0ns)"

cosim:
	python3 verify/run_cosim.py

cosim-matmul:
	python3 verify/run_cosim.py matmul

cosim-softmax:
	python3 verify/run_cosim.py softmax

cosim-layernorm:
	python3 verify/run_cosim.py layernorm

cosim-activation:
	python3 verify/run_cosim.py activation

cosim-residual_add:
	python3 verify/run_cosim.py residual_add

cosim-fp-primitives:
	python3 verify/run_cosim.py fp-primitives

cosim-fp16-mult:
	python3 verify/run_cosim.py fp16_mult

cosim-fp32-add:
	python3 verify/run_cosim.py fp32_add

cosim-fp32-to-fp16:
	python3 verify/run_cosim.py fp32_to_fp16

cosim-fp16-add:
	python3 verify/run_cosim.py fp16_add

cosim-fp16-compare:
	python3 verify/run_cosim.py fp16_compare

cosim-clean:
	rm -rf verify/test_data/*.hex

sim-small:
	python3 verify/test_top.py --data-only
	verilator --binary -f scripts/verilator_small.f \
		$(TB_DIR)/tb_top.v $(RTL_ALL) \
		--top-module tb_top
	./obj_dir/Vtb_top

test-small:
	python3 verify/test_top.py

test-top: test-small

test-matmul1k:
	python3 verify/test_matmul1k.py

test-1k:
	python3 verify/test_top_1k.py

test-1k-debug:
	STEP_DEBUG=1 python3 verify/test_top_1k.py

test-decode:
	python3 verify/test_decode_1k.py

test-multi:
	python3 verify/test_multi_layer.py

test-token:
	python3 verify/test_token_cosim.py

test-golden:
	python3 verify/test_golden_intacc.py

test-golden-24:
	NUM_LAYERS=24 python3 verify/test_golden_intacc.py

test-golden-1k:
	python3 verify/test_top_1k.py --golden-only

test-golden-multi:
	python3 verify/test_multi_layer.py --golden-only

test-golden-token:
	python3 verify/test_token_cosim.py --golden-only

test-golden-token-24:
	NUM_LAYERS=24 python3 verify/test_token_cosim.py --golden-only

test-diag:
	python3 verify/test_diag.py

test-pytorch:
	python3.11 verify/fp32_baseline.py

all: lint sim synth sta
