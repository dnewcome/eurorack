#!/usr/bin/env python3
"""End-to-end driver: one module spec -> simulated + fabricable outputs.

    python3 build.py modules/attenuator/module.toml

Runs all three legs of the toolchain from a single shared spec and drops every
artifact under build/<module>/. This is the first slice's payoff: idea (spec) ->
simulate -> PCB + fab files -> faceplate + mount, in one pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

from toolkit import spec
from toolkit import sim, pcb, panel


def main(spec_path: str) -> None:
    mod = spec.load(spec_path)
    out = Path("build") / mod.name
    print(f"=== {mod.title}  ({mod.hp}HP, {mod.panel_w:.2f} x {mod.panel_h} mm) ===\n")

    # 1. simulate
    print("[1/3] simulate (ngspice)")
    res = sim.simulate(mod)
    print(sim.report(res))
    ok = res.max_error < 1e-3
    print(f"  -> {'PASS' if ok else 'CHECK'}: divider transfer matches ideal\n")

    # 2. PCB + fab export (DRC-gated)
    print("[2/3] PCB (pcbnew + kicad-cli)")
    arts = pcb.generate(mod, out / "pcb")
    drc = arts.pop("drc")
    for k, v in arts.items():
        print(f"  {k}: {v}")
    print(f"  DRC: {drc.errors} errors, {drc.warnings} warnings"
          f"{' ' + str(dict(drc.by_type)) if drc.by_type else ''}")
    print(f"  -> {'PASS' if drc.clean else 'FAIL'}: design-rule check\n")

    # 3. mechanical
    print("[3/3] mechanical (build123d)")
    mech = panel.generate(mod, out / "panel")
    for k, v in mech.items():
        print(f"  {k}: {v}")
    print(f"\nDone. All artifacts under {out}/")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else "modules/attenuator/module.toml")
