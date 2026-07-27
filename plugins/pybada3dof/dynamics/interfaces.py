"""
dynamics/interfaces.py

``IAircraftDynamics`` is the one interface that must stay stable for a
future 3-DOF -> 6-DOF migration. Everything above this layer (Guidance,
Energy) speaks only in terms of ``GuidanceReference`` / ``ForceCommand`` —
concepts that remain meaningful regardless of how many degrees of freedom
the integrator below actually uses. Swapping the model is meant to be a
one-line change in guidance_layer.py, nothing else.
"""

from abc import ABC, abstractmethod

from ..state import AircraftState, ForceCommand


class IAircraftDynamics(ABC):

    @abstractmethod
    def integrate(self, state: AircraftState, command: ForceCommand, dt: float) -> AircraftState:
        """Advance `state` by `dt` seconds under `command`. Must return a
        *new* AircraftState (pure function) rather than mutating in place,
        so callers can keep the previous state around for logging/rollback.
        """
