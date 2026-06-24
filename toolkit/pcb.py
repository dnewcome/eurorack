"""PCB leg: shared spec -> pcbnew board -> kicad-cli fab export.

Builds a `.kicad_pcb` directly with the KiCad Python API:
  - places each component's footprint at its panel (x, y)
  - creates a net per spec net and assigns pads
  - draws the board outline (Edge.Cuts) from the panel size
  - lays a simple straight track per net (star from the first pad)
  - adds the module title to the front silkscreen

then shells out to `kicad-cli` to emit Gerbers, drill files, and an SVG preview.

The board outline matches the panel so footprints line up with faceplate
cutouts; both come from the same (x, y) in the spec.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import _env  # noqa: F401  (puts pcbnew on the path)
import pcbnew

from .spec import Module, PinRef

FOOTPRINT_ROOT = Path("/usr/share/kicad/footprints")
TRACK_WIDTH_MM = 0.4


def _mm(v: float) -> int:
    return pcbnew.FromMM(v)


def _vec(x: float, y: float):
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _load_footprint(board, fp_id: str):
    lib, _, name = fp_id.partition(":")
    lib_dir = FOOTPRINT_ROOT / f"{lib}.pretty"
    fp = pcbnew.FootprintLoad(str(lib_dir), name)
    if fp is None:
        raise RuntimeError(f"could not load footprint {fp_id!r} from {lib_dir}")
    return fp


def build_board(mod: Module, out_path: Path) -> Path:
    board = pcbnew.NewBoard(str(out_path))

    # --- nets ---------------------------------------------------------------
    nets = {}
    for net in mod.nets:
        ni = pcbnew.NETINFO_ITEM(board, net.name)
        board.Add(ni)
        nets[net.name] = ni

    # --- footprints ---------------------------------------------------------
    placed = {}
    for c in mod.components:
        fp = _load_footprint(board, c.part.footprint)
        fp.SetReference(c.ref)
        fp.SetValue(c.value or c.type)
        board.Add(fp)
        fp.SetPosition(_vec(c.x, c.y))
        placed[c.ref] = fp

    # --- assign pads to nets ------------------------------------------------
    for net in mod.nets:
        ni = nets[net.name]
        for pin in net.pins:
            fp = placed[pin.ref]
            pad_num = mod.pad_number(pin)
            pad = fp.FindPadByNumber(pad_num)
            if pad is None:
                raise RuntimeError(
                    f"{pin.ref}: footprint has no pad {pad_num!r}"
                )
            pad.SetNet(ni)

    # --- board outline (Edge.Cuts) -----------------------------------------
    rect = pcbnew.PCB_SHAPE(board)
    rect.SetShape(pcbnew.SHAPE_T_RECT)
    rect.SetStart(_vec(0, 0))
    rect.SetEnd(_vec(mod.panel_w, mod.panel_h))
    rect.SetLayer(pcbnew.Edge_Cuts)
    rect.SetWidth(_mm(0.15))
    board.Add(rect)

    # --- simple routing: star track per net --------------------------------
    for net in mod.nets:
        ni = nets[net.name]
        pads = []
        for pin in net.pins:
            pad = placed[pin.ref].FindPadByNumber(mod.pad_number(pin))
            pads.append(pad)
        anchor = pads[0]
        for pad in pads[1:]:
            trk = pcbnew.PCB_TRACK(board)
            trk.SetStart(anchor.GetPosition())
            trk.SetEnd(pad.GetPosition())
            trk.SetWidth(_mm(TRACK_WIDTH_MM))
            trk.SetLayer(pcbnew.F_Cu)
            trk.SetNet(ni)
            board.Add(trk)

    # --- silkscreen title ---------------------------------------------------
    text = pcbnew.PCB_TEXT(board)
    text.SetText(mod.title)
    text.SetPosition(_vec(mod.panel_w / 2, 8))
    text.SetLayer(pcbnew.F_SilkS)
    text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
    board.Add(text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(out_path), board)
    return out_path


def _cli(*args: str) -> None:
    if shutil.which("kicad-cli") is None:
        raise RuntimeError("kicad-cli not found on PATH")
    proc = subprocess.run(["kicad-cli", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"kicad-cli {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
        )


def export_fab(board_path: Path, out_dir: Path) -> dict[str, Path]:
    """Emit Gerbers, drill files, and an SVG preview next to the board."""
    gerber_dir = out_dir / "gerbers"
    gerber_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{board_path.stem}.svg"

    _cli("pcb", "export", "gerbers", "--output", str(gerber_dir), str(board_path))
    _cli("pcb", "export", "drill", "--output", str(gerber_dir) + "/",
         str(board_path))
    _cli("pcb", "export", "svg", "--output", str(svg_path),
         "--layers", "F.Cu,B.Cu,F.SilkS,Edge.Cuts",
         "--page-size-mode", "2",   # crop to board
         str(board_path))
    return {"gerbers": gerber_dir, "svg": svg_path}


def generate(mod: Module, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    board_path = out_dir / f"{mod.name}.kicad_pcb"
    build_board(mod, board_path)
    artifacts = export_fab(board_path, out_dir)
    artifacts["board"] = board_path
    return artifacts


if __name__ == "__main__":
    import sys
    from . import spec
    m = spec.load(sys.argv[1] if len(sys.argv) > 1
                  else "modules/attenuator/module.toml")
    arts = generate(m, Path("build") / m.name / "pcb")
    for k, v in arts.items():
        print(f"{k}: {v}")
