PY      := python3
BUILD   := build

.PHONY: all host-test runtime vectors vice-test smoke clean

all: runtime

host-test:
	pytest -q

runtime: $(BUILD)/runner.prg

$(BUILD)/gen/dispatch.s: host/gen_dispatch.py
	mkdir -p $(BUILD)/gen
	$(PY) host/gen_dispatch.py -o $@

RUNTIME_SRC := $(wildcard runtime/*.s runtime/ops/*.s tests/runner/*.s)

$(BUILD)/runner.prg: $(RUNTIME_SRC) $(BUILD)/gen/dispatch.s runtime/c64.cfg
	mkdir -p $(BUILD)
	cl65 -t none -C runtime/c64.cfg -o $@ -Ln $(BUILD)/runner.lbl -m $(BUILD)/runner.map \
		$(RUNTIME_SRC) $(BUILD)/gen/dispatch.s

vectors:
	mkdir -p $(BUILD)/vectors
	$(PY) -m host.vectors --out $(BUILD)/vectors

vice-test: runtime vectors
	$(PY) -m tests.run_vectors --prg $(BUILD)/runner.prg --vectors $(BUILD)/vectors

smoke:
	$(PY) -m tests.smoke

clean:
	rm -rf $(BUILD)
