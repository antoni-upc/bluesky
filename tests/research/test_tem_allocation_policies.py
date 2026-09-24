"""Portable contract for the TEM energy-allocation policies."""

import itertools

import numpy as np
import pytest

from bluesky.plugins.pybada.allocation import (
    AllocationPolicy, JointWeights, acceleration_capability, allocate_energy,
    parse_allocation_policy)
from bluesky.plugins.pybada.model import EvaluationError, allocate_speed_priority
from bluesky.tools.aero import g0

STATE = dict(tas=200.0, mass=60_000.0, drag=30_000.0, idle_thrust=10_000.0,
             maximum_thrust=80_000.0)
K = g0 / STATE['tas']
E_IDLE = (STATE['idle_thrust'] - STATE['drag']) / STATE['mass']
E_MAX = (STATE['maximum_thrust'] - STATE['drag']) / STATE['mass']
WEIGHTS = JointWeights(1.0, 0.04)


def allocate(policy, requested, reference, low, high, **extra):
    return allocate_energy(policy, requested_acceleration=requested,
                           reference_vertical_rate=reference, minimum_vertical_rate=low,
                           maximum_vertical_rate=high, **STATE, **extra)


def assert_physical(result, low, high, floor=-np.inf):
    assert STATE['idle_thrust'] - 1e-6 <= result.thrust <= STATE['maximum_thrust'] + 1e-6
    assert low - 1e-9 <= result.vertical_rate <= high + 1e-9
    specific_power = (result.thrust - STATE['drag']) / STATE['mass']
    assert result.acceleration + K * result.vertical_rate == pytest.approx(specific_power,
                                                                         abs=1e-9)
    if np.isfinite(floor):
        assert result.acceleration >= floor - 1e-9


def test_policy_names_and_aliases():
    assert parse_allocation_policy('speed') == AllocationPolicy.SPEED
    assert parse_allocation_policy('VERTICAL_PRIORITY') == AllocationPolicy.VERTICAL
    assert parse_allocation_policy('JOINT').value == 'JOINT'
    with pytest.raises(ValueError, match='SPEED, VERTICAL, or JOINT'):
        parse_allocation_policy('ESF')


@pytest.mark.parametrize(('requested', 'reference'), [(-0.3, -15.0), (0.0, 15.0), (0.5, 8.0)])
def test_speed_priority_is_the_existing_allocator(requested, reference):
    low, high = min(0.0, reference), max(0.0, reference)
    expected = allocate_speed_priority(
        requested_acceleration=requested, preferred_vertical_rate=reference,
        minimum_vertical_rate=low, maximum_vertical_rate=high, **STATE)
    result = allocate(AllocationPolicy.SPEED, requested, reference, low, high)
    assert (result.thrust, result.acceleration, result.vertical_rate, result.required_thrust,
            result.limited, result.reason) == expected


@pytest.mark.parametrize('policy', list(AllocationPolicy))
@pytest.mark.parametrize(('requested', 'reference'),
                         list(itertools.product([-2.0, -0.2, 0.0, 0.2, 2.0], [-15.0, 0.0, 15.0])))
def test_every_policy_is_physical(policy, requested, reference):
    low, high = min(0.0, reference), max(0.0, reference)
    result = allocate(policy, requested, reference, low, high, weights=WEIGHTS)
    assert_physical(result, low, high)


def test_vertical_priority_holds_the_reference_and_trades_speed():
    # A 15 m/s climb costs k*15 = 0.736 m/s2 of the 0.833 m/s2 available.
    result = allocate(AllocationPolicy.VERTICAL, 2.0, 15.0, 0.0, 15.0)
    assert result.vertical_rate == 15.0
    assert result.acceleration == pytest.approx(E_MAX - K * 15.0)
    assert result.limited and result.reason == 'ABOVE_MAXIMUM_THRUST'
    speed = allocate(AllocationPolicy.SPEED, 2.0, 15.0, 0.0, 15.0)
    assert speed.acceleration == pytest.approx(E_MAX) and speed.vertical_rate == 0.0


