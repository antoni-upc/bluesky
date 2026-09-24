"""Exact memoisation of pure pyBADA adapter evaluations (E1)."""

import math

import pytest

import bluesky.plugins.pybada.model as model_module
from bluesky.plugins.pybada.model import _memoised


class Adapter:
    def __init__(self):
        self.calls = 0

    @_memoised
    def evaluate(self, **state):
        self.calls += 1
        if state.get('fail'):
            raise ValueError('model failure')
        return {'value': state['h'] * 2.0, 'sign': math.copysign(1.0, state['h'])}


def test_identical_calls_evaluate_once_and_return_copies():
    adapter = Adapter()
    first = adapter.evaluate(h=1000.0, phase='Climb')
    first['value'] = -1.0
    second = adapter.evaluate(phase='Climb', h=1000.0)
    assert second == {'value': 2000.0, 'sign': 1.0}
    assert adapter.calls == 1
    adapter.evaluate(h=1000.0, phase='Cruise')
    assert adapter.calls == 2


def test_signed_zero_nan_and_failures_are_never_confused():
    adapter = Adapter()
    assert adapter.evaluate(h=0.0)['sign'] == 1.0
    assert adapter.evaluate(h=-0.0)['sign'] == -1.0
    adapter.evaluate(h=float('nan'))
    adapter.evaluate(h=float('nan'))
    assert adapter.calls == 4
    for _ in range(2):
        with pytest.raises(ValueError):
            adapter.evaluate(h=1.0, fail=True)
    assert adapter.calls == 6


def test_store_is_bounded_least_recently_used(monkeypatch):
    monkeypatch.setattr(model_module, 'MEMO_SIZE', 2)
    adapter = Adapter()
    adapter.evaluate(h=1.0)
    adapter.evaluate(h=2.0)
    adapter.evaluate(h=1.0)          # refreshes h=1
    adapter.evaluate(h=3.0)          # evicts h=2
    adapter.evaluate(h=1.0)
    assert adapter.calls == 3
    adapter.evaluate(h=2.0)
    assert adapter.calls == 4


def test_envelope_adapters_are_memoised():
    assert hasattr(model_module.BadaModelAdapter.bluesky_envelope, '__wrapped__')
    assert hasattr(model_module.BadaModelAdapter.bluesky_lateral_envelope, '__wrapped__')
