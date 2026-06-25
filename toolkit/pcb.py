"""PCB leg: shared spec -> pcbnew board -> DRC -> kicad-cli fab export.

Builds a `.kicad_pcb` with the KiCad Python API and a small 2-layer autorouter,
then gates fab export on KiCad's own DRC.

Approach:
  - place footprints at each component's (x, y); re-layer footprint-owned
    Edge.Cuts / overhanging silk to Dwgs.User; hide fields (panel labels parts)
  - size the board outline to the copper, clear of every part
  - GND is two poured copper zones (F.Cu + B.Cu); NPTH mounting holes get
    keepout rule-areas so the filler clears them (the filler won't on its own)
  - every other net is routed by a Dijkstra maze-router over a 2-layer grid;
    nets cross by switching layers through vias
  - export only if DRC is clean (generate() raises otherwise)
"""
from __future__ import annotations

import heapq
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import _env  # noqa: F401  (puts pcbnew on the path)
import pcbnew

from .spec import Module

FOOTPRINT_ROOT = Path("/usr/share/kicad/footprints")
TRACK_WIDTH = 0.3       # mm
CLEARANCE = 0.2         # mm copper-copper
EDGE_CLEAR = 0.5        # mm copper-to-edge
BOARD_MARGIN = 3.0      # mm copper bbox -> board edge
GRID = 0.3              # mm maze-router grid
VIA_DIA = 0.8
VIA_DRILL = 0.4
VIA_COST = 12           # grid-steps penalty per layer change
GND_NET = "GND"
LAYERS = (pcbnew.F_Cu, pcbnew.B_Cu)   # index 0 = front, 1 = back


def _mm(v: float) -> int:
    return pcbnew.FromMM(v)


def _vec(x: float, y: float):
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _pos_mm(item) -> tuple[float, float]:
    p = item.GetPosition()
    return pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)


