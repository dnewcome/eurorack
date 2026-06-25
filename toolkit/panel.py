"""Mechanical leg: shared spec -> build123d faceplate + PCB mount.

Generates, from the same module spec the sim and PCB legs use:
  - panel.stl   : 3D solid faceplate (for visualisation / 3D printing)
  - panel.dxf   : 2D cut profile (outline + cutouts + rail slots) for laser
  - panel.svg   : 2D preview of the cut profile
  - silk.svg    : silkscreen layer (title + component labels) for printing
  - standoff.stl: parametric M3 PCB standoff (the reusable mount primitive)

Spec coords are top-left origin, +y down. build123d sketches are centred with
+y up, so positions are converted via `_to_sketch`.
"""
from __future__ import annotations

from pathlib import Path

from build123d import (
    Align, Axis, BuildPart, BuildSketch, Circle, Cylinder, Locations, Mode,
    Plane, Rectangle, SlotOverall, Text, Unit, export_stl, extrude,
)
from build123d.exporters import ExportDXF, ExportSVG

PANEL_THICKNESS = 2.0       # mm (typical aluminium/acrylic Eurorack panel)
RAIL_INSET_Y = 3.0          # mounting slot centre from top/bottom edge
RAIL_INSET_X = 7.5          # mounting slot centre from left/right edge
SLOT_LEN = 5.5              # horizontal slot length (M3 + adjustment)
SLOT_W = 3.2               # M3 clearance
STANDOFF_OD = 6.0
STANDOFF_BORE = 3.2         # M3 clearance
STANDOFF_HEIGHT = 11.0      # panel-to-PCB offset


def _to_sketch(mod, x: float, y: float) -> tuple[float, float]:
    """Spec (top-left, +y down) -> sketch (centred, +y up)."""
    return (x - mod.panel_w / 2, mod.panel_h / 2 - y)


def _mount_slots(mod) -> list[tuple[float, float]]:
    """Diagonal 2-slot pattern (top-left + bottom-right), Doepfer style for
    narrow panels; centres in spec coords."""
    ix = min(RAIL_INSET_X, mod.panel_w / 2)
    return [
        (ix, RAIL_INSET_Y),
        (mod.panel_w - ix, mod.panel_h - RAIL_INSET_Y),
    ]


def _cut_profile(mod):
    """2D sketch of everything that gets cut: outline, component cutouts,
    mounting slots."""
    with BuildSketch() as sk:
        Rectangle(mod.panel_w, mod.panel_h)
        # component cutouts
        for c in mod.components:
            if c.part.panel_d <= 0:      # board-only part, no panel cutout
                continue
            with Locations(_to_sketch(mod, c.x, c.y)):
                Circle(c.part.panel_d / 2, mode=Mode.SUBTRACT)
        # mounting slots
        for (sx, sy) in _mount_slots(mod):
            with Locations(_to_sketch(mod, sx, sy)):
                SlotOverall(SLOT_LEN, SLOT_W, mode=Mode.SUBTRACT)
    return sk.sketch


def build_panel(mod):
    with BuildPart() as panel:
        with BuildSketch():
            Rectangle(mod.panel_w, mod.panel_h)
            for c in mod.components:
                if c.part.panel_d <= 0:      # board-only part, no panel cutout
                    continue
                with Locations(_to_sketch(mod, c.x, c.y)):
                    Circle(c.part.panel_d / 2, mode=Mode.SUBTRACT)
            for (sx, sy) in _mount_slots(mod):
                with Locations(_to_sketch(mod, sx, sy)):
                    SlotOverall(SLOT_LEN, SLOT_W, mode=Mode.SUBTRACT)
        extrude(amount=PANEL_THICKNESS)
    return panel.part


def build_standoff():
    with BuildPart() as so:
        Cylinder(STANDOFF_OD / 2, STANDOFF_HEIGHT,
                 align=(Align.CENTER, Align.CENTER, Align.MIN))
        Cylinder(STANDOFF_BORE / 2, STANDOFF_HEIGHT,
                 align=(Align.CENTER, Align.CENTER, Align.MIN),
                 mode=Mode.SUBTRACT)
    return so.part


def _silk_sketch(mod):
    """Silkscreen labels: title at top, component ref under each cutout."""
    shapes = []
    with BuildSketch() as title:
        with Locations(_to_sketch(mod, mod.panel_w / 2, 8)):
            Text(mod.title, font_size=4.0)
    shapes.append(title.sketch)
    for c in mod.components:
        if c.part.panel_d <= 0:      # board-only part, no panel label
            continue
        with BuildSketch() as lbl:
            lx, ly = _to_sketch(mod, c.x, c.y + c.part.panel_d / 2 + 3.5)
            with Locations((lx, ly)):
                Text(c.ref, font_size=2.5)
        shapes.append(lbl.sketch)
    return shapes


def generate(mod, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # 3D solid panel
    panel = build_panel(mod)
    p = out_dir / "panel.stl"
    export_stl(panel, str(p))
    paths["panel_stl"] = p

    # 2D cut profile -> DXF + SVG
    profile = _cut_profile(mod)
    dxf = ExportDXF(unit=Unit.MM)
    dxf.add_layer("cut")
    dxf.add_shape(profile, layer="cut")
    p = out_dir / "panel.dxf"
    dxf.write(str(p))
    paths["panel_dxf"] = p

    svg = ExportSVG(unit=Unit.MM)
    svg.add_layer("cut")
    svg.add_shape(profile, layer="cut")
    p = out_dir / "panel.svg"
    svg.write(str(p))
    paths["panel_svg"] = p

    # silkscreen layer
    silk = ExportSVG(unit=Unit.MM)
    silk.add_layer("silk")
    for s in _silk_sketch(mod):
        silk.add_shape(s, layer="silk")
    p = out_dir / "silk.svg"
    silk.write(str(p))
    paths["silk_svg"] = p

    # PCB mount primitive
    so = build_standoff()
    p = out_dir / "standoff.stl"
    export_stl(so, str(p))
    paths["standoff_stl"] = p

    return paths


if __name__ == "__main__":
    import sys
    from . import spec
    m = spec.load(sys.argv[1] if len(sys.argv) > 1
                  else "modules/attenuator/module.toml")
    for k, v in generate(m, Path("build") / m.name / "panel").items():
        print(f"{k}: {v}")
