#!/usr/bin/env python3
"""Check recorder TEM energy, applied motion and fuel against evaluation state.

This tests implemented point-mass consistency, not observed-flight accuracy.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from tests.research.schema_compat import SCHEMA_VERSION

G = 9.80665
TOLERANCES = {'energy_w_kg': 1e-7, 'acceleration_m_s2': 1e-9,
              'altitude_step_m': 1e-6, 'mass_kg': 1e-7, 'thrust_n': .2}


def validate(rows):
    errors=[]; samples=0; previous={}; strata={}; missing_idle=0
    def error(message):
        if len(errors)<100: errors.append(message)
    def number(row,name):
        x=float(row[name])
        if not math.isfinite(x): raise ValueError(f'non-finite {name}')
        return x
    for row in rows:
        if row.get('dynamics_mode')!='TEM': continue
        samples+=1
        try:
            if row['schema_version']!=SCHEMA_VERSION: raise ValueError(f'exact evaluation state requires {SCHEMA_VERSION}')
            if row['performance_valid']!='True': raise ValueError('invalid performance')
            if row['atmosphere_valid']!='True': raise ValueError('invalid atmosphere')
            t=number(row,'sim_time_s');acid=row['acid']
            if acid in previous and t<=previous[acid]: raise ValueError('non-increasing aircraft timestamp')
            previous[acid]=t
            dt=number(row,'evaluation_timestep_s');v=number(row,'evaluation_tas_m_s')
            mass=number(row,'evaluation_mass_kg');z=number(row,'evaluation_alt_m')
            if min(dt,v,mass)<=0: raise ValueError('non-positive evaluation state')
            a=number(row,'applied_acceleration_m_s2');w=number(row,'applied_vertical_rate_m_s')
            thrust=number(row,'thrust_n');drag=number(row,'drag_n')
            power=(thrust-drag)*v/mass
            residual=power-v*a-G*w
            da=(number(row,'tas_m_s')-v)/dt-a
            dz=number(row,'geometric_alt_m')-z-w*dt
            dm=number(row,'mass_kg')-mass+number(row,'fuel_flow_kg_s')*dt
            if number(row,'fuel_flow_kg_s')<0: raise ValueError('negative fuel flow')
            if thrust>number(row,'maximum_thrust_n')+TOLERANCES['thrust_n']: error(f'{acid}@{t}: above maximum thrust')
            idle=row.get('idle_thrust_n','')
            if idle and math.isfinite(float(idle)):
                if thrust<float(idle)-TOLERANCES['thrust_n']: error(f'{acid}@{t}: below idle thrust')
            else: missing_idle+=1
            values={'energy_w_kg':residual,'acceleration_m_s2':da,'altitude_step_m':dz,'mass_kg':dm}
            for name,value in values.items():
                if abs(value)>TOLERANCES[name]: error(f'{acid}@{t}: {name}={value:.12g}')
            key='|'.join([row['energy_allocation_policy'], 'capture='+row['speed_capture'],
                          'limited='+row['thrust_limited'], 'atmosphere='+row['atmosphere_source']])
            entry=strata.setdefault(key,{'samples':0,'aircraft_seconds':0.,'residuals':{k:[] for k in values}})
            entry['samples']+=1;entry['aircraft_seconds']+=dt
            for name,value in values.items(): entry['residuals'][name].append(value)
        except (ValueError,KeyError,TypeError) as exc: error(f'row {samples}: {exc}')
    if not samples: error('No TEM samples')
    for entry in strata.values():
        summary={}
        for name,values in entry.pop('residuals').items():
            absolute=sorted(abs(x) for x in values)
            summary[name]={'mean_signed':sum(values)/len(values),'max_abs':absolute[-1],
                           'p95_abs':absolute[math.ceil(.95*len(values))-1]}
        entry['metrics']=summary
    return {'passed':not errors,'samples':samples,'valid_evaluations':sum(x['samples'] for x in strata.values()),
            'errors':errors,'error_list_limit':100,'missing_idle_bound_samples':missing_idle,
            'tolerances':TOLERANCES,'strata':strata,
            'scope':'Differential geometric point-mass balance and one-step state/mass updates; not finite-step energy conservation or physical accuracy'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with args.samples.open() as stream: result=validate(csv.DictReader(stream))
    with args.samples.open('rb') as stream: result['samples_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    result['analysis_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(f"Numerical audit: passed={result['passed']}, samples={result['samples']}, errors={len(result['errors'])}")
    return 0 if result['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
