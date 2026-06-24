# Eurorack module toolkit — kickoff brief

- **Problem:** Going from a circuit idea to a buildable, fab-ready Eurorack
  module is slow and spread across disconnected tools; the goal is a fast
  iteration loop from idea → simulation → physical prototype.
- **Done looks like:** A trivial module taken end-to-end through the toolchain —
  simulated, then PCB + silkscreen + faceplate + mounts generated from shared
  parameters, producing fab-ready files for PCBWay or self-fab.
- **Not now:** Custom EDA/CAD engines (automate KiCad + ngspice, build on
  build123d); complex/active circuits; a UI; a parts library; a second module.
- **First slice:** A passive attenuator (one pot + two jacks) run through the
  whole toolchain — ngspice sanity-sim → automated KiCad PCB + Gerbers +
  silkscreen → build123d 3U panel (HP outline, rail slots, jack/pot cutouts) +
  PCB mount → all exported fab-ready. The circuit is trivial on purpose; the
  **integration seams between the separate tools** are the real target.
- **Architecture:** A loosely-coupled set of small tools (sim runner, KiCad
  automation, panel/mount generators) sharing a common parameter/interchange
  format (`module.toml`) — not one app.
- **Open question:** How far does the shared interchange reach across the
  electrical↔mechanical boundary? Today jack/pot positions, parts, and the
  netlist are defined once and consumed by sim, PCB, and mechanical legs alike.
  Net **topology** generalisation (arbitrary active circuits → generic
  netlist/schematic) is the next frontier.

## First slice — status: DONE ✅

Built and verified end-to-end (`python3 build.py modules/attenuator/module.toml`):

| Leg | Tool | Output | Verified |
|-----|------|--------|----------|
| Simulate | ngspice | AC transfer vs wiper | matches ideal divider to 1e-8 |
| PCB | pcbnew + kicad-cli | `.kicad_pcb`, Gerbers, drill, SVG | maze-routed, **0 DRC errors/warnings**, 4HP outline |
| Mechanical | build123d | panel STL/DXF/SVG, silk SVG, standoff STL | watertight solids, correct 20.02×128.5 mm, cutouts 6/6/7 mm |