# --------------------------------------------------------------------------- #
# 2-layer maze router
# --------------------------------------------------------------------------- #
class _Grid:
    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0 = x0, y0
        self.nx = int((x1 - x0) / GRID) + 1
        self.ny = int((y1 - y0) / GRID) + 1
        self.blk = [bytearray(self.nx * self.ny), bytearray(self.nx * self.ny)]

    def _i(self, i, j):
        return j * self.nx + i

    def cell(self, x, y):
        return (round((x - self.x0) / GRID), round((y - self.y0) / GRID))

    def xy(self, i, j):
        return (self.x0 + i * GRID, self.y0 + j * GRID)

    def inside(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny

    def block_circle(self, cx, cy, r, layer=None):
        layers = (0, 1) if layer is None else (layer,)
        i0, j0 = self.cell(cx - r, cy - r)
        i1, j1 = self.cell(cx + r, cy + r)
        r2 = r * r
        for j in range(max(0, j0), min(self.ny, j1 + 1)):
            for i in range(max(0, i0), min(self.nx, i1 + 1)):
                x, y = self.xy(i, j)
                if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                    for L in layers:
                        self.blk[L][self._i(i, j)] = 1

    def block_segment(self, x1, y1, x2, y2, r, layer):
        n = max(1, int(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / (GRID / 2)))
        for k in range(n + 1):
            t = k / n
            self.block_circle(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r, layer)

    def route(self, src, dst):
        """Dijkstra over (i, j, layer). Returns [(x, y, layer), ...] or None.
        A layer change at the same (i, j) marks a via."""
        si, sj = self.cell(*src)
        di, dj = self.cell(*dst)
        if not (self.inside(si, sj) and self.inside(di, dj)):
            return None
        # endpoints sit on through-hole pads: free on both layers
        for L in (0, 1):
            self.blk[L][self._i(si, sj)] = 0
            self.blk[L][self._i(di, dj)] = 0

        dist = {}
        prev = {}
        pq = [(0, si, sj, 0), (0, si, sj, 1)]
        for s in ((si, sj, 0), (si, sj, 1)):
            dist[s] = 0
            prev[s] = None
        goal = None
        while pq:
            d, i, j, L = heapq.heappop(pq)
            if d > dist.get((i, j, L), 1e18):
                continue
            if (i, j) == (di, dj):
                goal = (i, j, L)
                break
            # in-plane
            for ni, nj in ((i+1, j), (i-1, j), (i, j+1), (i, j-1)):
                if not self.inside(ni, nj) or self.blk[L][self._i(ni, nj)]:
                    continue
                nd = d + 1
                if nd < dist.get((ni, nj, L), 1e18):
                    dist[(ni, nj, L)] = nd
                    prev[(ni, nj, L)] = (i, j, L)
                    heapq.heappush(pq, (nd, ni, nj, L))
            # via to other layer
            oL = 1 - L
            if not self.blk[oL][self._i(i, j)]:
                nd = d + VIA_COST
                if nd < dist.get((i, j, oL), 1e18):
                    dist[(i, j, oL)] = nd
                    prev[(i, j, oL)] = (i, j, L)
                    heapq.heappush(pq, (nd, i, j, oL))
        if goal is None:
            return None
        path = []
        c = goal
        while c is not None:
            path.append(c)
            c = prev[c]
        path.reverse()
        pts = [(*self.xy(i, j), L) for (i, j, L) in path]
        pts[0] = (src[0], src[1], pts[0][2])
        pts[-1] = (dst[0], dst[1], pts[-1][2])
        return _simplify(pts)


def _simplify(pts):
    """Drop collinear interior points that stay on the same layer."""
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    for k in range(1, len(pts) - 1):
        ax, ay, aL = out[-1]
        bx, by, bL = pts[k]
        cx, cy, cL = pts[k + 1]
        if aL == bL == cL and abs((bx-ax)*(cy-ay) - (by-ay)*(cx-ax)) < 1e-6:
            continue
        out.append(pts[k])
    out.append(pts[-1])
    return out


# --------------------------------------------------------------------------- #
# footprints
# --------------------------------------------------------------------------- #
def _load_footprint(fp_id: str):
    lib, _, name = fp_id.partition(":")
    fp = pcbnew.FootprintLoad(str(FOOTPRINT_ROOT / f"{lib}.pretty"), name)
    if fp is None:
        raise RuntimeError(f"could not load footprint {fp_id!r}")
    return fp


def _strip_footprint_graphics(fp):
    """Re-layer footprint Edge.Cuts/silk to Dwgs.User (Remove() corrupts the
    footprint plugin state and breaks the next FootprintLoad)."""
    for g in fp.GraphicalItems():
        if g.GetLayer() in (pcbnew.Edge_Cuts, pcbnew.F_SilkS, pcbnew.B_SilkS):
            g.SetLayer(pcbnew.Dwgs_User)


def _copper_bbox(board):
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


# --------------------------------------------------------------------------- #
# copper pour + NPTH keepouts
# --------------------------------------------------------------------------- #
def _poly(board, pts):
    v = pcbnew.VECTOR_VECTOR2I()
    for (x, y) in pts:
        v.append(_vec(x, y))
    return v


def _add_pour(board, net, layer, x0, y0, x1, y1):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNetCode(net.GetNetCode())
    z.SetLocalClearance(_mm(CLEARANCE + 0.05))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    i = EDGE_CLEAR
    z.AddPolygon(_poly(board, [(x0+i, y0+i), (x1-i, y0+i),
                               (x1-i, y1-i), (x0+i, y1-i)]))
    board.Add(z)


def _add_npth_keepouts(board):
    """Circular no-pour rule-areas around mounting holes; the zone filler
    won't clear bare NPTH holes by itself."""
    import math
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    ls.AddLayer(pcbnew.B_Cu)
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
                continue
            cx, cy = _pos_mm(pad)
            r = pcbnew.ToMM(pad.GetDrillSizeX()) / 2 + CLEARANCE + 0.1
            ka = pcbnew.ZONE(board)
            ka.SetIsRuleArea(True)
            ka.SetDoNotAllowCopperPour(True)   # block the pour only...
            ka.SetDoNotAllowTracks(False)      # ...not the pads/tracks/vias
            ka.SetDoNotAllowVias(False)
            ka.SetDoNotAllowPads(False)
            ka.SetDoNotAllowFootprints(False)
            ka.SetLayerSet(ls)
            pts = [(cx + r*math.cos(a), cy + r*math.sin(a))
                   for a in [i*math.pi/8 for i in range(16)]]
            ka.AddPolygon(_poly(board, pts))
            board.Add(ka)


# --------------------------------------------------------------------------- #
# net routing
# --------------------------------------------------------------------------- #
def _obstacle_circles(board, keep_netcode):
    """Pads not on keep_netcode (+ all NPTH) as (cx, cy, r)."""
    obs = []
    inflate = CLEARANCE + TRACK_WIDTH / 2 + 0.15
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
            if not npth and pad.GetNetCode() == keep_netcode:
                continue
            bb = pad.GetBoundingBox()
            r = max(pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())) / 2
            cx, cy = _pos_mm(pad)
            obs.append((cx, cy, r + inflate))
    return obs


