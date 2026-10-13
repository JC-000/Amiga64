# fixtures/modwb/Makefile — the AmigaOS launcher of the MOD player (W16-X).
# One hunk executable, $(OUT)/modwb: the launcher (first: its first byte is
# the entry), the Wave 15 replay (MOD_HUNK) and UI (UI_HUNK), unchanged
# otherwise.  Outputs only in $(OUT) (default build/modwb, never committed).
# DEFS: extra vasm defines for every object (MOD_TRACE, MW_MUT_*, ...).
VBCC    ?= $(HOME)/.local/opt/vbcc
BIN     := $(VBCC)/bin
OUT     ?= ../../build/modwb
DEFS    ?=
RED     ?=
MOD     := ../mod
VDEFS   := -DUI_HUNK=1 $(foreach d,$(DEFS),-D$(d)=1)
ASM     := $(BIN)/vasmm68k_mot -quiet -m68000 -Fhunk $(VDEFS)
# RED=1 (leg S's red build): the replay without MOD_HUNK, i.e. at the W15
# fixed addresses $000400/$000480/$0004C0
RHUNK   := $(if $(RED),,-DMOD_HUNK=1)

OBJS    := $(OUT)/modwb.o $(OUT)/mwdata.o $(OUT)/replay.o $(OUT)/ui.o $(OUT)/mwend.o

.PHONY: all clean
all: $(OUT)/modwb

$(OUT):
	mkdir -p $@

$(OUT)/modwb.o: modwb.s os.i $(MOD)/mod.i | $(OUT)
	$(ASM) -DMOD_HUNK=1 -I $(MOD) -o $@ modwb.s

$(OUT)/mwdata.o: mwdata.s | $(OUT)
	$(ASM) -o $@ mwdata.s

$(OUT)/mwend.o: mwend.s | $(OUT)
	$(ASM) -o $@ mwend.s

$(OUT)/replay.o: $(MOD)/replay.s $(MOD)/mod.i $(MOD)/tables.i | $(OUT)
	$(ASM) $(RHUNK) -no-opt -I $(MOD) -o $@ $(MOD)/replay.s

$(OUT)/ui.o: $(MOD)/ui/ui.s $(wildcard $(MOD)/ui/*.i) | $(OUT)
	$(ASM) -I $(MOD)/ui -I $(MOD) -o $@ $(MOD)/ui/ui.s

$(OUT)/modwb: $(OBJS)
	$(BIN)/vlink -bamigahunk -Bstatic -M$(OUT)/modwb.map -o $@ $(OBJS)

clean:
	rm -f $(OBJS) $(OUT)/modwb $(OUT)/modwb.map
