"""Parts registry.

Maps an abstract component *type* (used in a module spec) to the concrete
details each leg of the toolchain needs:

  - footprint  : KiCad library footprint (PCB leg)
  - pins       : logical pin name -> footprint pad number (netlist <-> PCB)
  - panel_d    : panel cutout diameter in mm (mechanical leg)
  - sim        : how the sim leg models the part ("resistor", "potentiometer",
                 or "passthrough" for a part with no electrical model of its own)

Keeping this table in one place is the heart of the "shared interchange": every
leg reads the same definitions, so a part is described once and consumed by sim,
PCB, and mechanical generators alike.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Part:
    type: str
    footprint: str
    pins: dict[str, str]
    panel_d: float          # panel cutout diameter (0 = board-only, no cutout)
    sim: str                # "resistor"|"capacitor"|"potentiometer"|
                            # "ic"|"passthrough"
    subckt: str = ""        # SPICE subckt name (sim == "ic")
    npads: int = 0          # pad count, for emitting all IC nodes in order


def _ic_pins(npads: int, aliases: dict[str, int]) -> dict[str, str]:
    """Numeric self-maps for every pad, plus friendly aliases."""
    pins = {str(n): str(n) for n in range(1, npads + 1)}
    pins.update({name: str(pad) for name, pad in aliases.items()})
    return pins


PARTS: dict[str, Part] = {
    # Thonkiconn-style 3.5mm jack. Stock KiCad lib has no Thonkiconn; the CUI
    # SJ1-3523N has the same logical T/S/R contacts and is a fine stand-in for
    # proving the pipeline. Swap the footprint string to retarget to a real
    # Thonkiconn library later -- nothing else changes.
    "jack_35": Part(
        type="jack_35",
        footprint="Connector_Audio:Jack_3.5mm_CUI_SJ1-3523N_Horizontal",
        pins={"TIP": "T", "SLEEVE": "S", "RING": "R"},
        panel_d=6.0,  # 3.5mm jack threaded bushing clearance
        sim="passthrough",
    ),
    # Alpha 9mm vertical potentiometer (RD901F). Pads 1/2/3 = CW / wiper / CCW.
    "pot_9mm": Part(
        type="pot_9mm",
        footprint="Potentiometer_THT:Potentiometer_Alpha_RD901F-40-00D_Single_Vertical",
        pins={"1": "1", "CW": "1", "2": "2", "WIPER": "2", "3": "3", "CCW": "3"},
        panel_d=7.0,  # 7mm bushing
        sim="potentiometer",
    ),

    # --- discrete passives (board-only) ---
    "resistor": Part(
        type="resistor",
        footprint="Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
        pins={"1": "1", "2": "2", "A": "1", "B": "2"},
        panel_d=0.0,
        sim="resistor",
    ),
    "cap_film": Part(
        type="cap_film",
        footprint="Capacitor_THT:C_Disc_D4.7mm_W2.5mm_P5.00mm",
        pins={"1": "1", "2": "2", "A": "1", "B": "2"},
        panel_d=0.0,
        sim="capacitor",
    ),

    # --- ICs (board-only, SPICE subckt models) ---
    "tl072": Part(
        type="tl072",
        footprint="Package_DIP:DIP-8_W7.62mm",
        pins=_ic_pins(8, {
            "OUT1": 1, "IN1-": 2, "IN1+": 3, "V-": 4,
            "IN2+": 5, "IN2-": 6, "OUT2": 7, "V+": 8,
        }),
        panel_d=0.0,
        sim="ic",
        subckt="tl072",
        npads=8,
    ),
    "lm13700": Part(
        type="lm13700",
        footprint="Package_DIP:DIP-16_W7.62mm",
        pins=_ic_pins(16, {
            "IABC1": 1, "DIODE1": 2, "+IN1": 3, "-IN1": 4, "OUT1": 5,
            "BUFIN1": 6, "BUFOUT1": 7, "V-": 8, "BUFOUT2": 9, "BUFIN2": 10,
            "OUT2": 11, "-IN2": 12, "+IN2": 13, "DIODE2": 14, "IABC2": 15,
            "V+": 16,
        }),
        panel_d=0.0,
        sim="ic",
        subckt="lm13700",
        npads=16,
    ),

    # --- Eurorack power (2x5 boxed header). Simplified rail mapping:
    #     pin 1 = -12V (red stripe), pins 4/5/6 = GND, pin 10 = +12V. ---
    "power_2x5": Part(
        type="power_2x5",
        footprint="Connector_IDC:IDC-Header_2x05_P2.54mm_Latch_Vertical",
        pins=_ic_pins(10, {"V-": 1, "GND": 4, "V+": 10}),
        panel_d=0.0,
        sim="passthrough",
        npads=10,
    ),
}


def get_part(type_: str) -> Part:
    try:
        return PARTS[type_]
    except KeyError:
        raise KeyError(
            f"unknown part type {type_!r}; known types: {sorted(PARTS)}"
        ) from None