def _route_signals(board, mod, x0, y0, x1, y1):
    """Route every non-GND net on 2 layers with vias. Returns failed nets."""
    gx0 = x0 + EDGE_CLEAR + TRACK_WIDTH / 2
    gy0 = y0 + EDGE_CLEAR + TRACK_WIDTH / 2
    gx1 = x1 - EDGE_CLEAR - TRACK_WIDTH / 2
    gy1 = y1 - EDGE_CLEAR - TRACK_WIDTH / 2

    tracks = []   # (netcode, layer, x1, y1, x2, y2)
    vias = []     # (netcode, x, y)
    failed = []
    # route higher-fanout nets first -- they're hardest to fit and otherwise
    # get boxed in by the easy two-pin nets
    nets = [n for n in mod.nets if n.name != GND_NET]
    nets.sort(key=lambda n: -len(n.pins))

    for net in nets:
        ni = board.FindNet(net.name)
        nc = ni.GetNetCode()
        grid = _Grid(gx0, gy0, gx1, gy1)
        for (cx, cy, r) in _obstacle_circles(board, nc):
            grid.block_circle(cx, cy, r)
        for (t_nc, L, ax, ay, bx, by) in tracks:
            if t_nc != nc:
                grid.block_segment(ax, ay, bx, by,
                                   TRACK_WIDTH + CLEARANCE + 0.15, L)
        for (v_nc, vx, vy) in vias:
            if v_nc != nc:
                grid.block_circle(vx, vy, VIA_DIA / 2 + CLEARANCE + 0.15)

        pads = [board.FindFootprintByReference(p.ref)
                .FindPadByNumber(mod.pad_number(p)) for p in net.pins]
        anchor = _pos_mm(pads[0])
        ok = True
        for pad in pads[1:]:
            path = grid.route(anchor, _pos_mm(pad))
            if path is None:
                ok = False
                break
            for k in range(len(path) - 1):
                ax, ay, aL = path[k]
                bx, by, bL = path[k + 1]
                if aL != bL:                      # via
                    via = pcbnew.PCB_VIA(board)
                    via.SetViaType(pcbnew.VIATYPE_THROUGH)
                    via.SetPosition(_vec(ax, ay))
                    via.SetDrill(_mm(VIA_DRILL))
                    via.SetWidth(_mm(VIA_DIA))
                    via.SetNet(ni)
                    board.Add(via)
                    vias.append((nc, ax, ay))
                    grid.block_circle(ax, ay, VIA_DIA/2 + CLEARANCE + 0.15)
                else:                              # track
                    trk = pcbnew.PCB_TRACK(board)
                    trk.SetStart(_vec(ax, ay))
                    trk.SetEnd(_vec(bx, by))
                    trk.SetWidth(_mm(TRACK_WIDTH))
                    trk.SetLayer(LAYERS[aL])
                    trk.SetNet(ni)
                    board.Add(trk)
                    tracks.append((nc, aL, ax, ay, bx, by))
                    grid.block_segment(ax, ay, bx, by,
                                       TRACK_WIDTH + CLEARANCE + 0.15, aL)
        if not ok:
            failed.append(net.name)
    return failed


def build_board(mod: Module, out_path: Path) -> Path:
    board = pcbnew.NewBoard(str(out_path))
    ds = board.GetDesignSettings()
    ds.m_CopperEdgeClearance = _mm(EDGE_CLEAR)
    ds.m_HoleClearance = _mm(CLEARANCE + 0.05)
    ds.m_MinClearance = _mm(CLEARANCE)

    nets = {}
    for net in mod.nets:
        ni = pcbnew.NETINFO_ITEM(board, net.name)
        board.Add(ni)
        nets[net.name] = ni

    for c in mod.components:
        fp = _load_footprint(c.part.footprint)
        fp.SetReference(c.ref)
        fp.SetValue(c.value or c.type)
        for field in fp.GetFields():
            field.SetVisible(False)
        _strip_footprint_graphics(fp)
        board.Add(fp)
        fp.SetPosition(_vec(c.x, c.y))
        if c.rotation:
            fp.SetOrientationDegrees(c.rotation)

    for net in mod.nets:
        ni = nets[net.name]
        for pin in net.pins:
            pad = board.FindFootprintByReference(pin.ref).FindPadByNumber(
                mod.pad_number(pin))
            if pad is None:
                raise RuntimeError(f"{pin}: no pad {mod.pad_number(pin)!r}")
            pad.SetNet(ni)

    cx0, cy0, cx1, cy1 = _copper_bbox(board)
    x0, y0 = cx0 - BOARD_MARGIN, cy0 - BOARD_MARGIN
    x1, y1 = cx1 + BOARD_MARGIN, cy1 + BOARD_MARGIN
    _draw_outline(board, x0, y0, x1, y1)

    failed = _route_signals(board, mod, x0, y0, x1, y1)
    if failed:
        raise RuntimeError(f"router failed to connect nets: {failed}")

    # GND as two poured planes; keepouts clear the mounting holes
    if GND_NET in nets:
        _add_npth_keepouts(board)
        _add_pour(board, nets[GND_NET], pcbnew.F_Cu, x0, y0, x1, y1)
        _add_pour(board, nets[GND_NET], pcbnew.B_Cu, x0, y0, x1, y1)
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

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
            f"-- see {drc.report}")
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
