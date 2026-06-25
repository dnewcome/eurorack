# Notes on getting autorouting to work

Autorouting is the hardest part of this toolchain, and it's worth writing down
*why* and *what actually helps*, because the failure modes are non-obvious and
the same lessons keep recurring. These notes are grounded in what we hit
building `toolkit/pcb.py`, not generic theory.

## Why it's hard

- **It's NP-hard, and coupled to placement.** Whether a board *can* be routed is
  mostly decided before the router runs — by where the parts sit. A great router
  on a bad placement fails; a mediocre router on a good placement sails through.
  If routing is failing, suspect placement first.
- **Greedy routing paints itself into corners.** Route nets one at a time and the
  early nets grab the easy channels; the last few high-fanout nets find every
  path walled off. Order helps a little, never enough.
- **Clearance is geometric, not topological.** "Don't share a wire" is easy;
  "stay 0.2 mm from every other copper feature including diagonally" is what
  actually trips DRC. A cell-based router has to bake clearance into how it
  models obstacles, or it produces boards that look routed but fail DRC.

## What actually helped (hard-won)

1. **Placement dominates.** The VCO only became routable once the controls moved
   to a column on one edge and the active circuit got a single open rectangle,
   instead of being fragmented into short bands between centered jacks. Cluster
   each net's parts; keep high-fanout parts (ICs) central with room around them;
   leave channels.

2. **Model obstacles as inflated circles, and get the radius right.** Around each
   foreign pad/track, block a radius of
   `other_copper_half + clearance + own_track_half + grid_snap_margin`.
   - Too small → parallel tracks end up *under* the clearance rule. The bug that
     cost us: the track-vs-track radius omitted the routed track's **own**
     half-width, so traces landed 0.17 mm apart instead of 0.25 mm.
   - Too big → it blocks the channels *between* close pads (e.g. 2.54 mm DIP
     pins), and nets that should route can't.

3. **Grid resolution is a real tradeoff.** Finer grid = more channels (you can
   thread between pins) but quadratically slower. Coarser = fast but misses tight
   gaps. We run 0.3 mm. Escape routing from inner DIP pins works because pads
   escape *inward* toward the open inter-row gap, not by squeezing between
   same-row neighbours.

4. **Make GND a pour, not a net to route.** Ground is the highest-fanout net on
   almost every board; routing it as tracks is misery. Pour it as a plane on one
   or both layers and the router only has to deal with signals + power. Caveat we
   hit: KiCad's scripted zone filler **won't clear bare NPTH mounting holes** —
   add circular keepout rule-areas around them (allowing pads/tracks/vias, only
   disallowing the pour) or DRC fails with hole-clearance errors.

5. **Two layers + vias, with a via cost.** A second layer is what lets nets
   cross. Model the grid as `(i, j, layer)`; a layer change at the same cell is a
   via. Give vias a cost (we use ~12 grid-steps) so the router doesn't thrash
   between layers; it'll only via when it genuinely needs to cross.

6. **Set clearances on the board explicitly.** A fresh `pcbnew.NewBoard()` has
   *zero* default clearances, so the zone filler pours up to holes and edges. Set
   `m_MinClearance`, `m_HoleClearance`, `m_CopperEdgeClearance` before filling.

7. **DRC is ground truth.** The router's internal model can be subtly wrong; the
   only thing that matters is `kicad-cli pcb drc`. Gate export on it. A board
   that ships is a board that passed.

## The escalation ladder

Try these in order; each is more work than the last. Stop when the board is
clean.

1. **Greedy, single layer.** Fine for trivial boards (the attenuator). Fails the
   moment two nets need to cross.
2. **Greedy, two layers + vias.** Handles moderate boards. Route high-fanout
   nets first so they aren't boxed in. This is where naive routing tops out —
   the dense VCO leaves 3–5 nets unroutable.
