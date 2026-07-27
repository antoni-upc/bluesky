"""
state.py

Data Transfer Objects (DTOs) that flow between the plugin layers:

    BlueSkyBridge     -> BlueSkyTargets     (read from bs.traf)
    IntentClassifier  -> FlightIntent       (what the aircraft intends to do)
    FeasibilityFilter -> FlightIntent       (intent after envelope clamping)
    ReferenceGenerator-> GuidanceReference  (TEM-derived kinematic targets)
    GuidanceLayer     -> ForceCommand       (passthrough — no rate limiting)
    IAircraftDynamics -> AircraftState      (integrated state)

All DTOs are plain Python dataclasses — no NumPy arrays, no BlueSky imports.
This design keeps every layer above `bridge.py` testable with plain objects
and completely decoupled from BlueSky's internal array layout.  `bridge.py`
is the only module that reads/writes `bs.traf`.

`AircraftState` is intentionally open to extension via the ``extra`` dict
rather than subclassing, so a future 6-DOF model can attach attitude and
angular-rate fields without touching any existing method signatures
(see dynamics/interfaces.py).
"""

from dataclasses import dataclass, field
from enum import Enum, auto


# ---------------------------------------------------------------------------
# Flight intent / phase enumerations
# ---------------------------------------------------------------------------

class FlightMode(Enum):
    """Operational intent flags, decoupled from BADA's internal phase strings.

    Vertical modes (used as `vertical_mode`):
        CLIMB, CRUISE, DESCENT, LEVEL_OFF

    Speed modes (used as `speed_mode`):
        ACCELERATE, DECELERATE, CRUISE (= hold current speed)

    Lateral mode (used as a boolean `turning`, not as a FlightMode value):
        TURN
    """
    CLIMB = auto()
    CRUISE = auto()
    DESCENT = auto()
    ACCELERATE = auto()
    DECELERATE = auto()
    LEVEL_OFF = auto()
    TURN = auto()


# Mapping from FlightMode to the phase vocabulary pyBADA expects
# ("cl" / "des" / None for cruise) for its esf() and flightEnvelope calls.
# ACCELERATE/DECELERATE/LEVEL_OFF/TURN do not have a direct BADA phase; the
# caller is responsible for combining vertical_mode and speed_mode to resolve
# the correct bada_phase string (see guidance/reference_generator.py).
BADA_PHASE_MAP = {
    FlightMode.CLIMB:      "cl",
    FlightMode.DESCENT:    "des",
    FlightMode.CRUISE:     None,
    FlightMode.ACCELERATE: None,   # resolved against vertical_mode by caller
    FlightMode.DECELERATE: None,
    FlightMode.LEVEL_OFF:  None,
    FlightMode.TURN:       None,
}


@dataclass
class FlightIntent:
    """Operational intent for one aircraft at the current timestep.

    Produced by IntentClassifier.classify() and optionally modified
    (clamped) by FeasibilityFilter.apply() before reaching the
    ReferenceGenerator.
    """
    vertical_mode:  FlightMode   # CLIMB / CRUISE / DESCENT / LEVEL_OFF
    speed_mode:     FlightMode   # ACCELERATE / DECELERATE / CRUISE (= hold)
    turning:        bool         # True if a heading change is in progress
    target_alt_m:   float        # autopilot target altitude [m]
    target_tas_ms:  float        # autopilot target True Airspeed [m/s]
    target_hdg_deg: float        # autopilot target heading [deg]
    # Set to True by FeasibilityFilter if either target was clamped to the
    # BADA flight envelope.  Used for logging/diagnostics only.
    was_clamped: bool = False
    note: str = ""               # human-readable description of any clamping


# ---------------------------------------------------------------------------
# BlueSky-side data snapshot
# ---------------------------------------------------------------------------

