# Eurorack module toolchain.
#
# Usage:
#   make            # show this help
#   make all        # run every leg for the default module (attenuator)
#   make sim        # just the ngspice simulation
#   make pcb        # just the PCB (route + DRC + Gerbers)
#   make panel      # just the faceplate + mount
#   make fab        # PCB + a fab-ready Gerber zip for PCBWay
#
# Pick a different module with MODULE=<name>, e.g.:
#   make all MODULE=mixer

MODULE ?= attenuator
PY     ?= python3

SPEC  := modules/$(MODULE)/module.toml
OUT   := build/$(MODULE)
BOARD := $(OUT)/pcb/$(MODULE).kicad_pcb
ZIP   := $(OUT)/$(MODULE)-gerbers.zip

.DEFAULT_GOAL := help
.PHONY: all sim pcb panel drc fab clean clean-all list check help

all: $(SPEC) ## Run the full pipeline (sim -> PCB -> panel) for $(MODULE)
	$(PY) build.py $(SPEC)

sim: $(SPEC) ## Simulate the circuit with ngspice
	$(PY) -m toolkit.sim $(SPEC)

pcb: $(SPEC) ## Generate the PCB: route, DRC-gate, export Gerbers/drill/SVG
	$(PY) -m toolkit.pcb $(SPEC)

panel: $(SPEC) ## Generate the faceplate (STL/DXF/SVG), silkscreen, and PCB mount
	$(PY) -m toolkit.panel $(SPEC)

drc: ## Run KiCad DRC on the built board (run `make pcb` first)
	@test -f $(BOARD) || { echo "no board yet -- run: make pcb MODULE=$(MODULE)"; exit 1; }
	kicad-cli pcb drc --severity-all --exit-code-violations \
	  --output $(OUT)/pcb/$(MODULE)-drc.rpt $(BOARD)

fab: pcb ## Build the PCB and bundle Gerbers+drill into a fab-ready zip
	@rm -f $(ZIP)
	@zip -q -j $(ZIP) $(OUT)/pcb/gerbers/* \
	  || $(PY) -m zipfile -c $(ZIP) $(OUT)/pcb/gerbers
	@echo "  fab package -> $(ZIP)"

list: ## List available modules
	@ls -1 modules

clean: ## Remove build artifacts for $(MODULE)
	rm -rf $(OUT)

clean-all: ## Remove all build artifacts
	rm -rf build

check: ## Verify the toolchain dependencies are installed
	@echo "toolchain dependencies:"
	@command -v ngspice    >/dev/null && echo "  ngspice    ok" || echo "  ngspice    MISSING (apt install ngspice)"
	@command -v kicad-cli  >/dev/null && echo "  kicad-cli  ok" || echo "  kicad-cli  MISSING (install KiCad 9)"
	@command -v zip        >/dev/null && echo "  zip        ok" || echo "  zip        missing (fab zip falls back to python)"
	@command -v freert     >/dev/null && echo "  freert     ok" || echo "  freert     missing (only needed for router=freerouting)"
	@$(PY) -c "import toolkit, pcbnew" 2>/dev/null && echo "  pcbnew     ok" || echo "  pcbnew     MISSING (KiCad python module)"
	@$(PY) -c "import build123d"       2>/dev/null && echo "  build123d  ok" || echo "  build123d  MISSING (pip install build123d)"

help: ## Show this help
	@echo "Eurorack module toolchain  (MODULE=$(MODULE))"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'
