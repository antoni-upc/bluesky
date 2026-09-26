"""VNAV open climb (fms_climb_mode OPEN) and the climb-rate capability hook."""
import numpy as np
import pytest

import bluesky as bs
from bluesky.traffic import autopilot
from bluesky.traffic.autopilot import open_climb_rate
from bluesky.traffic.performance.perfbase import PerfBase


def rate(vnavvs, capability, active=True, alt=3000.0, target=9000.0):
    return open_climb_rate(np.array([vnavvs]), np.array([active]), np.array([alt]),
                           np.array([target]), np.array([capability]))[0]


def test_open_climb_uses_the_capability_when_it_is_faster():
    assert rate(9.0, 11.5) == 11.5


def test_open_climb_never_climbs_slower_than_the_vnav_gradient():
    # The gradient rate also covers reaching the constraint in time.
    assert rate(9.0, 6.0) == 9.0


@pytest.mark.parametrize('vnavvs, capability, active, alt, target', [
    (9.0, np.nan, True, 3000.0, 9000.0),    # capability unknown
    (9.0, -2.0, True, 3000.0, 9000.0),      # no positive capability
    (-9.0, 11.5, True, 9000.0, 3000.0),     # descending
    (0.0, 11.5, True, 9000.0, 9000.0),      # level
    (9.0, 11.5, False, 3000.0, 9000.0),     # VNAV not controlling the vertical
    (9.0, 11.5, True, 8999.8, 9000.0),      # at the constraint
])
def test_open_climb_leaves_other_cases_to_vnav(vnavvs, capability, active, alt, target):
    assert rate(vnavvs, capability, active, alt, target) == vnavvs


@pytest.mark.parametrize('value, expected', [('STEEPNESS', 'STEEPNESS'), ('open', 'OPEN')])
def test_climb_mode_setting(monkeypatch, value, expected):
    monkeypatch.setattr(bs.settings, 'fms_climb_mode', value, raising=False)
    assert autopilot.climb_mode() == expected


def test_unknown_climb_mode_is_rejected(monkeypatch):
    monkeypatch.setattr(bs.settings, 'fms_climb_mode', 'FAST', raising=False)
    with pytest.raises(ValueError, match='STEEPNESS or OPEN'):
        autopilot.climb_mode()


def test_vnavclimb_command_sets_and_reports_the_mode(monkeypatch):
    monkeypatch.setattr(bs.settings, 'fms_climb_mode', 'STEEPNESS', raising=False)
    ap = object.__new__(autopilot.Autopilot)
    assert autopilot.Autopilot.setvnavclimb(ap, 'open') == (True, 'VNAVCLIMB set to OPEN')
    assert bs.settings.fms_climb_mode == 'OPEN'
    assert autopilot.Autopilot.setvnavclimb(ap) == (True, 'VNAVCLIMB is OPEN')
    ok, _ = autopilot.Autopilot.setvnavclimb(ap, 'FAST')
    assert not ok and bs.settings.fms_climb_mode == 'OPEN'


def test_base_capability_uses_real_vsmax_limits_only():
    perf = object.__new__(PerfBase)
    perf.vsmax = np.array([12.5, 1e6])
    capability = perf.climb_rate_capability()
    assert capability[0] == 12.5 and np.isnan(capability[1])


def test_tem_capability_is_the_known_positive_rated_climb_rate():
    from bluesky.plugins.pybada.performance import PyBadaTEM
    perf = object.__new__(PyBadaTEM)
    perf.vsmax = np.array([1e6, 1e6, 1e6, 1e6])
    perf.model_rocd = np.array([10.2, -3.0, np.nan, 9.0])
    perf.invalid = np.array([False, False, False, True])
    capability = perf.climb_rate_capability()
    assert capability[0] == 10.2
    assert np.isnan(capability[1:]).all()
