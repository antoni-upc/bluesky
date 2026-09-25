"""Speed-change anticipation evaluated at the waypoint altitude (fms_speed_constraint_altitude)."""
from types import SimpleNamespace

import numpy as np
import pytest

import bluesky as bs
from bluesky.tools.aero import ft, kts, vcasormach2tas
from bluesky.traffic import autopilot
from bluesky.traffic.autopilot import distaccel, waypoint_speed_change_distance


def traffic(monkeypatch, alt_ft, spd, nextspd, nextspdalt_ft):
    alt = np.array([alt_ft * ft])
    actwp = SimpleNamespace(spd=np.array([spd]), nextspd=np.array([nextspd]),
                            nextspdalt=np.array([nextspdalt_ft * ft if nextspdalt_ft >= 0 else -999.]))
    tas = vcasormach2tas(actwp.spd, alt) if spd > 0 else np.array([150.0])
    monkeypatch.setattr(bs, 'traf', SimpleNamespace(alt=alt, tas=tas, actwp=actwp), raising=False)
    return tas, alt


def current_distance(tas, alt, nextspd, accel, decel):
    """The original anticipation: next speed converted at the aircraft altitude."""
    nexttas = vcasormach2tas(np.array([nextspd]), alt)
    return distaccel(tas, nexttas, np.where(nexttas > tas, accel, decel))


def test_cas_to_mach_crossover_needs_no_early_acceleration(monkeypatch):
    accel, decel = np.array([0.2]), np.array([0.5])
    tas, alt = traffic(monkeypatch, 12000.0, 310 * kts, 0.78, 28422.7)
    original = current_distance(tas, alt, 0.78, accel, decel)
    fixed = waypoint_speed_change_distance(accel, decel, original)
    # At FL284 (ISA), 310 kt CAS is M0.790: only the small slow-down to M0.780 remains.
    residual = distaccel(vcasormach2tas(np.array([310 * kts]), np.array([28422.7 * ft])),
                         vcasormach2tas(np.array([0.78]), np.array([28422.7 * ft])), decel)
    assert original[0] > 50_000.0
    assert fixed[0] == pytest.approx(residual[0], rel=1e-12)
    assert fixed[0] < 2_000.0


def test_mach_to_cas_descent_handover_needs_no_early_change(monkeypatch):
    accel, decel = np.array([0.2]), np.array([0.5])
    tas, alt = traffic(monkeypatch, 38966.0, 0.78, 300 * kts, 29380.9)
    original = current_distance(tas, alt, 300 * kts, accel, decel)
    fixed = waypoint_speed_change_distance(accel, decel, original)
    assert original[0] > 50_000.0
    assert fixed[0] < 1_000.0


def test_cas_change_at_the_waypoint_altitude_matches_the_original(monkeypatch):
    accel, decel = np.array([0.3]), np.array([0.5])
    tas, alt = traffic(monkeypatch, 11173.8, 250 * kts, 310 * kts, 11173.8)
    original = current_distance(tas, alt, 310 * kts, accel, decel)
    fixed = waypoint_speed_change_distance(accel, decel, original)
    assert fixed[0] == pytest.approx(original[0], rel=1e-12)


@pytest.mark.parametrize('spd, nextspd, nextspdalt_ft', [
    (250 * kts, 310 * kts, -999.0),   # waypoint without altitude
    (-999.0, 310 * kts, 11000.0),     # current leg without speed
    (250 * kts, -999.0, 11000.0),     # next waypoint without speed
])
def test_missing_data_keeps_the_original_distance(monkeypatch, spd, nextspd, nextspdalt_ft):
    traffic(monkeypatch, 5000.0, spd, nextspd, nextspdalt_ft)
    original = np.array([1234.5])
    fixed = waypoint_speed_change_distance(np.array([0.3]), np.array([0.5]), original)
    assert fixed[0] == original[0]


@pytest.mark.parametrize('value, expected', [('CURRENT', 'CURRENT'), ('waypoint', 'WAYPOINT')])
def test_setting_values(monkeypatch, value, expected):
    monkeypatch.setattr(bs.settings, 'fms_speed_constraint_altitude', value, raising=False)
    assert autopilot.speed_constraint_altitude() == expected


def test_unknown_setting_is_rejected(monkeypatch):
    monkeypatch.setattr(bs.settings, 'fms_speed_constraint_altitude', 'NEXT', raising=False)
    with pytest.raises(ValueError, match='CURRENT or WAYPOINT'):
        autopilot.speed_constraint_altitude()


def test_spdconalt_command_sets_and_reports_the_setting(monkeypatch):
    monkeypatch.setattr(bs.settings, 'fms_speed_constraint_altitude', 'CURRENT', raising=False)
    ap = object.__new__(autopilot.Autopilot)
    setter = autopilot.Autopilot.setspdconalt
    assert setter(ap, 'waypoint') == (True, 'SPDCONALT set to WAYPOINT')
    assert bs.settings.fms_speed_constraint_altitude == 'WAYPOINT'
    assert setter(ap) == (True, 'SPDCONALT is WAYPOINT')
    ok, _ = setter(ap, 'NEXT')
    assert not ok and bs.settings.fms_speed_constraint_altitude == 'WAYPOINT'


def crossover(selected, handover, alt_ft, active=True):
    from bluesky.traffic.autopilot import crossover_speed
    return crossover_speed(np.array([selected]), np.array([handover]),
                           np.array([alt_ft * ft]), np.array([active]))[0]


def test_climb_crossover_holds_cas_until_the_mach_is_reached():
    # Leg flown at M0.78 after a 310 kt leg: below the crossover altitude 310 kt is slower.
    assert crossover(0.78, 310 * kts, 24_500.0) == 310 * kts
    assert crossover(0.78, 310 * kts, 30_000.0) == 0.78


def test_descent_crossover_holds_mach_until_the_cas_is_reached():
    # Leg flown at 300 kt after a M0.78 leg: above the crossover altitude M0.78 is slower.
    assert crossover(300 * kts, 0.78, 37_500.0) == 0.78
    assert crossover(300 * kts, 0.78, 25_000.0) == 300 * kts


@pytest.mark.parametrize('selected, handover, active', [
    (310 * kts, 250 * kts, True),    # two CAS values
    (0.78, 0.76, True),              # two Mach numbers
    (0.78, -999.0, True),            # nothing to hand over from
    (0.78, 310 * kts, False),        # VNAV speed or LNAV not in control
])
def test_crossover_leaves_other_pairs_unchanged(selected, handover, active):
    assert crossover(selected, handover, 24_500.0, active) == selected
