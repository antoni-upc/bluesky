"""PyBADA longitudinal/vertical TEM plugin entry point."""

import bluesky as bs
from bluesky import stack
from bluesky.plugins.pybada import PyBadaTEM


def init_plugin():
    perf = PyBadaTEM()
    try:
        perf.activate()
    except Exception as exc:
        raise ImportError(f'PYBADATEM unavailable: {exc}') from exc
    PyBadaTEM.select(perf)
    stack.echo(f'PYBADATEM active: BADA {perf.family}, data={perf.store.data_path}, strict={perf.strict}')
    return {'plugin_name': 'PYBADATEM', 'plugin_type': 'sim'}


@stack.command(name='PERFMODEL')
def perfmodel(model: str = ''):
    perf = PyBadaTEM.implinstance()
    if not model:
        return True, f'Active pyBADA family: BADA{perf.family}'
    perf.activate(model.upper())
    return True, f'Active pyBADA family: BADA{perf.family}'


_DYNAMICS_MODES = {
    'KINEMATIC': 0,
    'TEM': 1,
}
_DYNAMICS_NAMES = {0: 'KINEMATIC', 1: 'TEM'}


def _parse_dynamics_mode(value):
    try:
        return _DYNAMICS_MODES[str(value).upper().strip()]
    except KeyError as exc:
        raise ValueError('mode must be KINEMATIC or TEM') from exc


@stack.command(name='DYNAMICS', annotations='[txt],[txt]')
def dynamics(acid=None, mode=None):
    """DYNAMICS [acid] [KINEMATIC|TEM] controls longitudinal/vertical motion.

    KINEMATIC uses BlueSky's native commanded-speed and vertical-speed model.
    TEM integrates pyBADA thrust, drag, fuel flow, acceleration, and ROCD while
    retaining BlueSky's lateral guidance.
    """
    perf = PyBadaTEM.implinstance()
    if acid is None:
        if not bs.traf.id:
            return True, 'DYNAMICS: no aircraft'
        return True, '\n'.join(
            f'{name}: {_DYNAMICS_NAMES[int(value)]}'
            for name, value in zip(bs.traf.id, perf.dyn_mode))
    if mode is None:
        mode, idx = acid, slice(None)
    else:
        idx = bs.traf.id2idx(acid)
        if idx < 0:
            return False, f'Aircraft {acid} not found'
    try:
        value = _parse_dynamics_mode(mode)
    except ValueError as exc:
        return False, f'DYNAMICS {exc}'
    perf.dyn_mode[idx] = value
    target = 'all aircraft' if isinstance(idx, slice) else bs.traf.id[idx]
    return True, f'{target}: dynamics set to {_DYNAMICS_NAMES[value]}'


@stack.command(name='SPDSCHED')
def spdsched(schedule: str = ''):
    perf = PyBadaTEM.implinstance()
    if not schedule:
        return True, f'Active speed schedule: {perf.schedule}'
    value = schedule.upper()
    if value not in ('ICAO', 'CONSCAS'):
        return False, 'SPDSCHED must be ICAO or CONSCAS'
    perf.schedule = value
    return True, f'Speed schedule set to {value}'


@stack.command(name='MASS')
def mass(acid: 'acid', mass_kg: float):
    if mass_kg <= 0:
        return False, 'MASS must be positive'
    perf = PyBadaTEM.implinstance()
    perf.mass[acid] = mass_kg
    perf.mass_override[acid] = True
    return True, f'{bs.traf.id[acid]} mass set to {mass_kg:.1f} kg'


@stack.command(name='PERFSTATUS', annotations='[txt]')
def perfstatus(acid=None):
    """Report model resolution, dynamics mode, validity, and miss count."""
    perf = PyBadaTEM.implinstance()
    if acid is None:
        indices = range(len(bs.traf.id))
    else:
        idx = bs.traf.id2idx(acid)
        if idx < 0:
            return False, f'Aircraft {acid} not found'
        indices = (idx,)
    if not bs.traf.id:
        return True, f'PYBADATEM BADA{perf.family}: no aircraft'
    lines = []
    for idx in indices:
        resolution = perf.resolutions[idx]
        lines.append(
            f'{bs.traf.id[idx]}: BADA{perf.family}/{resolution.resolved} '
            f'({resolution.method}), dynamics={_DYNAMICS_NAMES[int(perf.dyn_mode[idx])]}, '
            f'valid={not bool(perf.invalid[idx])}, misses={int(perf.failure_count[idx])}')
    return True, '\n'.join(lines)
