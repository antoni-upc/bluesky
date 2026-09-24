from types import SimpleNamespace

import pytest

from bluesky.tools.aero import mach2cas
from bluesky.traffic.route import _direct_speed_target


@pytest.mark.parametrize('selected', [.78, 150.0])
def test_pybada_direct_keeps_original_speed_representation(selected):
    perf = SimpleNamespace(preserves_direct_mach=True)
    assert _direct_speed_target(selected, 10000.0, perf) == selected


def test_native_direct_retains_existing_mach_to_cas_behavior():
    perf = SimpleNamespace()
    assert _direct_speed_target(.78, 10000.0, perf) == pytest.approx(
        mach2cas(.78, 10000.0))
    assert _direct_speed_target(150.0, 10000.0, perf) == 150.0
