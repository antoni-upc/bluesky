"""Counterexamples from the paper audit, independent of licensed aircraft data."""
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest
import bluesky as bs
from bluesky.tools.aero import g0
from bluesky.traffic.traffic import Traffic
from bluesky.traffic.dynamics import SpeedStepRequest
from bluesky.plugins.pybada.model import EnergyResult, EvaluationError, _clamp_thrust
from tests.research.test_pybada_dynamics import performance, traffic


class ConsistentModel:
    def __init__(self, idle=0., maximum=200000.):
        self.idle, self.maximum = idle, maximum
        self.last_fuel_thrust = None

    def bluesky_energy(self, **state):
        mass, v = state['mass'], state['tas']
        q, e = 1., .8
        thrust, drag = 10000.+mass*q, 10000.
        w = q*v*e/g0
        return dict(thrust=thrust, rated_thrust=thrust, drag=drag, fuel_flow=thrust/100000.,
                    esf=e, rocd=w, acceleration=q*(1-e),
                    applied_acceleration=q*(1-e), applied_vertical_rate=w,
                    idle_thrust=self.idle, maximum_thrust=self.maximum,
                    required_thrust=thrust, allocation_policy='BADA_ESF')

    def bluesky_fuel(self, *, thrust, **state):
        self.last_fuel_thrust = thrust
        return thrust/100000.


def configure(monkeypatch, target=200.05, delta_alt=1000., idle=0.):
    traf=traffic();traf.aporasas.alt[:]=traf.alt+delta_alt
    traf.aporasas.tas[:]=target
    traf.speed_request=SpeedStepRequest(np.array([target]), np.array([(target-200.)/.5]),
                                        np.array([True]), np.array([target]))
    monkeypatch.setattr(bs,'traf',traf)
    model=ConsistentModel(idle=idle);perf=performance(model,strict=True)
    return traf,model,perf


def assert_balance(traf,perf,mass=60000.,tas=200.):
    supplied=(perf.thrust[0]-perf.drag[0])*tas/mass
    applied=tas*traf.speed_result.applied_acceleration[0]+g0*traf.vs[0]
    assert applied==pytest.approx(supplied,abs=1e-9)


@pytest.mark.parametrize('delta_alt',[1000.,.5])
def test_speed_and_combined_capture_close_applied_energy(monkeypatch,delta_alt):
    traf,model,perf=configure(monkeypatch,delta_alt=delta_alt)
    perf.update_dynamics(traf,.5)
    assert traf.speed_result.next_tas[0]==pytest.approx(200.05)
    assert_balance(traf,perf)
    assert model.last_fuel_thrust==pytest.approx(perf.thrust[0])
    assert perf.mass[0]==pytest.approx(60000.-perf.fuelflow[0]*.5)


def test_idle_bound_prevents_unphysical_exact_capture(monkeypatch):
    traf,model,perf=configure(monkeypatch,target=200.,delta_alt=.5,idle=30000.)
    perf.update_dynamics(traf,.5)
    assert perf.thrust[0]>=30000.
    assert not traf.speed_result.capture[0]
    assert traf.speed_result.next_tas[0]>200.
    assert_balance(traf,perf)


def test_tem_altitude_is_integrated_without_native_snap(monkeypatch):
    monkeypatch.setattr(bs,'sim',SimpleNamespace(simdt=.5))
    traf=SimpleNamespace(alt=np.array([1000.]),vs=np.array([1.]),swaltsel=np.array([False]),
        _vertical_dynamics_handled=np.array([True]),aporasas=SimpleNamespace(alt=np.array([1000.52])),
        lat=np.array([50.]),lon=np.array([0.]),gsnorth=np.array([0.]),gseast=np.array([0.]),
        gs=np.array([0.]),distflown=np.array([0.]))
    Traffic.update_pos(traf)
    assert traf.alt[0]==pytest.approx(1000.5)
    traf._vertical_dynamics_handled[:]=False
    Traffic.update_pos(traf)
    assert traf.alt[0]==pytest.approx(1000.52)


