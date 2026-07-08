"""
dynamics/point_mass_3dof.py

Point-mass 3-DOF integrator.  All forces have already been resolved by the
Energy and Control layers above; this module simply integrates the BADA
point-mass equations of motion for one timestep dt:

    dV/dt   = (T - D)/mass - g*sin(gamma)   (already folded into
                                              ForceCommand.tas_rate_ms2
                                              by the Guidance Layer's ESF
                                              split, so we apply it directly)

    dh/dt   = V * sin(gamma) = VS            (ForceCommand.vs_ms, directly
                                              from the BADA ROCD via
                                              EnergyRateController)

    dpsi/dt = g * tan(bank) / V              (turn-rate from bank angle,
                                              computed by pyBADA's
                                              Bada.rateOfTurn_bankAngle())

Ground-track position (lat/lon) is intentionally NOT integrated here.
BlueSky's own Traffic.update_pos() already performs correct great-circle
propagation from tas/hdg/vs.  Duplicating that integration here would
risk divergence from BlueSky's geodetic model.  Instead, the bridge writes
the new tas, hdg, and vs back into bs.traf after each tick and lets
BlueSky handle position propagation, exactly as it does for the native
kinematic model — only the *source* of those values changes.
"""

from pyBADA.aircraft import Bada

from ..state import AircraftState, ForceCommand
from .interfaces import IAircraftDynamics


class PointMass3DOF(IAircraftDynamics):

    def integrate(self, state: AircraftState, command: ForceCommand, dt: float) -> AircraftState:
        """Advance `state` by `dt` seconds under `command`.

        Returns a *new* AircraftState; never mutates `state` in place so
        callers can retain the previous state for logging or rollback.

        :param state:   Aircraft state at the start of the tick (pre-kinematic
                        snapshot from bridge.py).
        :param command: Rate-limited force command from FlightPathAngleController.
        :param dt:      Simulation timestep [s].
        :returns:       New AircraftState after integration.
        """
        # Longitudinal: integrate TAS from the ESF-derived acceleration.
        # Clamp at zero to prevent negative TAS (e.g. if initial conditions
        # produce a large deceleration on the first tick).
        new_tas = max(state.tas_ms + command.tas_rate_ms2 * dt, 0.0)

        # Vertical: ROCD is applied directly (no further integration of VS;
        # altitude is updated from vs_ms, not from the force balance directly).
        new_vs = command.vs_ms

        # Lateral: compute heading rate from bank angle using the kinematic
        # turn-rate formula dpsi/dt = g * tan(bank) / V.
        # Guard against very low TAS or near-zero bank to avoid division errors.
        turn_rate_degps = 0.0
        if new_tas > 1.0 and abs(command.bank_deg) > 1e-3:
            turn_rate_degps = Bada.rateOfTurn_bankAngle(
                TAS=new_tas, bankAngle=command.bank_deg
            )

        new_hdg = (state.hdg_deg + turn_rate_degps * dt) % 360.0
        new_alt = state.alt_m + new_vs * dt

        # Mass depletion: subtract fuel burned this tick.
        # Clamp at 1.0 kg to prevent zero or negative mass from corrupting
        # downstream BADA aerodynamic calculations.
        new_mass = max(state.mass_kg - command.fuel_flow_kgps * dt, 1.0)

        return AircraftState(
            # lat/lon are NOT updated here — BlueSky's update_pos() handles
            # great-circle propagation from the new tas/hdg/vs below.
            lat_deg=state.lat_deg, lon_deg=state.lon_deg,
            alt_m=new_alt, tas_ms=new_tas, vs_ms=new_vs,
            hdg_deg=new_hdg, bank_deg=command.bank_deg,
            mass_kg=new_mass,
            ax_ms2=command.tas_rate_ms2,   # store for next-tick cruise T = D + m*ax
            phase=state.phase,             # unchanged — set by GuidanceLayer after return
            extra={
                # Stored in extra{} so bridge.py can read them back and write
                # into bs.traf arrays for SAVEHEADER logging without adding
                # fields to the AircraftState dataclass.
                "thrust_n":        command.thrust_n,
                "drag_n":          command.drag_n,
                "fuel_flow_kgps":  command.fuel_flow_kgps,
                "thrust_max_n":    command.thrust_max_n,
                "thrust_idle_n":   command.thrust_idle_n,
            },
        )
