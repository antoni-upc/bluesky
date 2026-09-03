import datetime as dt

import pytest
import bluesky as bs

from tests.research.matrix_preflight import (
    check_bounds, era5_targets, scenario_contract,
)
from tests.research.scenario_profiles import ProfileConfigError


def test_repository_scenarios_have_execution_contract():
    for name, duration in (("example_direct.scn", 8100), ("example_ops.scn", 10800)):
        contract = scenario_contract(f"experiments/{name}")
        assert contract["simulation_utc"] == dt.datetime(
            2025, 5, 1, 12, tzinfo=dt.timezone.utc
        )
        assert contract["safety_duration_s"] == duration
        assert contract["positions"]


def test_bounds_reject_an_excluded_route_position():
    with pytest.raises(ProfileConfigError, match="exclude 1 route position"):
        check_bounds([(41.0, 2.0), (54.0, 2.0)], [40, -5, 53, 10])


def test_era5_targets_cover_every_possible_hour(tmp_path):
    bs.settings.era5_region = "western-europe"
    targets = era5_targets(
        tmp_path, dt.datetime(2025, 5, 1, 12, tzinfo=dt.timezone.utc),
        8100, [40, -5, 53, 10],
    )
    assert [path.name.split("_")[2] for path in targets] == [
        "20250501T1200Z", "20250501T1300Z", "20250501T1400Z",
    ]
