"""Sim leg: shared spec -> ngspice netlist -> run -> attenuation report.

Generates an ngspice netlist from the module's nets and parts, runs ngspice
headless, and reports the small-signal transfer (output / input) of the passive
network as the potentiometer wiper is swept.

Scope: handles passive R networks driven from one input jack and probed at one
output jack, with potentiometers expanded to a top/bottom resistor pair. That
covers the attenuator (and any passive divider) -- richer netlisting is a later
generalisation, deliberately out of scope for the first slice.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .spec import Module, PinRef

GND_NET = "GND"


def _ohms(value: str) -> float:
    """Parse '100k', '4k7', '1M', '470' -> ohms."""
    s = value.strip().lower()
    mult = {"k": 1e3, "m": 1e6, "r": 1.0}
    # forms like 4k7 -> 4700
    m = re.fullmatch(r"(\d+)([kmr])(\d+)", s)
    if m:
        whole, suf, frac = m.groups()
        return float(f"{whole}.{frac}") * mult[suf]
    m = re.fullmatch(r"([\d.]+)([kmr]?)", s)
    if not m:
        raise ValueError(f"cannot parse resistance {value!r}")
    num, suf = m.groups()
    return float(num) * (mult.get(suf, 1.0))


def _node(mod: Module, pinref: PinRef) -> str:
    """ngspice node name for a pin: the net it sits on (GND -> '0')."""
    net = mod.net_of(pinref)
    if net is None:
        raise ValueError(f"{pinref} is not on any net")
    return "0" if net.name == GND_NET else net.name


def _input_node(mod: Module) -> str:
    for c in mod.components:
        if c.role == "input":
            return _node(mod, PinRef(c.ref, "TIP"))
    raise ValueError("no component with role='input'")


def _output_node(mod: Module) -> str:
    for c in mod.components:
        if c.role == "output":
            return _node(mod, PinRef(c.ref, "TIP"))
    raise ValueError("no component with role='output'")


def build_netlist(mod: Module, wiper: float, freq: float = 1000.0,
                  out_file: str = "out.dat") -> str:
    """Render an ngspice netlist for one wiper position (0..1)."""
    vin = _input_node(mod)
    vout = _output_node(mod)
    lines = [
        f"* {mod.title} attenuation sim (wiper={wiper:.3f})",
        f"V1 {vin} 0 AC 1",
    ]
    for c in mod.components:
        part = c.part
        if part.sim == "potentiometer":
            rtot = _ohms(c.value or "100k")
            n1 = _node(mod, PinRef(c.ref, "1"))
            n2 = _node(mod, PinRef(c.ref, "2"))
            n3 = _node(mod, PinRef(c.ref, "3"))
            # pin1->wiper carries (1-w) of the track, wiper->pin3 carries w.
            # Output at the wiper => Vout/Vin = w (for in@1, gnd@3).
            rtop = max(rtot * (1.0 - wiper), 1e-3)
            rbot = max(rtot * wiper, 1e-3)
            lines.append(f"Rtop_{c.ref} {n1} {n2} {rtop:.6g}")
            lines.append(f"Rbot_{c.ref} {n2} {n3} {rbot:.6g}")
        elif part.sim == "resistor":
            # two-terminal resistor across its first two pins
            pins = list(part.pins)
            a = _node(mod, PinRef(c.ref, pins[0]))
            b = _node(mod, PinRef(c.ref, pins[1]))
            lines.append(f"R_{c.ref} {a} {b} {_ohms(c.value):.6g}")
        # passthrough parts (jacks) contribute only their net membership
    lines += [
        ".control",
        f"ac lin 1 {freq:g} {freq:g}",
        f"wrdata {out_file} vm({vout})",
        ".endc",
        ".end",
        "",
    ]
    return "\n".join(lines)


@dataclass
class SimPoint:
    wiper: float
    gain: float          # linear V/V
    gain_db: float
    expected: float      # analytic prediction


@dataclass
class SimResult:
    module: str
    freq: float
    points: list[SimPoint]
    max_error: float     # largest |gain - expected| across the sweep


def _run_ngspice(netlist: str, out_path: Path) -> float:
    if shutil.which("ngspice") is None:
        raise RuntimeError("ngspice not found on PATH; install it to run sims")
    with tempfile.TemporaryDirectory() as td:
        cir = Path(td) / "ckt.cir"
        dat = Path(td) / out_path.name
        cir.write_text(netlist.replace(out_path.name, dat.name))
        proc = subprocess.run(
            ["ngspice", "-b", str(cir)],
            capture_output=True, text=True, cwd=td,
        )
        if not dat.exists():
            raise RuntimeError(
                f"ngspice produced no output\nstdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}"
            )
        # wrdata format: "<freq> <vm>"  (one row for a single-point .ac)
        row = dat.read_text().split()
        return float(row[-1])


def simulate(mod: Module, wipers=(0.0, 0.25, 0.5, 0.75, 1.0),
             freq: float = 1000.0) -> SimResult:
    points: list[SimPoint] = []
    max_err = 0.0
    for w in wipers:
        nl = build_netlist(mod, w, freq)
        gain = _run_ngspice(nl, Path("out.dat"))
        expected = w  # ideal divider transfer for this topology
        err = abs(gain - expected)
        max_err = max(max_err, err)
        gdb = 20 * math.log10(gain) if gain > 0 else float("-inf")
        points.append(SimPoint(w, gain, gdb, expected))
    return SimResult(mod.name, freq, points, max_err)


def report(res: SimResult) -> str:
    lines = [
        f"ngspice AC analysis @ {res.freq:g} Hz  (module: {res.module})",
        f"{'wiper':>6} {'gain V/V':>10} {'gain dB':>9} {'expected':>9} {'err':>9}",
    ]
    for p in res.points:
        db = f"{p.gain_db:8.2f}" if math.isfinite(p.gain_db) else "    -inf"
        lines.append(
            f"{p.wiper:6.2f} {p.gain:10.4f} {db} {p.expected:9.4f} "
            f"{abs(p.gain - p.expected):9.2e}"
        )
    lines.append(f"max error vs ideal divider: {res.max_error:.2e}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from . import spec
    m = spec.load(sys.argv[1] if len(sys.argv) > 1
                  else "modules/attenuator/module.toml")
    print(report(simulate(m)))