@dataclass
class BlueSkyTargets:
    """Snapshot of what BlueSky's autopilot/ASAS is currently commanding
    for one aircraft.

    Values are read from ``bs.traf.aporasas`` rather than the raw
    ``bs.traf.sel*`` arrays, because aporasas already folds in the LNAV,
    VNAV and ASAS conflict-resolution outputs — it is the closest BlueSky
    concept to "what a real FMS/autopilot would be told to do".
    """
    target_alt_m:    float   # autopilot target altitude [m]
    target_tas_ms:   float   # autopilot target TAS [m/s]
    target_vs_ms:    float   # autopilot target vertical speed [m/s]
    target_hdg_deg:  float   # autopilot target heading [deg]
    bank_limit_deg:  float   # maximum allowable bank angle [deg]

    # CAS [m/s] of the speed constraint at the current active waypoint,
    # or -1.0 if no speed constraint is assigned to this waypoint.
    # Read directly from traf.actwp.spd, NOT from aporasas, because aporasas
    # tracks the aircraft's current speed when swvnavspd is off (as is the
    # case in pybada3dof), making it unreliable for waypoint speed detection.
    actwp_spd_cas_ms: float = -1.0

    # The highest altitude constraint [m] remaining in the route, computed by
    # scanning traf.ap.route[i].wpalt from the current active waypoint index
    # (iactwp) forward.  Unlike target_alt_m (which is gated by BlueSky's
    # VNAV swvnavvs flag and can freeze at an intermediate step-climb waypoint
    # altitude until that waypoint's lat/lon is physically sequenced), this
    # value always reflects the highest altitude the aircraft still needs to
    # reach on the current flight plan.  The IntentClassifier uses it as a
    # step-climb guard: if route_alt_m is significantly above the current
    # altitude, the CL->CR transition is blocked even when the local altitude
    # target has been reached.
    route_alt_m: float = -1.0

    # The lowest altitude constraint [m] remaining in the route (analogous to
    # route_alt_m but for the descent direction).  Used as a step-descent
    # guard: if route_min_alt_m is significantly below the current altitude,
    # a level-off at an intermediate waypoint must not trigger a transition to
    # Cruise, because further descent is still committed ahead.
    route_min_alt_m: float = -1.0


# ---------------------------------------------------------------------------
# Performance envelope (queried from the active BADA adapter each tick)
# ---------------------------------------------------------------------------

@dataclass
class FlightEnvelope:
    """Aircraft performance envelope at the current flight condition.

    All speed limits are in True Airspeed [m/s].  FeasibilityFilter clamps
    FlightIntent targets against these values before they reach the
    ReferenceGenerator.
    """
    vmin_ms:      float   # minimum TAS (stall margin + config) [m/s]
    vmax_ms:      float   # maximum TAS (VMO/MMO) [m/s]
    vstall_ms:    float   # stall speed TAS for current configuration [m/s]
    hmax_m:       float   # service/structural ceiling [m]; -1 if unknown
    vsmin_ms:     float   # minimum (most negative) allowable VS [m/s]
    vsmax_ms:     float   # maximum (most positive) allowable VS [m/s]
    axmax_ms2:    float   # maximum longitudinal acceleration [m/s²]
    thrust_max_n: float   # MCMB (max climb) thrust [N] at current condition
    thrust_idle_n:float   # LIDL (idle) thrust [N] at current condition
    # True when this envelope was built from generic/fallback BADA data
    # (e.g. Dummy-TWIN or J2M___) rather than a type-specific dataset.
    # FeasibilityFilter skips the altitude ceiling clamp for dummy aircraft
    # to prevent the generic model's artificially low ceiling from blocking
    # en-route cruise at realistic flight levels.
    is_dummy: bool = False


# ---------------------------------------------------------------------------
# Energy terms (output of the TEM / BADA adapter layer)
# ---------------------------------------------------------------------------

@dataclass
class EnergyTerms:
    """Everything the Guidance Layer needs from the active BADA adapter
    (BADA 3 or BADA 4, selected via the PERFMODEL stack command) for one
    aircraft at the current timestep.

    This is the sole hand-off point between the Energy layer
    (bada3_adapter / bada4_adapter) and the Guidance layer.
    Neither layer ever touches a Bada3Aircraft or Bada4Aircraft object
    directly, keeping the model-family swap entirely transparent.
    """
    thrust_n:       float   # net engine thrust at current rating [N]
    drag_n:         float   # total aerodynamic drag [N]
    fuel_flow_kgps: float   # fuel flow [kg/s]
    esf:            float   # Energy Share Factor used in ROCD computation [-]
    rocd_ms:        float   # Rate of Climb or Descent from pyBADA ROCD() [m/s]
                            # (computed using the esf above)
    config:         str     # aerodynamic configuration string: "TO"/"CR"/"AP"/"LD"
    thrust_max_n:   float   # MCMB (max climb) thrust at current condition [N]
    thrust_idle_n:  float   # LIDL (idle) thrust at current condition [N]


