"""
energy/factory.py

Centralises creation of the active IPerformanceModel adapter so that the
``PERFMODEL`` stack command and the plugin bootstrap never need to import
or reference the concrete adapter classes directly.

Adding a new BADA family (e.g. BADA 5 or OpenAP) requires only:
  1. Adding a new adapter class that implements IPerformanceModel.
  2. Adding its name -> class mapping to _NAME_MAP here.
  No other file needs to change.
"""

from .bada3_adapter import Bada3PerformanceAdapter
from .bada4_adapter import Bada4PerformanceAdapter
from .performance_model import IPerformanceModel


class PerformanceModelFactory:
    """Factory for IPerformanceModel adapters.

    All adapter creation goes through this class so that the rest of the
    plugin never couples to a concrete adapter class.
    """

    # Registry of valid PERFMODEL names and their adapter classes.
    # Names are matched case-insensitively after stripping whitespace.
    _NAME_MAP = {
        "BADA3": Bada3PerformanceAdapter,
        "BADA4": Bada4PerformanceAdapter,
    }

    @classmethod
    def create(cls, name: str, actype_lookup) -> IPerformanceModel:
        """Instantiate the named performance model adapter.

        :param name:          "BADA3" or "BADA4" (case-insensitive).
        :param actype_lookup: callable(idx) -> ICAO type string, forwarded
                              to the adapter constructor.
        :raises ValueError:   If `name` is not in the registry.
        """
        name = name.upper().strip()
        if name not in cls._NAME_MAP:
            raise ValueError(f"Unknown performance model '{name}'. Use BADA3 or BADA4.")
        return cls._NAME_MAP[name](actype_lookup)

    @classmethod
    def available(cls) -> list:
        """Return the list of registered performance model names."""
        return list(cls._NAME_MAP.keys())
