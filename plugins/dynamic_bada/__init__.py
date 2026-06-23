"""
dynamic_bada — Dynamic force-driven BADA performance plugin for BlueSky.

Load with:
    PLUGINS LOAD dynamic_bada

Stack commands exposed:
    DYNMODE  [acid] [0|1|2]     — set fidelity mode globally or per aircraft
    DYNBADA  [acid] [3|4]       — select BADA generation globally or per aircraft
    DYNSTATS [acid]             — show full dynamic state for one aircraft
    DYNRESET                    — re-read config.yaml without restarting
"""
from .plugin import init_plugin, DynamicBada  # noqa: F401
