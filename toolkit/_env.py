"""Environment shim.

KiCad's Python bindings (`pcbnew`) live in the system dist-packages dir, which
isn't on this interpreter's path. Append (don't insert) so the toolchain's own
numpy/build123d win over anything shadowing them in dist-packages.

Import this module before importing `pcbnew`.
"""
import sys

_KICAD_DIST = "/usr/lib/python3/dist-packages"
if _KICAD_DIST not in sys.path:
    sys.path.append(_KICAD_DIST)
