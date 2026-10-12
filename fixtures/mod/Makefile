# fixtures/mod/Makefile — the MOD player program (W15-M).  Outputs only in
# $(OUT) (default build/mod, never committed).  DEFS: extra vasm defines
# (MOD_TRACE, MOD_STUB, MOD_MUT_*), e.g. make DEFS="MOD_TRACE MOD_MUT_ARP" OUT=...
VBCC    ?= $(HOME)/.local/opt/vbcc
export VBCC
BIN     := $(VBCC)/bin
OUT     ?= ../../build/mod
DEFS    ?=
UI_SRC  ?= ui_stub.c
VDEFS   := $(foreach d,$(DEFS),-D$(d)=1)

OBJS    := $(OUT)/crt0.o $(OUT)/replay.o $(OUT)/main.o $(OUT)/ui.o

.PHONY: all clean
all: $(OUT)/modplay.bin

$(OUT):
	mkdir -p $@

$(OUT)/crt0.o: crt0.s mod.i | $(OUT)
	$(BIN)/vasmm68k_mot -quiet -m68000 -no-opt -Fvobj $(VDEFS) -o $@ crt0.s

$(OUT)/replay.o: replay.s mod.i tables.i | $(OUT)
	$(BIN)/vasmm68k_mot -quiet -m68000 -no-opt -Fvobj $(VDEFS) -o $@ replay.s

$(OUT)/main.o: main.c mod_view.h | $(OUT)
	PATH=$(BIN):$$PATH $(BIN)/vc +aos68k -c -O2 -cpu=68000 -o $@ main.c

# UI_SRC: C (vbcc) or 68k asm (the W15-U kit: UI_SRC=ui/ui.s, vasm -Fhunk -I ui);
# it is rebuilt when its directory's include files change, and gets DEFS too
UI_DEPS := $(wildcard $(dir $(UI_SRC))*.i $(dir $(UI_SRC))*.h)
$(OUT)/ui.o: $(UI_SRC) $(UI_DEPS) mod_view.h | $(OUT)
ifeq ($(suffix $(UI_SRC)),.s)
	$(BIN)/vasmm68k_mot -quiet -m68000 -Fhunk $(VDEFS) -I $(dir $(UI_SRC)) -I . -o $@ $(UI_SRC)
else
	PATH=$(BIN):$$PATH $(BIN)/vc +aos68k -c -O2 -cpu=68000 -I. -o $@ $(UI_SRC)
endif

$(OUT)/modplay.bin: $(OBJS) link.ld
	$(BIN)/vlink -brawbin1 -T link.ld -M$(OUT)/modplay.map -o $@ $(OBJS)

clean:
	rm -f $(OBJS) $(OUT)/modplay.bin $(OUT)/modplay.map
