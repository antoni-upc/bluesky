"""PyBADA longitudinal/vertical TEM plugin entry point."""

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.plugins.pybada import PyBadaTEM
from bluesky.plugins.pybada.envelope import (EnvelopePolicy, EnvelopeProfile,
    expand_checks, parse_checks, parse_policy, parse_profile)
from bluesky.stack.cmdparser import CommandRejected


def init_plugin():
    perf = PyBadaTEM()
    try:
        perf.activate()
    except Exception as exc:
        raise ImportError(f'PYBADATEM unavailable: {exc}') from exc
    PyBadaTEM.select(perf)
    stack.echo(f'PYBADATEM active: BADA {perf.version}, data={perf.store.data_path}, strict={perf.strict}')
    return {'plugin_name': 'PYBADATEM', 'plugin_type': 'sim'}


@stack.command(name='PERFMODEL')
def perfmodel(model: str = ''):
    perf = PyBadaTEM.implinstance()
    if not model:
        return True, f'Active pyBADA dataset: BADA {perf.version}'
    try:
        perf.activate(model.upper())
    except Exception as exc:
        return False, f'PERFMODEL unchanged: {exc}'
    return True, f'Active pyBADA dataset: BADA {perf.version}'


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
    perf = PyBadaTEM.implinstance()
    aircraft = bs.traf.id[acid]
    if not np.isfinite(mass_kg) or mass_kg <= 0:
        return False, CommandRejected(
            f'{aircraft}: MASS {mass_kg!r} kg rejected; reason=mass must be finite and positive; '
            f'policy={getattr(perf, "envelope_policy", ["OFF"])[acid]}, '
            f'preserved={perf.mass[acid]:.1f} kg')
    success, reason = perf.assign_mass(acid, mass_kg)
    if not success:
        bounds = perf.bounds(acid)
        bounds_text = ('unknown' if not bounds.known else
                       f'{bounds.minimum:.1f}..{bounds.maximum:.1f} kg')
        return False, CommandRejected(
            f'{aircraft}: MASS {mass_kg:.1f} kg rejected; '
            f'policy={perf.envelope_policy[acid]}, reason={reason}, '
            f'bounds={bounds_text}, preserved={perf.mass[acid]:.1f} kg')
    if perf.envelope_last_action[acid] == 'ABORTED':
        return True, (f'{aircraft}: MASS {mass_kg:.1f} kg applied; quality event emitted, '
                      'simulation placed in HOLD')
    return True, f'{aircraft} mass set to {mass_kg:.1f} kg'


def _checks_text(checks):
    return ','.join(check.value for check in checks) or 'none'


@stack.command(name='ENVELOPE', annotations='[txt],[txt]')
def envelope(acid=None, policy=None):
    """Inspect or set independent per-aircraft BADA envelope policy."""
    perf = PyBadaTEM.implinstance()
    if acid is None:
        lines = [f'default: {str(bs.settings.pybada_envelope_policy).upper()}']
        lines.extend(f'{name}: {perf.envelope_policy[idx]}'
                     for idx, name in enumerate(bs.traf.id))
        return True, '\n'.join(lines)
    # A policy without an aircraft changes only the creation default.
    if policy is None:
        try:
            default = parse_policy(acid)
        except ValueError:
            idx = bs.traf.id2idx(acid)
            if idx < 0:
                return False, f'Aircraft {acid} not found'
            return True, f'{bs.traf.id[idx]}: {perf.envelope_policy[idx]}'
        bs.settings.pybada_envelope_policy = default.value
        return True, f'Default envelope policy set to {default.value}'
    idx = bs.traf.id2idx(acid)
    if idx < 0:
        return False, f'Aircraft {acid} not found'
    try:
        value = parse_policy(policy)
    except ValueError as exc:
        return False, f'ENVELOPE {exc}'
    success, reason = perf.configure_envelope(idx, policy=value)
    if not success:
        return False, f'ENVELOPE unchanged: {reason}'
    return True, f'{bs.traf.id[idx]}: envelope policy set to {value.value}'