def test_vertical_priority_gives_up_climb_to_respect_the_speed_floor():
    result = allocate(AllocationPolicy.VERTICAL, -2.0, 15.0, 0.0, 15.0,
                      minimum_acceleration=0.5)
    assert result.acceleration == pytest.approx(0.5)
    assert result.vertical_rate == pytest.approx((E_MAX - 0.5) / K)
    assert_physical(result, 0.0, 15.0, floor=0.5)


def test_unreachable_floor_flies_the_lowest_permitted_rate():
    result = allocate(AllocationPolicy.VERTICAL, 0.0, 15.0, 5.0, 15.0,
                      minimum_acceleration=2.0)
    assert result.vertical_rate == 5.0
    assert result.acceleration == pytest.approx(E_MAX - K * 5.0)


def test_joint_requires_explicit_positive_weights():
    with pytest.raises(EvaluationError, match='explicit weights'):
        allocate(AllocationPolicy.JOINT, 0.0, 5.0, 0.0, 5.0)
    with pytest.raises(EvaluationError, match='finite and positive'):
        allocate(AllocationPolicy.JOINT, 0.0, 5.0, 0.0, 5.0, weights=JointWeights(1.0, 0.0))


@pytest.mark.parametrize(('requested', 'reference', 'low', 'high', 'floor'), [
    (2.0, 15.0, 0.0, 15.0, -np.inf), (-2.0, -15.0, -15.0, 0.0, -np.inf),
    (0.3, 12.0, 0.0, 12.0, 0.1), (-0.6, 3.0, 0.0, 3.0, -np.inf), (0.0, 0.0, 0.0, 0.0, -np.inf)])
def test_joint_is_the_exact_constrained_optimum(requested, reference, low, high, floor):
    weights = JointWeights(2.0, 0.05)
    result = allocate(AllocationPolicy.JOINT, requested, reference, low, high,
                      minimum_acceleration=floor, weights=weights)
    assert_physical(result, low, high, floor)

    def cost(a, w):
        return weights.acceleration * (a - requested) ** 2 + weights.vertical * (w - reference) ** 2

    best = np.inf
    for w in np.linspace(low, high, 401):
        a_low = max(E_IDLE - K * w, floor)
        a_high = E_MAX - K * w
        if a_low <= a_high:
            best = min(best, cost(float(np.clip(requested, a_low, a_high)), w))
    assert cost(result.acceleration, result.vertical_rate) <= best + 1e-9


def test_joint_approaches_each_priority_as_its_weight_dominates():
    speed = allocate(AllocationPolicy.SPEED, 2.0, 15.0, 0.0, 15.0)
    vertical = allocate(AllocationPolicy.VERTICAL, 2.0, 15.0, 0.0, 15.0)
    toward_speed = allocate(AllocationPolicy.JOINT, 2.0, 15.0, 0.0, 15.0,
                            weights=JointWeights(1e8, 1.0))
    toward_vertical = allocate(AllocationPolicy.JOINT, 2.0, 15.0, 0.0, 15.0,
                               weights=JointWeights(1.0, 1e8))
    assert toward_speed.acceleration == pytest.approx(speed.acceleration, abs=1e-6)
    assert toward_speed.vertical_rate == pytest.approx(speed.vertical_rate, abs=1e-5)
    assert toward_vertical.acceleration == pytest.approx(vertical.acceleration, abs=1e-6)
    assert toward_vertical.vertical_rate == pytest.approx(vertical.vertical_rate, abs=1e-5)