3. **Rip-up & reroute** *(what we're building)*. When a net can't route, rip up
   the routed nets that are in its way, route it, then re-route the ripped ones.
   Bound the number of rip-up rounds and per-net rip count so it can't loop
   forever. Simple, and resolves most of what greedy can't.
4. **Negotiated congestion (PathFinder).** The gold standard. Let nets route
   *through* each other at a cost; each iteration, re-route every net while
   raising the cost of over-used cells (present congestion + accumulated
   history). Contention dissolves over iterations. More code and slower, but
   converges on boards rip-up can't. Worth it only if rip-up stalls.
5. **Hand off to a real router.** `freerouting` (the open-source Java router)
   reads the Specctra DSN that KiCad can export and routes industrial-grade
   boards. If our router can't do it, export and let freerouting finish — no
   shame in it.

## Implementation tactics that matter

- **A\*, not plain Dijkstra**, for point-to-point: a Manhattan-distance heuristic
  (admissible while min step-cost ≥ 1) explores a corridor instead of a disc —
  big speedup, which matters once you're re-routing nets many times.
- **Multi-pin nets are trees, not stars.** Routing every pin back to one anchor
  congests the anchor. Grow a tree: route each new pin to the *nearest cell
  already in the net's tree* (multi-source A\*). Shorter total copper, less
  contention.
- **Free the endpoints.** A pad cell is "blocked" by its own pad; explicitly
  un-block the source/target cells before searching or the router can't leave or
  arrive.
- **Rip-up blocker selection.** When a net fails, you don't know exactly which
  nets wall it off. A cheap, effective heuristic: rip the routed nets whose
  geometry falls inside the bounding box of the failing net's pins. Re-queue them
  after. Cap per-net rips to guarantee termination.
- **Log what you dropped.** If the router gives up on a net, say so loudly — a
  silently-unrouted net reads as "done" until DRC (or the fab) finds it.

## The recipe that worked (freerouting) — read this first

This is the flow that actually routed the VCO (16 parts, 13 nets, 2 ICs) to
**0 DRC errors** on the first genuinely-dense board. When autorouting "just
doesn't work," it's almost never the router — it's placement or board setup.
Freerouting routes wires reliably; your job is to hand it a routable board.

Implemented in `toolkit/pcb.py::_route_freerouting` + `build_board`.

**Pipeline (order matters):**

1. **Place** footprints, assign nets, draw the Edge.Cuts outline, set clearances
   (`m_MinClearance`, `m_HoleClearance`, `m_CopperEdgeClearance`).
2. **Pour GND first** — two planes (F.Cu + B.Cu) with NPTH keepouts — and
   `ZONE_FILLER().Fill()` *before* exporting. Freerouting then sees GND as a
   plane and routes **only the signals**, not a ground rat's nest.
3. **Export DSN** with `pcbnew.ExportSpecctraDSN(board, "x.dsn")`. (`kicad-cli`
   has no specctra command — the bridge is in the `pcbnew` module.)
4. **Route** with `freert -de x.dsn -do x.ses`.
5. **Import SES** with `pcbnew.ImportSpecctraSES(board, "x.ses")` — applies the
   traces and vias to the in-memory board.
6. **Refill** the GND planes (now they flow around the new traces) and run DRC.

**The exact gotchas we hit, and the fix for each:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ExportSpecctraDSN failed` | output directory didn't exist yet | `mkdir(parents=True)` before exporting |
| freerouting: "input and output must be specified" | wrong CLI flags | it's `-de` / `-do`, **not** `-i` / `-o` (the v2.1.0 flags) |
| 11 × `unconnected_items` | GND being treated as signal / under-routed | pour + fill GND **before** DSN export so it's a plane |
| 2 × `starved_thermal` | thermal spokes to GND pads too thin | pour `SetPadConnection(ZONE_CONNECTION_FULL)` (solid) |
| 7 × `courtyards_overlap`, `hole_to_hole`, `solder_mask_bridge` | parts placed too close | space parts ≥ their courtyard; resistors need ≥6 mm pitch |
| board 62 mm wide on a 60.7 mm (12HP) panel | margins pushed copper past the panel | pull edge parts in; board must fit **within** the panel width |
| power header courtyard 33 mm tall | latched IDC footprint's ejector clearance | use a plain `PinHeader_2x05` (6×14 mm) instead |

**The debugging loop that got us there:** run → read the DRC error *types* → each
type maps to one fix above → re-run. The count fell 27 → 2 → 0. DRC error
categories are a to-do list, not a wall.

**Key mindset:** placement is ~90% of routability. We never had to touch the
router itself — every failure was a placement or board-setup problem that DRC
named precisely. If freerouting leaves nets unrouted, give it more passes
(`-mp N`) or, far more often, fix the placement.

## Pitfalls specific to scripting KiCad (pcbnew)

- `FOOTPRINT.Remove()` corrupts the global footprint-plugin state and breaks the
  next `FootprintLoad()`. To hide footprint Edge.Cuts/silk, **re-layer** them to
  `Dwgs.User` instead of removing.
- Mutate a footprint's fields/graphics **before** `board.Add()` — once the board
  owns it, SWIG hands back unwrapped pointers and setters vanish.
- Store the **layer index (0/1)**, not the pcbnew layer id, in your own grid
  structures — `B_Cu` is id 31 and will index out of range.
- Keepout rule-areas disallow *everything* by default. For an NPTH pour-clear,
  turn off only `DoNotAllowCopperPour` and explicitly re-allow pads/tracks/vias.
