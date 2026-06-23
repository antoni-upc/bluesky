"""
dynamic_bada.config
===================
Loads and exposes the plugin configuration from config.yaml.

Usage
-----
    from dynamic_bada.config import cfg
    print(cfg.default_fidelity_mode)

The singleton ``cfg`` is populated on first import.  Call ``cfg.reload()``
to re-read the YAML at runtime (e.g. after a DYNRESET stack command).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# ── Optional YAML import ────────────────────────────────────────────────────────
try:
    import yaml as _yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

# Absolute path to the config file that lives next to this module
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


@dataclass
class DynBadaConfig:
    """Complete plugin configuration."""

    # pyBADA paths and versions
    bada3_dir:     str = ""
    bada4_dir:     str = ""
    bada3_version: str = "3.15"
    bada4_version: str = "4.2"
    bada4_fallback: str = "Dummy-TWIN"

    default_bada_version: int = 4
    default_fidelity_mode: int = 1  # MODE 1 as per user approval
    performance_dt: float = 1.0

    vs_threshold_climb_m_s:   float =  0.5
    vs_threshold_descent_m_s: float = -0.5
    min_tas_m_s:              float =  1.0

    integrator: str = "euler"
    roll_rate_deg_s: float = 5.0   # Maximum roll rate for Mode 2 [deg/s]

    # ── Loader ─────────────────────────────────────────────────────────────────

    def reload(self, path: str = _CONFIG_PATH) -> None:
        """Parse *path* (YAML) and update this config in-place."""
        if not _HAVE_YAML:
            print("[dynamic_bada/config] PyYAML not installed — using defaults.")
            return
        try:
            with open(path) as fh:
                data: dict[str, Any] = _yaml.safe_load(fh) or {}
        except FileNotFoundError:
            print(f"[dynamic_bada/config] {path} not found — using defaults.")
            return
        except Exception as exc:
            print(f"[dynamic_bada/config] Failed to parse {path}: {exc}")
            return

        # Scalar fields
        for key in (
            "bada3_dir", "bada4_dir", "bada3_version", "bada4_version",
            "bada4_fallback", "default_bada_version", "default_fidelity_mode",
            "performance_dt", "vs_threshold_climb_m_s",
            "vs_threshold_descent_m_s", "min_tas_m_s", "integrator",
            "roll_rate_deg_s",
        ):
            if key in data:
                object.__setattr__(self, key, data[key])


# ── Module-level singleton ─────────────────────────────────────────────────────

cfg = DynBadaConfig()
cfg.reload()
