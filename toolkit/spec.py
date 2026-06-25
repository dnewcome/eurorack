"""Shared module spec: the single source of truth.

A module is described once in a `module.toml` and loaded into the dataclasses
below. Every leg of the toolchain (sim / PCB / mechanical) consumes the *same*
loaded `Module` -- that shared consumption is the integration the first slice
exists to prove.

Coordinate convention (panel face):
    origin = top-left of the panel, +x right, +y down, millimetres.
The same (x, y) drives both the faceplate cutout and the PCB footprint placement.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .parts import Part, get_part

HP_MM = 5.08          # 1 HP
PANEL_H = 128.5       # 3U panel height (Doepfer)


@dataclass
class Component:
    ref: str          # e.g. "RV1", "J1"
    type: str         # key into PARTS
    x: float          # panel position, mm from top-left
    y: float
    role: str = ""    # "input" / "output" / "" -- used by the sim leg
    value: str = ""   # e.g. "100k" (pots/resistors), "1n" (caps)
    wiper: float = 0.5  # pot wiper position 0..1, for simulation
    rotation: float = 0.0  # footprint rotation in degrees (PCB placement)

    @property
    def part(self) -> Part:
        return get_part(self.type)


@dataclass
class PinRef:
    ref: str          # component ref
    pin: str          # logical pin name (per part.pins)

    @classmethod
    def parse(cls, token: str) -> "PinRef":
        ref, _, pin = token.partition(".")
        if not pin:
            raise ValueError(f"bad pin ref {token!r}; expected REF.PIN")
        return cls(ref.strip(), pin.strip())

    def __str__(self) -> str:
        return f"{self.ref}.{self.pin}"


@dataclass
class Net:
    name: str
    pins: list[PinRef]


@dataclass
class SimConfig:
    """Optional [sim] block: how to analyse the module.

    If absent, the sim leg falls back to its passive-AC divider analysis
    (used by the attenuator).
    """
    analysis: str = "ac"            # "ac" | "tran"
    tstep: str = "5us"
    tstop: str = "20ms"
    uic: bool = True
    ic: dict[str, float] = field(default_factory=dict)        # net -> volts
    probes: list[str] = field(default_factory=list)           # nets to record
    stimulus: dict[str, float] = field(default_factory=dict)  # net -> DC volts
    measure_net: str = ""           # net whose frequency we report
    sweep_net: str = ""             # net swept for a characterisation
    sweep_values: list[float] = field(default_factory=list)


@dataclass
class Module:
    name: str
    title: str
    hp: int
    description: str = ""
    components: list[Component] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    supplies: dict[str, float] = field(default_factory=dict)  # net -> volts
    sim: SimConfig | None = None

    # --- geometry -----------------------------------------------------------
    @property
    def panel_w(self) -> float:
        """Panel width in mm (HP pitch less the standard 0.3mm clearance)."""
        return self.hp * HP_MM - 0.3

    @property
    def panel_h(self) -> float:
        return PANEL_H

    # --- lookups ------------------------------------------------------------
    def component(self, ref: str) -> Component:
        for c in self.components:
            if c.ref == ref:
                return c
        raise KeyError(f"no component {ref!r}")

    def net_of(self, pinref: PinRef) -> Net | None:
        for n in self.nets:
            for p in n.pins:
                if p.ref == pinref.ref and p.pin == pinref.pin:
                    return n
        return None

    def pad_number(self, pinref: PinRef) -> str:
        """Resolve a logical pin to the footprint pad number."""
        part = self.component(pinref.ref).part
        try:
            return part.pins[pinref.pin]
        except KeyError:
            raise KeyError(
                f"{pinref.ref} ({part.type}) has no pin {pinref.pin!r}; "
                f"known: {sorted(part.pins)}"
            ) from None


def load(path: str | Path) -> Module:
    path = Path(path)
    data = tomllib.loads(path.read_text())

    m = data["module"]
    components = [
        Component(
            ref=c["ref"],
            type=c["type"],
            x=float(c["x"]),
            y=float(c["y"]),
            role=c.get("role", ""),
            value=c.get("value", ""),
            wiper=float(c.get("wiper", 0.5)),
            rotation=float(c.get("rotation", 0.0)),
        )
        for c in data.get("components", [])
    ]
    nets = [
        Net(name=n["name"], pins=[PinRef.parse(t) for t in n["connect"]])
        for n in data.get("nets", [])
    ]
    supplies = {k: float(v) for k, v in data.get("supplies", {}).items()}

    sim = None
    if "sim" in data:
        s = data["sim"]
        sw = s.get("sweep", {})
        sim = SimConfig(
            analysis=s.get("analysis", "ac"),
            tstep=s.get("tstep", "5us"),
            tstop=s.get("tstop", "20ms"),
            uic=s.get("uic", True),
            ic={k: float(v) for k, v in s.get("ic", {}).items()},
            probes=list(s.get("probes", [])),
            stimulus={k: float(v) for k, v in s.get("stimulus", {}).items()},
            measure_net=s.get("measure", {}).get("net", ""),
            sweep_net=sw.get("net", ""),
            sweep_values=[float(v) for v in sw.get("values", [])],
        )

    mod = Module(
        name=m["name"],
        title=m.get("title", m["name"]),
        hp=int(m["hp"]),
        description=m.get("description", ""),
        components=components,
        nets=nets,
        supplies=supplies,
        sim=sim,
    )
    _validate(mod)
    return mod


def _validate(mod: Module) -> None:
    refs = {c.ref for c in mod.components}
    if len(refs) != len(mod.components):
        raise ValueError("duplicate component refs")
    for net in mod.nets:
        for p in net.pins:
            if p.ref not in refs:
                raise ValueError(f"net {net.name!r} references unknown {p.ref}")
            mod.pad_number(p)  # raises if pin name invalid for the part