# ---------------------------------------------------------------------------
# Guidance reference (output of ReferenceGenerator, input to GuidanceLayer)
# ---------------------------------------------------------------------------

@dataclass
class GuidanceReference:
    """Physically consistent kinematic/energetic targets produced by the
    TEM + ESF split inside ReferenceGenerator.

    These are *kinematic* targets (rates and angles), not forces.
    GuidanceLayer translates them into a ForceCommand by passing all fields
    through directly (no actuator rate limiting) before handing off to
    the Dynamics integrator.
    """
    rocd_ms:       float   # target rate of climb/descent [m/s], signed
                           # (+ve = climb, -ve = descent)
    tas_rate_ms2:  float   # target longitudinal TAS acceleration [m/s²]
                           # = (T - D) * (1 - ESF) / mass
    bank_ref_deg:  float   # target bank angle [deg], signed (+ = right turn)
    esf:           float   # Energy Share Factor used to derive rocd/tas_rate
    rating:        str     # BADA thrust rating that produced this reference:
                           # "MCMB" / "LIDL" / "MCRZ (bounded)" / etc.
    thrust_n:      float = 0.0   # net thrust at current rating [N] (for logging)
    drag_n:        float = 0.0   # aerodynamic drag [N] (for logging)
    fuel_flow_kgps:float = 0.0   # fuel flow [kg/s] (for logging + mass update)
    thrust_max_n:  float = 0.0   # MCMB thrust [N] (passed through to dynamics)
    thrust_idle_n: float = 0.0   # LIDL thrust [N] (passed through to dynamics)


# ---------------------------------------------------------------------------
# Force command (output of GuidanceLayer, input to Dynamics)
# ---------------------------------------------------------------------------

@dataclass
class ForceCommand:
    """Actuation command forwarded to the Dynamics integrator.

    All fields are passed through directly from GuidanceReference without
    rate limiting.  vs_ms and bank_deg are the raw TEM/ESF outputs;
    tas_rate_ms2 is the longitudinal acceleration from the Energy layer.
    """
    thrust_n:       float   # net thrust [N] — forwarded for logging
    drag_n:         float   # aerodynamic drag [N] — forwarded for logging
    bank_deg:       float   # bank angle commanded to the dynamics integrator [deg]
    vs_ms:          float   # vertical speed reference (ROCD) [m/s]
                            # passed through directly from GuidanceReference
    tas_rate_ms2:   float   # longitudinal TAS acceleration [m/s²] — pass-through
    fuel_flow_kgps: float   # fuel flow [kg/s] — used for mass depletion
    thrust_max_n:   float = 0.0   # MCMB thrust [N] — forwarded for logging
    thrust_idle_n:  float = 0.0   # LIDL thrust [N] — forwarded for logging


# ---------------------------------------------------------------------------
# Aircraft state (passed through the whole pipeline each tick)
# ---------------------------------------------------------------------------

@dataclass
class AircraftState:
    """Snapshot of the aircraft's kinematic and performance state at the
    start of the current simulation tick.

    In bridge.py this is populated from BlueSky's traf arrays using
    PRE-kinematic values (before _ORIGINAL_UPDATE_AIRSPEED runs) to avoid
    double-counting the kinematic autopilot's speed step.
    """
    lat_deg:  float          # geodetic latitude [deg]
    lon_deg:  float          # geodetic longitude [deg]
    alt_m:    float          # pressure altitude (geometric fallback if no p) [m]
    tas_ms:   float          # True Airspeed [m/s]
    vs_ms:    float          # vertical speed [m/s]
    hdg_deg:  float          # magnetic/true heading [deg]
    bank_deg: float          # bank angle [deg]
    mass_kg:  float          # current aircraft mass [kg]
    ax_ms2:   float = 0.0   # longitudinal TAS acceleration [m/s²]
    cas_ms:   float = 0.0   # Calibrated Airspeed [m/s]; set by bridge from traf.cas
    phase:    str  = "cl"   # last resolved BADA phase string:
                            # "cl" (climb) / "des" (descent) / "cruise"
    # Extension point for future dynamics models (e.g. 6-DOF attitude,
    # angular rates) without changing this dataclass's public API.
    # PointMass3DOF stores thrust/drag/fuel_flow here for the bridge to
    # read back and write into bs.traf for SAVEHEADER logging.
    extra: dict = field(default_factory=dict)
