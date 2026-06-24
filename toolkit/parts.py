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
    panel_d: float
    sim: str


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
}


def get_part(type_: str) -> Part:
    try:
        return PARTS[type_]
    except KeyError:
        raise KeyError(
            f"unknown part type {type_!r}; known types: {sorted(PARTS)}"
        ) from None
