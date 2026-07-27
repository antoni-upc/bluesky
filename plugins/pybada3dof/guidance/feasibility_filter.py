"""
guidance/feasibility_filter.py

Single enforcement point where an operationally "impossible" FlightIntent
is clamped to the closest one the aircraft's current BADA envelope can
actually deliver.

BlueSky scenarios routinely command things a Point-Mass 3-DOF aircraft
cannot do instantly: jump to a new CAS, capture an altitude in zero time,
etc.  Without clamping, those commands reach the ReferenceGenerator and
produce physically invalid ESF/ROCD values.

Design choice: this module never raises or rejects.  A guidance layer that
refuses to produce an answer is worse than one that produces the closest
safe answer and records that it had to (via FlightIntent.was_clamped / note).

Two constraints are enforced:

  Speed envelope clamp:
      target_tas_ms is clamped to [vmin_ms, vmax_ms] from the BADA envelope.
      Both limits are always applied regardless of whether the aircraft is
      using real or fallback (dummy) BADA data.

  Altitude ceiling clamp:
      target_alt_m is clamped to hmax_m from the BADA envelope.
      This clamp is intentionally skipped for dummy aircraft (is_dummy=True)
      because generic fallback models (e.g. J2M___, Dummy-TWIN) have
      artificially low service ceilings that would prevent the simulation
      from reaching realistic en-route cruise flight levels.
      To re-enable ceiling clamping for dummy aircraft (e.g. for testing),
      replace the condition with:  `if envelope.hmax_m > 0:`
"""

from ..state import FlightEnvelope, FlightIntent


class FeasibilityFilter:

    def apply(self, intent: FlightIntent, envelope: FlightEnvelope) -> FlightIntent:
        """Clamp `intent` to the physical limits in `envelope`.

        Modifies `intent` in place and returns it.  If any clamping was
        applied, `intent.was_clamped` is set to True and a human-readable
        description is stored in `intent.note`.
        """
        notes = []

        # --- Speed envelope clamp -------------------------------------------
        # Ensure target TAS stays within [vmin_ms, vmax_ms].  A tolerance of
        # 1e-6 m/s prevents spurious clamping notes from floating-point noise.
        clamped_tas = min(max(intent.target_tas_ms, envelope.vmin_ms), envelope.vmax_ms)
        if abs(clamped_tas - intent.target_tas_ms) > 1e-6:
            notes.append(
                f"tas {intent.target_tas_ms:.1f} clamped to envelope "
                f"[{envelope.vmin_ms:.1f}, {envelope.vmax_ms:.1f}]"
            )
            intent.target_tas_ms = clamped_tas

        # --- Altitude ceiling clamp -----------------------------------------
        # Skipped for dummy/fallback aircraft: their hmax_m reflects the
        # generic model's ceiling, not the actual type's service ceiling.
        # To clamp dummy aircraft as well, replace the condition with:
        #if envelope.hmax_m > 0:
        if not envelope.is_dummy and envelope.hmax_m > 0:
            clamped_alt = min(intent.target_alt_m, envelope.hmax_m)
            if clamped_alt != intent.target_alt_m:
                notes.append(f"altitude clamped to ceiling {envelope.hmax_m:.0f} m")
                intent.target_alt_m = clamped_alt

        if notes:
            intent.was_clamped = True
            intent.note = "; ".join(notes)

        return intent