def test_rejected_fuel_decrement_holds_without_applying_motion(monkeypatch):
    traf,model,perf=configure(monkeypatch)
    perf.envelope_policy=np.array(['ENFORCE'])
    perf.envelope_checks=[()]
    perf.assign_mass=lambda *args,**kwargs:(False,'mass minimum reached')
    held=[]
    monkeypatch.setattr(bs,'sim',SimpleNamespace(hold=lambda:held.append(True)))
    monkeypatch.setattr('bluesky.stack.echo',lambda *args:None)
    perf.update_dynamics(traf,.5)
    assert held and perf.invalid[0]
    assert perf.mass[0]==60000.
    assert traf.vs[0]==0.


def test_inverted_thrust_bounds_are_rejected():
    with pytest.raises(EvaluationError): _clamp_thrust(20.,30.,10.)


@pytest.mark.parametrize('family',['3','4'])
@pytest.mark.parametrize('temperature',[245.,280.])
def test_non_isa_model_rate_is_converted_to_geometric_motion(family,temperature):
    from pathlib import Path
    import pyBADA
    from pyBADA import atmosphere as atm
    from bluesky.plugins.pybada.model import ModelStore
    path=Path(pyBADA.__file__).parent/'aircraft'/f'BADA{family}'/'DUMMY'
    model,_=ModelStore(family,str(path),version='3.15' if family=='3' else '4.2').resolve('A320')
    result=EnergyResult(**model.bluesky_energy(h=5000.,tas=200.,mass=60000.,
        temperature=temperature,pressure=54000.,phase='Climb',schedule='ICAO'))
    delta=atm.ISATemperatureDeviation(temperature=temperature,pressureAltitude=5000.)
    k=(temperature-delta)/temperature
    assert result.applied_vertical_rate==pytest.approx(result.rocd/k)
    assert abs(k-1)>0.01
    assert (result.thrust-result.drag)*200/60000==pytest.approx(
        200*result.applied_acceleration+g0*result.applied_vertical_rate,abs=1e-9)
    bounds=model.bluesky_vertical_envelope(h=5000.,tas=200.,mass=60000.,
        temperature=temperature,pressure=54000.,schedule='ICAO')
    assert bounds['maximum_rocd']==pytest.approx(result.applied_vertical_rate)


@pytest.mark.parametrize('family',['3','4'])
def test_rocd_uses_applied_thrust_after_limiting(monkeypatch,family):
    from pathlib import Path
    import pyBADA
    import bluesky.plugins.pybada.model as module
    path=Path(pyBADA.__file__).parent/'aircraft'/f'BADA{family}'/'DUMMY'
    model,_=module.ModelStore(family,str(path),version='3.15' if family=='3' else '4.2').resolve('A320')
    original=module._clamp_thrust
    # Synthetic actuator restriction tests ordering without licensed data.
    monkeypatch.setattr(module,'_clamp_thrust',lambda req,idle,maximum:original(req,idle,maximum*.8))
    result=EnergyResult(**model.bluesky_energy(h=5000.,tas=200.,mass=60000.,
        temperature=280.,pressure=54000.,phase='Climb',schedule='ICAO'))
    assert result.thrust_limited
    assert (result.thrust-result.drag)*200/60000==pytest.approx(
        200*result.applied_acceleration+g0*result.applied_vertical_rate,abs=1e-9)


@pytest.mark.parametrize('field',['applied_acceleration','applied_vertical_rate'])
def test_energy_result_rejects_nonfinite_applied_response(field):
    values=dict(thrust=1000.,rated_thrust=1000.,drag=100.,fuel_flow=.2,esf=1.,rocd=0.,acceleration=0.)
    values[field]=float('nan')
    with pytest.raises(EvaluationError): EnergyResult(**values).validate()