def test_capability_follows_the_energy_equation_of_each_policy():
    state = dict(reference_vertical_rate=10.0, minimum_vertical_rate=0.0,
                 maximum_vertical_rate=10.0, **STATE)
    # Speed priority uses the most helpful permitted rate: level to accelerate,
    # full permitted climb to decelerate, a = (Thr - D)/m - (g0/V) w.
    up, down = acceleration_capability(AllocationPolicy.SPEED, 2.0, **state)
    assert (up, down) == pytest.approx((E_MAX, E_IDLE - K * 10.0))
    # Vertical priority keeps the climb: a = (Thr - D)/m - (g0/V) w.
    up, down = acceleration_capability(AllocationPolicy.VERTICAL, 2.0, **state)
    assert (up, down) == pytest.approx((E_MAX - K * 10.0, E_IDLE - K * 10.0))
    # Joint trades both errors: less acceleration than levelling would give,
    # but it keeps climbing (its reference) while it decelerates.
    up, down = acceleration_capability(AllocationPolicy.JOINT, 2.0, weights=WEIGHTS, **state)
    assert E_MAX - K * 10.0 < up < E_MAX
    assert down == pytest.approx(E_IDLE - K * 10.0)
    # A capability never exceeds the native request.
    up, down = acceleration_capability(AllocationPolicy.SPEED, 0.1, **state)
    assert (up, down) == pytest.approx((0.1, -0.1))


def tem(monkeypatch, target, policy=None, weights=None, maximum=70_000.0):
    """One-aircraft TEM step: fake model rate 16.3 m/s, guidance 5 m/s."""
    from tests.research.test_numerical_corrections import configure
    traf, model, perf = configure(monkeypatch, target=target)
    model.maximum = maximum
    perf.axmax = np.array([2.0])
    perf.energy_policy = np.array([AllocationPolicy.SPEED.value], dtype='U24')
    perf.joint_weight_acceleration = np.full(1, np.nan)
    perf.joint_weight_vertical = np.full(1, np.nan)
    perf.acceleration_capability_up = np.full(1, np.nan)
    perf.acceleration_capability_down = np.full(1, np.nan)
    if policy is not None:
        perf.configure_energy_policy(0, policy, weights)
    perf.update_dynamics(traf, 0.5)
    return traf, model, perf


def balance(traf, perf):
    supplied = (perf.thrust[0] - perf.drag[0]) * 200.0 / 60_000.0
    assert 200.0 * traf.speed_result.applied_acceleration[0] + g0 * traf.vs[0] == \
        pytest.approx(supplied, abs=1e-9)


def test_speed_priority_climbs_no_faster_than_guidance(monkeypatch):
    # At the target speed the model's 16.3 m/s rated-thrust climb is capped to
    # the 5 m/s guidance rate, so thrust drops below rated and fuel follows.
    traf, model, perf = tem(monkeypatch, target=200.0)
    assert traf.vs[0] == pytest.approx(5.0)
    assert perf.energy_allocation_policy[0] == 'SPEED_PRIORITY'
    assert perf.thrust[0] < perf.rated_thrust[0]
    assert model.last_fuel_thrust == pytest.approx(perf.thrust[0])
    balance(traf, perf)


def test_vertical_priority_holds_guidance_and_gives_speed_the_rest(monkeypatch):
    # 1 m/s2 of specific power: the 5 m/s climb costs k*5, speed gets the rest.
    speed, _, speed_perf = tem(monkeypatch, target=200.4)
    traf, _, perf = tem(monkeypatch, target=200.4, policy='VERTICAL')
    k = g0 / 200.0
    assert traf.vs[0] == pytest.approx(5.0)
    assert traf.speed_result.applied_acceleration[0] == pytest.approx(1.0 - 5.0 * k)
    assert perf.energy_allocation_policy[0] == 'VERTICAL_PRIORITY'
    assert speed.speed_result.applied_acceleration[0] == pytest.approx(0.8)
    assert speed.vs[0] == pytest.approx((1.0 - 0.8) / k)
    balance(traf, perf)


def test_joint_is_configured_with_explicit_weights_only(monkeypatch):
    with pytest.raises(ValueError, match='requires acceleration and vertical weights'):
        tem(monkeypatch, target=200.4, policy='JOINT')
    with pytest.raises(ValueError, match='takes no weights'):
        tem(monkeypatch, target=200.4, policy='SPEED', weights=(1.0, 1.0))
    traf, _, perf = tem(monkeypatch, target=200.4, policy='JOINT', weights=(1.0, 0.04))
    assert perf.energy_allocation_policy[0] == 'JOINT'
    assert 0.8 * (1.0 - 5.0 * g0 / 200.0) < traf.speed_result.applied_acceleration[0] < 0.8
    assert 1.0 / 0.4 < traf.vs[0] < 5.0
    balance(traf, perf)


