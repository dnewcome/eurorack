# eurorack

A code-driven toolchain for taking a Eurorack module from **idea → simulation →
fab-ready prototype**.

It is deliberately *not* one monolithic app. It's a set of small, independently
runnable tools that share **one module spec** and each drive a best-in-class
engine — ngspice for circuits, KiCad for PCBs, build123d for mechanical parts.
Describe a module once; get a simulation, a manufacturable PCB, and a 3U
faceplate out the other end.

```
                       module.toml
              (single source of truth)
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
   toolkit/sim.py      toolkit/pcb.py      toolkit/panel.py
        │                   │                   │
     ngspice         pcbnew + kicad-cli      build123d
        │                   │                   │
   AC transfer /     route + DRC-gated      panel STL/DXF/SVG,
   sanity sim        .kicad_pcb, Gerbers,   silkscreen, PCB mount
                     drill, SVG preview
                            │                   │
                            └──────► PCBWay / your laser + printer
```

---

## Quick start

```bash
# run the whole pipeline for a module
python3 build.py modules/attenuator/module.toml

# …or run a single leg
python3 -m toolkit.sim   modules/attenuator/module.toml
python3 -m toolkit.pcb   modules/attenuator/module.toml
python3 -m toolkit.panel modules/attenuator/module.toml
```

All artifacts land under `build/<module>/`:

```
build/attenuator/
├── pcb/
│   ├── attenuator.kicad_pcb      # open/edit in KiCad
│   ├── attenuator.svg            # board preview
│   └── gerbers/                  # Gerbers + drill — zip and send to a fab
└── panel/
    ├── panel.stl                 # 3D faceplate (print / visualise)
    ├── panel.dxf                 # 2D cut profile  → laser cutter
    ├── panel.svg                 # cut-profile preview
    ├── silk.svg                  # silkscreen layer → screen printing
    └── standoff.stl              # M3 PCB standoff  → 3D print
```

---

## The shared spec (`module.toml`)

One file describes a module; **every leg reads it**. Here's the attenuator,
annotated:

```toml
[module]
name = "attenuator"        # output dir + file stem
title = "ATTEN"            # silkscreen / panel label
hp = 4                     # width in HP (1 HP = 5.08 mm)

# Components carry a panel position (top-left origin, +y down, mm).
# The SAME (x, y) places the PCB footprint AND cuts the faceplate hole.
[[components]]
ref = "J1"; type = "jack_35"; role = "input";  x = 10.16; y = 24.0
[[components]]
ref = "RV1"; type = "pot_9mm"; role = "level"; value = "100k"; x = 10.16; y = 64.0
[[components]]
ref = "J2"; type = "jack_35"; role = "output"; x = 10.16; y = 104.0

# Nets connect REF.PIN tokens. Read by the sim netlister AND PCB net assignment.
[[nets]]
name = "IN";  connect = ["J1.TIP", "RV1.1"]
[[nets]]
name = "OUT"; connect = ["RV1.2", "J2.TIP"]
[[nets]]
name = "GND"; connect = ["RV1.3", "J1.SLEEVE", "J2.SLEEVE"]
```

> TOML note: each `[[components]]` block normally uses one `key = value` per
> line; the inline `;` form above is shown only for compactness in this README.
> See `modules/attenuator/module.toml` for the canonical layout.

### Parts registry — the real interchange

`toolkit/parts.py` maps an abstract part **`type`** to everything the three legs
need, defined **once**:

| Field | Used by | Meaning |
|-------|---------|---------|
| `footprint` | PCB | KiCad library footprint (`Lib:Name`) |
| `pins` | PCB + sim | logical pin (`TIP`, `WIPER`, …) → footprint pad number |
| `panel_d` | mechanical | panel cutout diameter (mm) |
| `sim` | sim | how to model it (`potentiometer`, `resistor`, `passthrough`) |

Add a part here and all three legs immediately understand it. This is what makes
the toolchain a toolchain rather than three disconnected scripts.

---

## Example output (sim leg)

ngspice computes the small-signal transfer of the pot-as-divider across wiper
positions and checks it against the analytic ideal:

```
ngspice AC analysis @ 1000 Hz  (module: attenuator)
 wiper   gain V/V   gain dB  expected       err
  0.00     0.0000  -160.00    0.0000  1.00e-08
  0.25     0.2500   -12.04    0.2500  0.00e+00
  0.50     0.5000    -6.02    0.5000  0.00e+00
  0.75     0.7500    -2.50    0.7500  0.00e+00
  1.00     1.0000    -0.00    1.0000  1.00e-08
max error vs ideal divider: 1.00e-08
```

---

## Requirements

| Tool | Install | Used for |
|------|---------|----------|
| Python | 3.12+ | everything |
| **ngspice** | `apt install ngspice` | sim leg |
| **KiCad 9** | provides `kicad-cli` + `pcbnew` Python module | PCB leg |
| **build123d** | `pip install build123d` | mechanical leg |
| **freerouting** | `freert` on PATH (+ Java) | autorouting dense boards (`router = "freerouting"`); not needed for the built-in maze router |

KiCad's `pcbnew` module lives in the system `dist-packages` dir.
`toolkit/_env.py` *appends* it to `sys.path` (not inserts — so the toolchain's
own numpy/build123d still win), letting the whole toolchain run in one
interpreter.

---

## Project layout

```
toolkit/
  spec.py        load + validate module.toml → dataclasses (shared interchange)
  parts.py       part registry (footprint / pins / cutout / sim model)
  sim.py         ngspice netlister (AC + transient) + runner
  pcb.py         pcbnew board gen + router (maze | freerouting) + DRC + export
  panel.py       build123d faceplate + PCB standoff
  models.spice   behavioral op-amp / OTA / TL072 / LM13700 subckts
  _env.py        makes pcbnew importable alongside the rest
modules/
  attenuator/module.toml   passive attenuator (AC sim, maze router)
  vco/module.toml          linear VCO (transient sim, freerouting)
build.py         end-to-end driver
docs/AUTOROUTING.md  how the autorouting actually works (read this)
BRIEF.md         kickoff brief + first-slice status
```

---

## Status & roadmap

**Two modules build end-to-end and DRC-clean:**

| Module | Sim | PCB | Mechanical |
|--------|-----|-----|------------|
| **attenuator** (4HP) | AC transfer matches ideal divider to **1e-8** | maze router, **0 DRC errors**, GND pour | panel STL/DXF/silk, standoff |
| **vco** (12HP) | transient: oscillates, **f = 23.5·CV + 644 Hz, R²=0.99998** | freerouting, **0 DRC errors**, 16 parts / 2 ICs, dual GND pour | panel STL/DXF/silk, standoff |

The VCO is a linear triangle/square oscillator (LM13700 OTA core + TL072). Both
the active-device transient sim and the dense 2-layer autoroute are real — see
`docs/AUTOROUTING.md` for how the freerouting flow was made to work.

**Next frontiers:**

- **1V/oct expo converter** feeding the OTA bias current (the VCO is linear-CV
  today; the expo stage is the documented next step).
- Real **Thonkiconn** footprints (currently a CUI 3.5 mm stand-in — one string
  to swap in `parts.py`).
- **Auto-placement** — routing is solved, but component placement is still hand-
  tuned per module; an auto-placer would close the loop.
- Vendor-accurate IC SPICE models (today the op-amp/OTA are behavioral).
- A growing **parts + module library**.

See `BRIEF.md` for the scoping decisions behind all of this.
