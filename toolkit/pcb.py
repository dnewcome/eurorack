"""PCB leg: shared spec -> pcbnew board -> DRC -> kicad-cli fab export.

Builds a `.kicad_pcb` directly with the KiCad Python API, then runs KiCad's own
design-rule check before exporting fab files. Routing is real, not cosmetic:

  - footprints are placed at each component's panel (x, y)
  - footprint-owned Edge.Cuts (panel-mount slots) and overhanging silk are
    stripped so they don't corrupt the board outline / silk clearance
  - the board outline is sized to the copper, inset-clear of every part
  - GND is a poured copper zone (B.Cu) -- the realistic way to handle ground and
    the thing that kills almost all the track crossings/shorts
  - signal nets are routed on F.Cu by a small grid maze-router that threads the
    gaps between pads and mounting holes
  - the title goes on the front silkscreen

`generate()` fails loud if DRC reports errors, so a board that ships is a board
that passed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import _env  # noqa: F401  (puts pcbnew on the path)
import pcbnew

from .spec import Module

FOOTPRINT_ROOT = Path("/usr/share/kicad/footprints")
TRACK_WIDTH = 0.4       # mm
CLEARANCE = 0.25        # mm copper-copper
EDGE_CLEAR = 0.5        # mm copper-to-edge
BOARD_MARGIN = 2.5      # mm from outermost copper to board edge
GRID = 0.4              # mm maze-router grid
GND_NET = "GND"


# --------------------------------------------------------------------------- #
# units
# --------------------------------------------------------------------------- #
def _mm(v: float) -> int:
    return pcbnew.FromMM(v)


def _vec(x: float, y: float):
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _pos_mm(item) -> tuple[float, float]:
    p = item.GetPosition()
    return pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)


# --------------------------------------------------------------------------- #
# maze router
# --------------------------------------------------------------------------- #
class _Grid:
    """4-connected occupancy grid over the board interior, in mm."""

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0 = x0, y0
        self.nx = int((x1 - x0) / GRID) + 1
        self.ny = int((y1 - y0) / GRID) + 1
        self.blocked = bytearray(self.nx * self.ny)

    def _idx(self, i, j):
        return j * self.nx + i

    def cell(self, x, y):
        return (round((x - self.x0) / GRID), round((y - self.y0) / GRID))

    def xy(self, i, j):
        return (self.x0 + i * GRID, self.y0 + j * GRID)

    def inside(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny

    def block_circle(self, cx, cy, r):
        i0, j0 = self.cell(cx - r, cy - r)
        i1, j1 = self.cell(cx + r, cy + r)
        r2 = r * r
        for j in range(max(0, j0), min(self.ny, j1 + 1)):
            for i in range(max(0, i0), min(self.nx, i1 + 1)):
                x, y = self.xy(i, j)
                if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                    self.blocked[self._idx(i, j)] = 1

    def block_segment(self, x1, y1, x2, y2, r):
        n = max(1, int(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / (GRID / 2)))
        for k in range(n + 1):
            t = k / n
            self.block_circle(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r)

    def route(self, src, dst):
        """BFS from src to dst (mm). Returns simplified polyline or None."""
        si, sj = self.cell(*src)
        di, dj = self.cell(*dst)
        if not (self.inside(si, sj) and self.inside(di, dj)):
            return None
        # endpoints are always free (they sit on their own pads)
        self.blocked[self._idx(si, sj)] = 0
        self.blocked[self._idx(di, dj)] = 0
        start, goal = (si, sj), (di, dj)
        prev = {start: None}
        q = deque([start])
        while q:
            cur = q.popleft()
            if cur == goal:
                break
            ci, cj = cur
            for ni, nj in ((ci + 1, cj), (ci - 1, cj), (ci, cj + 1), (ci, cj - 1)):
                if not self.inside(ni, nj) or (ni, nj) in prev:
                    continue
                if self.blocked[self._idx(ni, nj)]:
                    continue
                prev[(ni, nj)] = cur
                q.append((ni, nj))
        if goal not in prev:
            return None
        # reconstruct + simplify collinear runs
        cells = []
        c = goal
        while c is not None:
            cells.append(c)
            c = prev[c]
        cells.reverse()
        pts = [self.xy(*c) for c in cells]
        pts[0], pts[-1] = src, dst  # snap to exact pad centres
        return _simplify(pts)


def _simplify(pts):
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        ax, ay = out[-1]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        # drop b if a-b-c are collinear (cross product ~0)
        if abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) > 1e-6:
            out.append(pts[i])
    out.append(pts[-1])
    return out


# --------------------------------------------------------------------------- #
# board construction
# --------------------------------------------------------------------------- #
def _load_footprint(fp_id: str):
    lib, _, name = fp_id.partition(":")
    fp = pcbnew.FootprintLoad(str(FOOTPRINT_ROOT / f"{lib}.pretty"), name)
    if fp is None:
        raise RuntimeError(f"could not load footprint {fp_id!r}")
    return fp


def _strip_footprint_graphics(fp):
    """Move footprint-owned Edge.Cuts (panel-mount slots) and silkscreen
    graphics onto a non-fab layer, so they neither corrupt the board outline
    nor overhang a narrow board.

    We re-layer rather than Remove(): Remove() corrupts the global footprint
    plugin state and breaks the next FootprintLoad().
    """
    for g in fp.GraphicalItems():
        if g.GetLayer() in (pcbnew.Edge_Cuts, pcbnew.F_SilkS, pcbnew.B_SilkS):
            g.SetLayer(pcbnew.Dwgs_User)


def _copper_bbox(board):
    """Bounding box (mm) of all pads -- what the outline must clear."""
    x0 = y0 = 1e18
    x1 = y1 = -1e18
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            bb = pad.GetBoundingBox()
            x0 = min(x0, pcbnew.ToMM(bb.GetLeft()))
            y0 = min(y0, pcbnew.ToMM(bb.GetTop()))
            x1 = max(x1, pcbnew.ToMM(bb.GetRight()))
            y1 = max(y1, pcbnew.ToMM(bb.GetBottom()))
    return x0, y0, x1, y1


def _draw_outline(board, x0, y0, x1, y1):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for k in range(4):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_vec(*corners[k]))
        seg.SetEnd(_vec(*corners[(k + 1) % 4]))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(_mm(0.1))
        board.Add(seg)


def _add_gnd_zone(board, net, x0, y0, x1, y1):
    inset = EDGE_CLEAR
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.B_Cu)
    zone.SetNetCode(net.GetNetCode())
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    # a fresh NewBoard() has zero default clearances, so the filler would pour
    # right up to mounting holes -- force a clearance above the DRC rule.
    zone.SetLocalClearance(_mm(CLEARANCE + 0.05))
    pts = pcbnew.VECTOR_VECTOR2I()
    for (x, y) in [(x0 + inset, y0 + inset), (x1 - inset, y0 + inset),
                   (x1 - inset, y1 - inset), (x0 + inset, y1 - inset)]:
        pts.append(_vec(x, y))
    zone.AddPolygon(pts)
    board.Add(zone)
    return zone


def _obstacles(board, keep_netcode):
    """(cx, cy, r) circles for every pad not on keep_netcode, plus all NPTH."""
    obs = []
    # pad edge + copper clearance + our own track half-width + a grid-snap margin
    inflate = CLEARANCE + TRACK_WIDTH / 2 + GRID
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            is_npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
            if not is_npth and pad.GetNetCode() == keep_netcode:
                continue
            bb = pad.GetBoundingBox()
            r = max(pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())) / 2
            cx, cy = _pos_mm(pad)
            obs.append((cx, cy, r + inflate))
    return obs


def _route_all(board, mod, x0, y0, x1, y1):
    """Maze-route every net on F.Cu. Returns nets that failed.

    GND is routed last so it threads around the already-laid signal tracks
    rather than the other way round.
    """
    failed = []
    # grid bounds inset so copper stays clear of the edge
    gx0, gy0 = x0 + EDGE_CLEAR + TRACK_WIDTH / 2, y0 + EDGE_CLEAR + TRACK_WIDTH / 2
    gx1, gy1 = x1 - EDGE_CLEAR - TRACK_WIDTH / 2, y1 - EDGE_CLEAR - TRACK_WIDTH / 2

    nets = sorted(mod.nets, key=lambda n: n.name == GND_NET)
    routed_segments = []  # (x1,y1,x2,y2) of already-laid tracks (other nets)
    for net in nets:
        ni = board.FindNet(net.name)
        grid = _Grid(gx0, gy0, gx1, gy1)
        for (cx, cy, r) in _obstacles(board, ni.GetNetCode()):
            grid.block_circle(cx, cy, r)
        for (ax, ay, bx, by) in routed_segments:
            # other track half-width + clearance + our half-width + grid margin
            grid.block_segment(ax, ay, bx, by,
                               TRACK_WIDTH + CLEARANCE + GRID)

        pads = [board.FindFootprintByReference(p.ref)
                .FindPadByNumber(mod.pad_number(p)) for p in net.pins]
        anchor = pads[0]
        ax, ay = _pos_mm(anchor)
        for pad in pads[1:]:
            bx, by = _pos_mm(pad)
            path = grid.route((ax, ay), (bx, by))
            if path is None:
                failed.append(net.name)
                continue
            for k in range(len(path) - 1):
                (sx, sy), (ex, ey) = path[k], path[k + 1]
                trk = pcbnew.PCB_TRACK(board)
                trk.SetStart(_vec(sx, sy))
                trk.SetEnd(_vec(ex, ey))
                trk.SetWidth(_mm(TRACK_WIDTH))
                trk.SetLayer(pcbnew.F_Cu)
                trk.SetNet(ni)
                board.Add(trk)
                routed_segments.append((sx, sy, ex, ey))
    return failed


def build_board(mod: Module, out_path: Path) -> Path:
    board = pcbnew.NewBoard(str(out_path))

    # design rules to match what we route to
    # a fresh NewBoard() has zero clearances, so the zone filler would pour up
    # to holes/edges; set them above the DRC rules we check against.
    settings = board.GetDesignSettings()
    settings.m_CopperEdgeClearance = _mm(EDGE_CLEAR)
    settings.m_HoleClearance = _mm(CLEARANCE + 0.05)
    settings.m_MinClearance = _mm(CLEARANCE)

    # nets
    nets = {}
    for net in mod.nets:
        ni = pcbnew.NETINFO_ITEM(board, net.name)
        board.Add(ni)
        nets[net.name] = ni

    # footprints
    for c in mod.components:
        fp = _load_footprint(c.part.footprint)
        fp.SetReference(c.ref)
        fp.SetValue(c.value or c.type)
        # mutate before Add (the board takes ownership) and hide fields before
        # Remove()-ing graphics (Remove() unwraps later SWIG accessors).
        for field in fp.GetFields():  # panel carries the labelling
            field.SetVisible(False)
        _strip_footprint_graphics(fp)
        board.Add(fp)
        fp.SetPosition(_vec(c.x, c.y))

    # pad -> net
    for net in mod.nets:
        ni = nets[net.name]
        for pin in net.pins:
            pad = board.FindFootprintByReference(pin.ref).FindPadByNumber(
                mod.pad_number(pin))
            if pad is None:
                raise RuntimeError(f"{pin}: no pad {mod.pad_number(pin)!r}")
            pad.SetNet(ni)

    # outline sized to copper + margin
    cx0, cy0, cx1, cy1 = _copper_bbox(board)
    x0, y0 = cx0 - BOARD_MARGIN, cy0 - BOARD_MARGIN
    x1, y1 = cx1 + BOARD_MARGIN, cy1 + BOARD_MARGIN
    _draw_outline(board, x0, y0, x1, y1)

    # route every net (GND included) on F.Cu
    failed = _route_all(board, mod, x0, y0, x1, y1)
    if failed:
        raise RuntimeError(f"router failed to connect nets: {failed}")

    # silkscreen title
    text = pcbnew.PCB_TEXT(board)
    text.SetText(mod.title)
    text.SetPosition(_vec((x0 + x1) / 2, y0 + 1.5))
    text.SetLayer(pcbnew.F_SilkS)
    text.SetTextSize(pcbnew.VECTOR2I(_mm(1.5), _mm(1.5)))
    text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
    board.Add(text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(out_path), board)
    return out_path


# --------------------------------------------------------------------------- #
# DRC + fab export
# --------------------------------------------------------------------------- #
def _cli(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("kicad-cli") is None:
        raise RuntimeError("kicad-cli not found on PATH")
    return subprocess.run(["kicad-cli", *args], capture_output=True, text=True)


@dataclass
class DRCResult:
    errors: int
    warnings: int
    by_type: dict[str, int]
    report: Path

    @property
    def clean(self) -> bool:
        return self.errors == 0


def run_drc(board_path: Path, report_path: Path) -> DRCResult:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _cli("pcb", "drc", "--format", "json", "--severity-all",
                "--output", str(report_path), str(board_path))
    if not report_path.exists():
        raise RuntimeError(f"DRC produced no report:\n{proc.stdout}\n{proc.stderr}")
    d = json.loads(report_path.read_text())
    items = (d.get("violations", []) + d.get("unconnected_items", [])
             + d.get("schematic_parity", []))
    errors = sum(1 for v in items if v.get("severity") == "error")
    warnings = sum(1 for v in items if v.get("severity") == "warning")
    by_type: dict[str, int] = {}
    for v in items:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    return DRCResult(errors, warnings, by_type, report_path)


def export_fab(board_path: Path, out_dir: Path) -> dict[str, Path]:
    gerber_dir = out_dir / "gerbers"
    gerber_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{board_path.stem}.svg"
    _cli("pcb", "export", "gerbers", "--output", str(gerber_dir), str(board_path))
    _cli("pcb", "export", "drill", "--output", str(gerber_dir) + "/", str(board_path))
    _cli("pcb", "export", "svg", "--output", str(svg_path),
         "--layers", "F.Cu,B.Cu,F.SilkS,Edge.Cuts", "--page-size-mode", "2",
         str(board_path))
    return {"gerbers": gerber_dir, "svg": svg_path}


def generate(mod: Module, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    board_path = out_dir / f"{mod.name}.kicad_pcb"
    build_board(mod, board_path)

    drc = run_drc(board_path, out_dir / "drc.json")
    if not drc.clean:
        raise RuntimeError(
            f"DRC failed: {drc.errors} error(s) {dict(drc.by_type)} "
            f"-- see {drc.report}"
        )

    artifacts = export_fab(board_path, out_dir)
    artifacts["board"] = board_path
    artifacts["drc"] = drc
    return artifacts


if __name__ == "__main__":
    import sys
    from . import spec
    m = spec.load(sys.argv[1] if len(sys.argv) > 1
                  else "modules/attenuator/module.toml")
    arts = generate(m, Path("build") / m.name / "pcb")
    drc = arts.pop("drc")
    for k, v in arts.items():
        print(f"{k}: {v}")
    print(f"DRC: {drc.errors} errors, {drc.warnings} warnings {dict(drc.by_type)}")
