import pytest
from tests.research.schema_compat import SCHEMA_VERSION
from tests.research.validate_numerical_run import validate,G


def sample():
    return {'schema_version':SCHEMA_VERSION,'dynamics_mode':'TEM','performance_valid':'True',
        'atmosphere_valid':'True','acid':'T1','sim_time_s':'1','evaluation_timestep_s':'1',
        'evaluation_tas_m_s':'100','evaluation_mass_kg':'1000','evaluation_alt_m':'1000',
        'tas_m_s':'100.5','mass_kg':'999.5','geometric_alt_m':str(1000+50/G),
        'applied_acceleration_m_s2':'.5','applied_vertical_rate_m_s':str(50/G),
        'thrust_n':'1100','drag_n':'100','maximum_thrust_n':'1200','idle_thrust_n':'0',
        'fuel_flow_kg_s':'.5','energy_allocation_policy':'BADA_ESF','speed_capture':'False',
        'thrust_limited':'False','atmosphere_source':'ERA5','temperature_k':'300',
        'evaluation_temperature_k':'270','pressure_alt_m':'2000'}


def test_uses_evaluation_state_not_post_step_airdata():
    result=validate([sample()]);assert result['passed'] and result['valid_evaluations']==1


@pytest.mark.parametrize('field,value,reason',[
    ('applied_acceleration_m_s2','0','energy_w_kg'),
    ('geometric_alt_m','1005.2','altitude_step_m'),
    ('mass_kg','1000','mass_kg'),
    ('maximum_thrust_n','900','above maximum'),
    ('evaluation_tas_m_s','','could not convert'),
    ('schema_version','samples-v10',SCHEMA_VERSION)])
def test_rejects_independent_energy_snap_mass_bound_and_alignment_errors(field,value,reason):
    row=sample();row[field]=value;result=validate([row]);assert not result['passed']
    assert any(reason in error for error in result['errors'])


def test_strata_include_capture_and_limit_instead_of_filtering_them():
    row=sample();row['speed_capture']='True';row['thrust_limited']='True'
    result=validate([row]);assert result['passed']
    assert any('capture=True|limited=True' in key for key in result['strata'])