@stack.command(name='ENVELOPECHECKS')
def envelopechecks(acid: str, profile: str = '', *checks):
    """ENVELOPECHECKS acid [CORE_ONLY|LONGITUDINAL|FULL|CUSTOM checks...]."""
    perf = PyBadaTEM.implinstance()
    idx = bs.traf.id2idx(acid)
    if idx < 0:
        return False, f'Aircraft {acid} not found'
    if not profile:
        return True, (f'{bs.traf.id[idx]}: profile={perf.envelope_profile[idx]}, '
                      f'checks={_checks_text(perf.envelope_checks[idx])}')
    try:
        selected_profile = parse_profile(profile)
        if selected_profile == EnvelopeProfile.CUSTOM:
            selected = parse_checks(checks)
        else:
            if checks:
                raise ValueError(f'{selected_profile.value} does not accept explicit checks')
            selected = expand_checks(selected_profile)
    except ValueError as exc:
        return False, f'ENVELOPECHECKS unchanged: {exc}'
    success, reason = perf.configure_envelope(
        idx, profile=selected_profile, checks=selected)
    if not success:
        return False, f'ENVELOPECHECKS unchanged: {reason}'
    return True, (f'{bs.traf.id[idx]}: profile={selected_profile.value}, '
                  f'checks={_checks_text(selected)}')


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
        return True, f'PYBADATEM BADA {perf.version}: no aircraft'
    lines = []
    for idx in indices:
        resolution = perf.resolutions[idx]
        policy = getattr(perf, 'envelope_policy', ['OFF'] * len(bs.traf.id))[idx]
        checks = getattr(perf, 'envelope_checks', [()] * len(bs.traf.id))[idx]
        status = getattr(perf, 'envelope_status', ['VALID'] * len(bs.traf.id))[idx]
        action = getattr(perf, 'envelope_last_action', ['NONE'] * len(bs.traf.id))[idx]
        reason = getattr(perf, 'envelope_last_reason', [''] * len(bs.traf.id))[idx]
        events = getattr(perf, 'envelope_event_count', [0] * len(bs.traf.id))[idx]
        violations = getattr(perf, 'envelope_violation_count', [0] * len(bs.traf.id))[idx]
        bounds = perf.bounds(idx) if hasattr(perf, 'bounds') else None
        bounds_text = ('unknown' if bounds is None or not bounds.known else
                       f'{bounds.minimum:.1f}..{bounds.maximum:.1f} kg')
        flight = perf.flight_bounds(idx) if hasattr(perf, 'flight_bounds') else None
        def value_text(value, digits):
            return 'unknown' if value is None or not np.isfinite(value) else f'{value:.{digits}f}'
        flight_text = ('unknown' if flight is None else
                       f'CAS={value_text(flight.minimum_cas, 1)}..'
                       f'{value_text(flight.maximum_cas, 1)} m/s, '
                       f'Mach={value_text(flight.minimum_mach, 3)}..'
                       f'{value_text(flight.maximum_mach, 3)}, '
                       f'hmax={value_text(flight.maximum_altitude, 1)} m, '
                       f'config={flight.configuration or "unknown"}')
        vertical = perf.vertical_bounds(idx) if hasattr(perf, 'vertical_bounds') else None
        rod_max = (None if vertical is None or vertical.minimum_rocd is None
                   else abs(vertical.minimum_rocd))
        vertical_text = ('unknown' if vertical is None else
                         f'ROC_MAX={value_text(vertical.maximum_rocd, 2)} m/s, '
                         f'ROD_MAX={value_text(rod_max, 2)} m/s')
        lateral = perf.lateral_bounds(idx) if hasattr(perf, 'lateral_bounds') else None
        bank = perf.effective_bank_angle(idx) if hasattr(perf, 'effective_bank_angle') else None
        load = (None if bank is None or not np.isfinite(bank) or abs(bank) >= 90.0
                else 1.0 / np.cos(np.radians(abs(bank))))
        lateral_text = ('unknown' if lateral is None else
                        f'config={lateral.configuration or "unknown"}, '
                        f'BANK_MAX={value_text(lateral.maximum_bank_angle_deg, 2)} deg, '
                        f'LOAD_FACTOR={value_text(lateral.minimum_load_factor, 2)}..'
                        f'{value_text(lateral.maximum_load_factor, 2)}, '
                        f'current_bank={value_text(bank, 2)} deg, '
                        f'current_load={value_text(load, 3)}')
        lines.append(
            f'{bs.traf.id[idx]}: BADA {perf.version}/{resolution.resolved} '
            f'({resolution.method}), dynamics={_DYNAMICS_NAMES[int(perf.dyn_mode[idx])]}, '
            f'mass={perf.mass[idx]:.1f} kg, '
            f'valid={not bool(perf.invalid[idx])}, misses={int(perf.failure_count[idx])}, '
            f'envelope={policy}/{status}, checks={_checks_text(checks)}, bounds={bounds_text}, '
            f'flight_bounds={flight_text}, '
            f'vertical_bounds={vertical_text}, '
            f'lateral_bounds={lateral_text}, '
            f'last={action}/{reason or "-"}, events={int(events)}, violations={int(violations)}')
    return True, '\n'.join(lines)
