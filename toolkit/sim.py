"""Sim leg: shared spec -> ngspice -> report.

Two analysis paths, chosen by the module's optional [sim] block:

  * no [sim]            -> passive-AC divider transfer (the attenuator). Pots
                          expand to a top/bottom resistor pair; sweeps the wiper.
  * [sim].analysis=tran -> general transient netlister: resistors, caps, pots,
                          and IC subckts (op-amps / OTAs from models.spice),
                          with supplies, stimulus, optional CV sweep, and a
                          frequency measurement (e.g. a VCO).

Component values pass straight through to ngspice (it parses 100k / 1n / 2.5meg),
so only the pot-expansion math needs numeric resistance.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .spec import Module, PinRef

GND_NET = "GND"
_MODELS = (Path(__file__).parent / "models.spice").read_text()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ohms(value: str) -> float:
    """Parse '100k', '4k7', '1meg', '470' -> ohms (for pot-split math)."""
    s = value.strip().lower().replace("meg", "M")
    mult = {"k": 1e3, "M": 1e6, "r": 1.0}
    m = re.fullmatch(r"(\d+)([kmr])(\d+)", s)
    if m:
        whole, suf, frac = m.groups()
        return float(f"{whole}.{frac}") * mult[suf.upper() if suf == "m" else suf]
    m = re.fullmatch(r"([\d.]+)([kMr]?)", s)
    if not m:
        raise ValueError(f"cannot parse resistance {value!r}")
    num, suf = m.groups()
    return float(num) * mult.get(suf, 1.0)


def _node(name: str) -> str:
    return "0" if name == GND_NET else name


def _pin_node(mod: Module, ref: str, pin: str) -> str:
    net = mod.net_of(PinRef(ref, pin))
    if net is None:
        raise ValueError(f"{ref}.{pin} is not on any net")
    return _node(net.name)


def _run_ngspice(netlist: str, datafile: str) -> np.ndarray:
    if shutil.which("ngspice") is None:
        raise RuntimeError("ngspice not found on PATH; install it to run sims")
    with tempfile.TemporaryDirectory() as td:
        cir = Path(td) / "ckt.cir"
        cir.write_text(netlist)
        proc = subprocess.run(["ngspice", "-b", str(cir)],
                              capture_output=True, text=True, cwd=td)
        dat = Path(td) / datafile
        if not dat.exists():
            raise RuntimeError(
                f"ngspice produced no output\n{proc.stdout}\n{proc.stderr}")
        return np.loadtxt(dat)


def _frequency(t: np.ndarray, v: np.ndarray) -> float | None:
    """Frequency from rising mean-crossings of the settled second half."""
    m = len(t) // 2
    t, v = t[m:], v[m:]
    vc = v - v.mean()
    cr = np.where((vc[:-1] < 0) & (vc[1:] >= 0))[0]
    if len(cr) < 2:
        return None
    return 1.0 / float(np.diff(t[cr]).mean())


# --------------------------------------------------------------------------- #
# passive AC path (attenuator)
# --------------------------------------------------------------------------- #
def _input_node(mod: Module) -> str:
    for c in mod.components:
        if c.role == "input":
            return _pin_node(mod, c.ref, "TIP")
    raise ValueError("no component with role='input'")


def _output_node(mod: Module) -> str:
    for c in mod.components:
        if c.role == "output":
            return _pin_node(mod, c.ref, "TIP")
    raise ValueError("no component with role='output'")


def build_netlist(mod: Module, wiper: float, freq: float = 1000.0) -> str:
    vin, vout = _input_node(mod), _output_node(mod)
    lines = [f"* {mod.title} attenuation sim (wiper={wiper:.3f})",
             f"V1 {vin} 0 AC 1"]
    for c in mod.components:
        if c.part.sim == "potentiometer":
            rtot = _ohms(c.value or "100k")
            n1 = _pin_node(mod, c.ref, "1")
            n2 = _pin_node(mod, c.ref, "2")
            n3 = _pin_node(mod, c.ref, "3")
            lines.append(f"Rtop_{c.ref} {n1} {n2} {max(rtot*(1-wiper),1e-3):.6g}")
            lines.append(f"Rbot_{c.ref} {n2} {n3} {max(rtot*wiper,1e-3):.6g}")
    lines += [".control", f"ac lin 1 {freq:g} {freq:g}",
              f"wrdata out.dat vm({vout})", ".endc", ".end", ""]
    return "\n".join(lines)


@dataclass
class SimPoint:
    wiper: float
    gain: float
    gain_db: float
    expected: float


@dataclass
class SimResult:
    module: str
    freq: float
    points: list[SimPoint]
    max_error: float

    @property
    def ok(self) -> bool:
        return self.max_error < 1e-3


def simulate(mod: Module, wipers=(0.0, 0.25, 0.5, 0.75, 1.0),
             freq: float = 1000.0) -> SimResult:
    points, max_err = [], 0.0
    for w in wipers:
        data = _run_ngspice(build_netlist(mod, w, freq), "out.dat")
        gain = float(np.atleast_1d(data)[-1])
        err = abs(gain - w)
        max_err = max(max_err, err)
        gdb = 20 * math.log10(gain) if gain > 0 else float("-inf")
        points.append(SimPoint(w, gain, gdb, w))
    return SimResult(mod.name, freq, points, max_err)


def report(res: SimResult) -> str:
    lines = [f"ngspice AC analysis @ {res.freq:g} Hz  (module: {res.module})",
             f"{'wiper':>6} {'gain V/V':>10} {'gain dB':>9} "
             f"{'expected':>9} {'err':>9}"]
    for p in res.points:
        db = f"{p.gain_db:8.2f}" if math.isfinite(p.gain_db) else "    -inf"
        lines.append(f"{p.wiper:6.2f} {p.gain:10.4f} {db} {p.expected:9.4f} "
                     f"{abs(p.gain - p.expected):9.2e}")
    lines.append(f"max error vs ideal divider: {res.max_error:.2e}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# general transient path (VCO, active circuits)
# --------------------------------------------------------------------------- #
def _ic_nodes(mod: Module, comp) -> tuple[list[str], list[str]]:
    """Nodes for an IC in pad order; unconnected pads get tie-off nets."""
    padnet: dict[str, str] = {}
    for net in mod.nets:
        for p in net.pins:
            if p.ref == comp.ref:
                padnet[mod.pad_number(p)] = _node(net.name)
    nodes, ties = [], []
    for k in range(1, comp.part.npads + 1):
        if str(k) in padnet:
            nodes.append(padnet[str(k)])
        else:
            tn = f"{comp.ref}_nc{k}"
            nodes.append(tn)
            ties.append(tn)
    return nodes, ties


def build_tran_netlist(mod: Module, overrides: dict[str, float]) -> str:
    sim = mod.sim
    lines = [f"* {mod.title} transient sim", _MODELS, ""]

    # supplies + stimulus as DC sources
    for net, volts in mod.supplies.items():
        lines.append(f"Vsup_{net} {_node(net)} 0 DC {volts}")
    stim = dict(sim.stimulus)
    stim.update(overrides)
    for net, volts in stim.items():
        lines.append(f"Vstim_{net} {_node(net)} 0 DC {volts}")

    # components
    for c in mod.components:
        kind = c.part.sim
        if kind == "resistor":
            a = _pin_node(mod, c.ref, "1")
            b = _pin_node(mod, c.ref, "2")
            lines.append(f"R{c.ref} {a} {b} {c.value}")
        elif kind == "capacitor":
            a = _pin_node(mod, c.ref, "1")
            b = _pin_node(mod, c.ref, "2")
            lines.append(f"C{c.ref} {a} {b} {c.value}")
        elif kind == "potentiometer":
            rtot = _ohms(c.value or "100k")
            n1 = _pin_node(mod, c.ref, "1")
            n2 = _pin_node(mod, c.ref, "2")
            n3 = _pin_node(mod, c.ref, "3")
            lines.append(f"Rtop_{c.ref} {n1} {n2} {max(rtot*(1-c.wiper),1e-3):.6g}")
            lines.append(f"Rbot_{c.ref} {n2} {n3} {max(rtot*c.wiper,1e-3):.6g}")
        elif kind == "ic":
            nodes, ties = _ic_nodes(mod, c)
            lines.append(f"X{c.ref} {' '.join(nodes)} {c.part.subckt}")
            for i, tn in enumerate(ties):
                lines.append(f"Rtie_{c.ref}_{i} {tn} 0 1g")
        # passthrough (jacks, power header): net membership only

    # initial conditions
    for net, volts in sim.ic.items():
        lines.append(f".ic v({_node(net)})={volts}")

    probes = sim.probes or ([sim.measure_net] if sim.measure_net else [])
    cols = " ".join(f"v({_node(p)})" for p in probes)
    uic = " uic" if sim.uic else ""
    lines += [".control", f"tran {sim.tstep} {sim.tstop}{uic}",
              f"wrdata tran.dat {cols}", ".endc", ".end", ""]
    return "\n".join(lines), probes


@dataclass
class TranResult:
    module: str
    sweep_net: str
    points: list[tuple[float, float]]   # (sweep value, frequency Hz)
    osc: bool
    fit: tuple[float, float, float] | None  # slope Hz/V, intercept Hz, R^2
    amp: tuple[float, float]            # measured probe min, max

    @property
    def ok(self) -> bool:
        return self.osc


def _measure(mod: Module, overrides: dict[str, float]):
    netlist, probes = build_tran_netlist(mod, overrides)
    data = _run_ngspice(netlist, "tran.dat")
    idx = probes.index(mod.sim.measure_net) if mod.sim.measure_net else 0
    t = data[:, 0]
    v = data[:, 1 + 2 * idx]
    return _frequency(t, v), (float(v.min()), float(v.max()))


def simulate_tran(mod: Module) -> TranResult:
    sim = mod.sim
    if sim.sweep_net and sim.sweep_values:
        points, amp = [], (0.0, 0.0)
        for val in sim.sweep_values:
            f, amp = _measure(mod, {sim.sweep_net: val})
            points.append((val, f if f else float("nan")))
        freqs = np.array([f for _, f in points])
        osc = bool(np.all(np.isfinite(freqs))) and (amp[1] - amp[0] > 0.1)
        fit = None
        if osc:
            xs = np.array([v for v, _ in points])
            a = np.polyfit(xs, freqs, 1)
            pred = np.polyval(a, xs)
            ss = ((freqs - pred) ** 2).sum()
            r2 = 1 - ss / max(((freqs - freqs.mean()) ** 2).sum(), 1e-30)
            fit = (float(a[0]), float(a[1]), float(r2))
        return TranResult(mod.name, sim.sweep_net, points, osc, fit, amp)

    f, amp = _measure(mod, {})
    osc = f is not None and (amp[1] - amp[0] > 0.1)
    return TranResult(mod.name, "", [(0.0, f if f else float("nan"))],
                      osc, None, amp)


def report_tran(res: TranResult) -> str:
    lines = [f"ngspice transient analysis  (module: {res.module})"]
    if res.sweep_net:
        lines.append(f"sweeping {res.sweep_net}:")
        lines.append(f"{res.sweep_net+'(V)':>8} {'freq(Hz)':>10}")
        for v, f in res.points:
            lines.append(f"{v:8.2f} {f:10.1f}")
    else:
        f = res.points[0][1]
        lines.append(f"oscillation frequency: {f:.1f} Hz" if res.osc
                     else "no oscillation detected")
    lines.append(f"probe swing: [{res.amp[0]:.2f}, {res.amp[1]:.2f}] V")
    if res.fit:
        s, b, r2 = res.fit
        lines.append(f"linear fit: f = {s:.2f}*{res.sweep_net} + {b:.1f} Hz "
                     f"(R^2 = {r2:.5f})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #
def analyze(mod: Module) -> tuple[str, bool]:
    """Run the appropriate analysis; return (report text, ok)."""
    if mod.sim and mod.sim.analysis == "tran":
        res = simulate_tran(mod)
        return report_tran(res), res.ok
    res = simulate(mod)
    return report(res), res.ok


if __name__ == "__main__":
    import sys
    from . import spec
    m = spec.load(sys.argv[1] if len(sys.argv) > 1
                  else "modules/attenuator/module.toml")
    text, ok = analyze(m)
    print(text)
    print("->", "PASS" if ok else "CHECK")
