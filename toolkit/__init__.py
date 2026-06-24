"""Eurorack module toolchain.

A set of small, independently-runnable tools that turn one shared module spec
(`module.toml`) into a simulated, fabricable Eurorack module:

  spec   -- the shared interchange (single source of truth)
  sim    -- ngspice netlist + run (idea -> simulate)
  pcb    -- pcbnew board generation + kicad-cli fab export
  panel  -- build123d faceplate + PCB mount (mechanical output)
"""
from . import _env  # noqa: F401  (side-effect: make pcbnew importable)
from .spec import Module, load

__all__ = ["Module", "load"]
