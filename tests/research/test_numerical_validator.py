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


def aborted_mass_command():
    before=sample()
    after={**sample(), 'sim_time_s':'2', 'evaluation_mass_kg':'999.5',
           'mass_kg':'1500', 'thrust_n':'1099.5', 'mass_max_kg':'1200',
           'envelope_policy':'ABORT',
           'envelope_status':'INFEASIBLE', 'envelope_last_action':'ABORTED',
           'envelope_last_reason':'MASS_MAX'}
    event={'component':'PYBADATEM', 'action':'ABORTED', 'continuation':'STOP',
           'policy':'ABORT', 'reason':'MASS_MAX', 'aircraft':'T1',
           'sim_time_s':2.0, 'requested':1500.0, 'applied':1500.0}
    return before,after,event


def test_terminal_mass_command_exempts_only_mass_integration():
    before,after,event=aborted_mass_command()
    audit=validate([before,after], [event])
    assert audit['passed'] and audit['external_mass_command_samples']==1
    after['drag_n']='200'
    broken=validate([before,after], [event])
    assert not broken['passed']
    assert any('energy_w_kg' in error for error in broken['errors'])


@pytest.mark.parametrize('change', ['missing_event','wrong_mass','wrong_time',
                                     'wrong_action','not_terminal'])
def test_mass_command_exemption_requires_matching_terminal_abort(change):
    before,after,event=aborted_mass_command()
    rows=[before,after]
    events=[event]
    if change=='missing_event': events=[]
    elif change=='wrong_mass': event['applied']=1499.0
    elif change=='wrong_time': event['sim_time_s']=3.0
    elif change=='wrong_action': event['action']='ACCEPTED'
    else:
        rows.append({**sample(),'sim_time_s':'3','evaluation_mass_kg':'1500',
                     'mass_kg':'1499.5'})
    audit=validate(rows,events)
    assert not audit['passed']
    assert any('mass_kg' in error for error in audit['errors'])
