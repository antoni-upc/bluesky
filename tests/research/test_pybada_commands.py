import pytest

from bluesky.plugins.pybada_tem import _parse_dynamics_mode


def test_kinematic_mode_name():
    assert _parse_dynamics_mode('KINEMATIC') == 0


def test_tem_mode_name():
    assert _parse_dynamics_mode('TEM') == 1


def test_unknown_dynamics_mode_is_actionable():
    for value in ('fast', 'DYNAMIC', 'TRUE', '1', 1, 'DYNMODE'):
        with pytest.raises(ValueError, match='KINEMATIC or TEM'):
            _parse_dynamics_mode(value)
