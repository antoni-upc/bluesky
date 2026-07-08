"""
control/controllers.py

The Guidance Layer resolves *what* the aircraft should do energetically
(ROCD, acceleration, bank angle) using the TEM.  The Controllers' job is
*how fast* that setpoint can physically be tracked — no real aircraft snaps
its bank angle instantly.

Two rate limiters are implemented:

  EnergyRateController (VS / ROCD):
      Passes the BADA TEM vertical-speed reference directly to the dynamics
      layer WITHOUT any rate limiting.  A 0.5 m/s² d(VS)/dt cap was present
      in earlier versions but was removed because:
        - In a 3-DOF point-mass model there are no pitch dynamics, so capping
          d(VS)/dt has no physical basis.
        - The cap actively prevented the BADA ROCD from being applied promptly,
          making climbs unacceptably sluggish.
        - The BADA TEM (MCMB/LIDL/MCRZ rating) is the correct authority for
          the achievable ROCD; no additional clamping is needed at this layer.

  BankController (roll angle):
      Rate-limits the bank-angle setpoint using BANK_RATE_LIMIT_DEGPS.
      A physical roll rate limit IS appropriate here because even a simplified
      3-DOF model should not instantaneously snap to a new bank angle —
      doing so would produce unrealistic lateral guidance.

Both controllers are stateful per-aircraft (they remember the previous
commanded value to rate-limit against) but import no BlueSky or pyBADA
modules — they only see GuidanceReference/ForceCommand DTOs, making them
trivially unit-testable and reusable unchanged if the dynamics model is
later upgraded to 6-DOF.
"""

from dataclasses import dataclass, field
from typing import Dict

from ..state import ForceCommand, GuidanceReference

# Actuator response limits — conservative, generic transport-category defaults.
# Expose as plugin stack-command settings if per-type tuning is needed.
VS_RATE_LIMIT_MS2    = 0.5    # [m/s²] not used (see EnergyRateController docstring)
BANK_RATE_LIMIT_DEGPS = 5.0   # [deg/s] maximum roll rate


@dataclass
class _ControllerState:
    """Mutable per-aircraft state shared by both rate limiters."""
    vs_ms:    float = 0.0   # last commanded VS [m/s]
    bank_deg: float = 0.0   # last commanded bank angle [deg]


class EnergyRateController:
    """Passes the BADA TEM vertical-speed reference through to the dynamics
    layer without modification.

    No rate limiting is applied.  See module docstring for the rationale.
    The class is retained as an explicit architectural boundary so a future
    vertical-speed PID or energy-rate controller can replace it here without
    touching GuidanceLayer or the Dynamics integrator.
    """

    def __init__(self):
        self._state: Dict[int, _ControllerState] = {}

    def command(self, idx: int, ref: GuidanceReference, dt: float) -> float:
        """Return the vertical-speed setpoint for aircraft `idx`.

        Simply returns ref.rocd_ms.  The _ControllerState is updated for
        future extensibility (a rate-limited version would use it).
        """
        st = self._state.setdefault(idx, _ControllerState())
        st.vs_ms = ref.rocd_ms
        return st.vs_ms


class BankController:
    """Rate-limits the bank-angle setpoint from ReferenceGenerator toward
    a physically achievable roll rate (BANK_RATE_LIMIT_DEGPS).

    At each tick, the commanded bank angle advances toward the reference by
    at most BANK_RATE_LIMIT_DEGPS * dt degrees, preventing instantaneous
    roll reversals.
    """

    def __init__(self):
        self._state: Dict[int, _ControllerState] = {}

    def command(self, idx: int, ref: GuidanceReference, dt: float) -> float:
        """Return the rate-limited bank angle for aircraft `idx` [deg]."""
        st       = self._state.setdefault(idx, _ControllerState())
        delta    = ref.bank_ref_deg - st.bank_deg
        max_step = BANK_RATE_LIMIT_DEGPS * dt
        if abs(delta) > max_step:
            st.bank_deg += max_step if delta > 0 else -max_step
        else:
            st.bank_deg = ref.bank_ref_deg
        return st.bank_deg


class FlightPathAngleController:
    """Facade that combines EnergyRateController and BankController into a
    single ForceCommand for the Dynamics integrator.

    Kept as its own class (rather than inlining the logic into GuidanceLayer)
    so the Strategy boundary for a future PID/LQR controller is explicit and
    testable in isolation.
    """

    def __init__(self):
        self._vs_ctrl   = EnergyRateController()
        self._bank_ctrl = BankController()

    def compute(self, idx: int, ref: GuidanceReference, dt: float) -> ForceCommand:
        """Apply rate limiters and assemble a ForceCommand for one aircraft."""
        vs_cmd   = self._vs_ctrl.command(idx, ref, dt)
        bank_cmd = self._bank_ctrl.command(idx, ref, dt)
        return ForceCommand(
            thrust_n=ref.thrust_n,
            drag_n=ref.drag_n,
            bank_deg=bank_cmd,
            vs_ms=vs_cmd,
            tas_rate_ms2=ref.tas_rate_ms2,
            fuel_flow_kgps=ref.fuel_flow_kgps,
            thrust_max_n=ref.thrust_max_n,
            thrust_idle_n=ref.thrust_idle_n,
        )
