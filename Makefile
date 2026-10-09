PY      := python3
BUILD   := build

.PHONY: all host-test runtime vectors vice-test smoke rescheck clean
.DELETE_ON_ERROR:               # W12-T: a failed stamp leaves no unstamped runner.prg

all: runtime

host-test:
	pytest -q

runtime: $(BUILD)/runner.prg

$(BUILD)/gen/dispatch.s: host/gen_dispatch.py runtime/core.s $(wildcard runtime/ops/*.s)
	mkdir -p $(BUILD)/gen
	$(PY) host/gen_dispatch.py -o $@

# W7-T: chip.s links LAST so adding it moves no other module (perf layout);
# W8-L: mach.s after it, for the same reason; W9-C: cia.s between them;
# W10-P: cop.s and W10-V: disp.s (whole pages of CODE and BSS) after cia.s, before mach.s
# W11-B: blt.s after cop.s, before disp.s
# W12-T: snap.s then ureu.s after mach.s (their BSS last: snap.s's image ends
# at snap_keep, so nothing of theirs but snap_chain is in it)
RUNTIME_SRC := $(filter-out runtime/ops/chip.s runtime/ops/cia.s runtime/ops/cop.s runtime/ops/blt.s runtime/ops/disp.s runtime/ops/mach.s runtime/ops/td.s runtime/ops/snap.s runtime/ops/ureu.s,$(wildcard runtime/*.s runtime/ops/*.s tests/runner/*.s)) runtime/ops/chip.s runtime/ops/cia.s runtime/ops/cop.s runtime/ops/blt.s runtime/ops/disp.s runtime/ops/mach.s runtime/ops/td.s runtime/ops/snap.s runtime/ops/ureu.s
RUNTIME_INC := runtime/macros.inc $(wildcard runtime/rc/*.inc) $(wildcard runtime/ops/*.inc)

$(BUILD)/runner.prg: $(RUNTIME_SRC) $(RUNTIME_INC) $(BUILD)/gen/dispatch.s runtime/c64.cfg host/snapfmt.py
	mkdir -p $(BUILD)
	cl65 -t none -C runtime/c64.cfg -o $@ -Ln $(BUILD)/runner.lbl -m $(BUILD)/runner.map \
		$(RUNTIME_SRC) $(BUILD)/gen/dispatch.s
	$(PY) -m host.snapfmt --stamp $@ $(BUILD)/runner.lbl   # W12-T: build ID (I3)

vectors:
	mkdir -p $(BUILD)/vectors
	$(PY) -m host.vectors --out $(BUILD)/vectors --offline

vice-test: runtime vectors
	$(PY) -m tests.run_vectors --prg $(BUILD)/runner.prg --vectors $(BUILD)/vectors --jobs 4
	$(PY) -m host.handvec --out $(BUILD)/vectors-hand
	$(PY) -m tests.run_vectors --prg $(BUILD)/runner.prg --vectors $(BUILD)/vectors-hand

smoke:
	$(PY) -m tests.smoke

# Resource-contract checker: runtime/rc/*.inc vs. sources (docs/RESOURCES.md)
rescheck:
	$(PY) -m host.rescheck

clean:
	rm -rf $(BUILD)