@pytest.mark.parametrize('policy', ['SPEED', 'VERTICAL'])
def test_guidance_capability_follows_the_selected_policy(monkeypatch, policy):
    _, _, perf = tem(monkeypatch, target=200.0, policy=policy)
    k = g0 / 200.0
    up, down = perf.acceleration_limits()
    # Idle 0 N, drag 10 kN, maximum 70 kN at 60 t: E_idle = -1/6, E_max = 1.
    held = 0.0 if policy == 'SPEED' else 5.0
    assert up[0] == pytest.approx(1.0 - k * held)
    assert down[0] == pytest.approx(1.0 / 6.0 + k * 5.0)
    perf.dyn_mode[:] = 0
    assert perf.acceleration_limits()[0][0] == 2.0


@pytest.mark.parametrize('policy', ['SPEED', 'VERTICAL'])
def test_guidance_rate_holds_through_the_last_metre_of_capture(monkeypatch, policy):
    # 0.5 m below the target the recorded request is zero (1 m deadband), but
    # the climb must stay within guidance's 5 m/s and close the gap exactly.
    from tests.research.test_numerical_corrections import configure
    traf, model, perf = configure(monkeypatch, target=200.0, delta_alt=0.5)
    model.maximum = 70_000.0
    perf.axmax = np.array([2.0])
    perf.energy_policy = np.array([AllocationPolicy.SPEED.value], dtype='U24')
    perf.joint_weight_acceleration = perf.joint_weight_vertical = np.full(1, np.nan)
    perf.acceleration_capability_up = perf.acceleration_capability_down = np.full(1, np.nan)
    perf.configure_energy_policy(0, policy)
    perf.update_dynamics(traf, 0.02)
    assert traf.vs[0] == pytest.approx(min(5.0, 0.5 / 0.02))
    assert perf.requested_vertical_rate[0] == 0.0
    balance(traf, perf)
    perf.update_dynamics(traf, 0.2)
    assert traf.vs[0] == pytest.approx(0.5 / 0.2)


def test_capability_uses_the_weaker_deceleration_at_the_pending_target(monkeypatch):
    # Drag grows with TAS^2, so idle deceleration is weaker at the 150 m/s
    # target than at the current 200 m/s; guidance must plan with the weaker.
    from tests.research.test_numerical_corrections import ConsistentModel, configure

    class QuadraticDrag(ConsistentModel):
        def bluesky_energy(self, **state):
            values = super().bluesky_energy(**state)
            values['drag'] = 10_000.0 * (state['tas'] / 200.0) ** 2
            return values

    traf, _, perf = configure(monkeypatch, target=150.0, delta_alt=0.0)
    perf.models = [QuadraticDrag(maximum=70_000.0)]
    perf.axmax = np.array([2.0])
    perf.energy_policy = np.array([AllocationPolicy.SPEED.value], dtype='U24')
    perf.joint_weight_acceleration = perf.joint_weight_vertical = np.full(1, np.nan)
    perf.acceleration_capability_up = perf.acceleration_capability_down = np.full(1, np.nan)
    perf.update_dynamics(traf, 0.5)
    at_target = 10_000.0 * (150.0 / 200.0) ** 2 / 60_000.0
    assert perf.acceleration_limits()[1][0] == pytest.approx(at_target)
    assert at_target < 10_000.0 / 60_000.0

    def fail(*args, tas=None, **kwargs):
        if tas is not None:
            raise EvaluationError('auxiliary evaluation unavailable')
        return original(*args, **kwargs)

    original = perf._evaluate
    monkeypatch.setattr(perf, '_evaluate', fail)
    perf.update_dynamics(traf, 0.5)
    assert perf.acceleration_limits()[1][0] == pytest.approx(10_000.0 / 60_000.0)
