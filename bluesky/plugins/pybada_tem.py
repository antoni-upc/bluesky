"""PyBADA longitudinal/vertical TEM plugin entry point."""

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.plugins.pybada import PyBadaTEM
from bluesky.plugins.pybada.model import parse_configuration_mode
from bluesky.plugins.pybada.envelope import (EnvelopePolicy, EnvelopeProfile,
    expand_checks, parse_checks, parse_policy, parse_profile)
from bluesky.stack.cmdparser import CommandRejected
from bluesky.tools.aero import ft


def init_plugin():
    perf = PyBadaTEM()
    # Loading the plugin must not require the configured default family when
    # the scenario will explicitly select another family before creating
    # traffic. Existing traffic still requires immediate, transactional model
    # resolution; otherwise activation is deferred to PERFMODEL or CRE.
    if bs.traf.id:
        try:
            perf.activate()
        except Exception as exc:
            raise ImportError(f'PYBADATEM unavailable: {exc}') from exc
    PyBadaTEM.select(perf)
    if perf.store is None:
        stack.echo('PYBADATEM loaded: no dataset selected; use PERFMODEL BADA3 or BADA4')
    else:
        stack.echo(
            f'PYBADATEM active: BADA {perf.version}, '
            f'data={perf.store.data_path}, strict={perf.strict}')
    return {'plugin_name': 'PYBADATEM', 'plugin_type': 'sim'}


@stack.command(name='PERFMODEL')
def perfmodel(model: str = ''):
    perf = PyBadaTEM.implinstance()
    if not model:
        return True, ('No active pyBADA dataset' if perf.store is None else
                      f'Active pyBADA dataset: BADA {perf.version}')
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


@stack.command(name='BADACONFIG', annotations='[txt],[txt]')
def badaconfig(acid=None, mode=None):
    """BADACONFIG [acid] [CRUISE|PYBADA] selects BADA configuration source."""
    perf = PyBadaTEM.implinstance()
    if acid is None:
        if not bs.traf.id:
            return True, 'BADACONFIG: no aircraft'
        return True, '\n'.join(
            f'{name}: {perf.bada_configuration_mode[idx]}'
            for idx, name in enumerate(bs.traf.id))
    idx = bs.traf.id2idx(acid)
    if idx < 0:
        return False, f'Aircraft {acid} not found'
    if mode is None:
        return True, f'{bs.traf.id[idx]}: {perf.bada_configuration_mode[idx]}'
    try:
        value = parse_configuration_mode(mode)
    except ValueError as exc:
        return False, f'BADACONFIG {exc}'
    success, reason = perf.configure_bada_configuration(idx, value)
    if not success:
        return False, f'BADACONFIG unchanged: {reason}'
    return True, (f'{bs.traf.id[idx]}: BADA configuration mode set to '
                  f'{value.value}')


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


@stack.command(name='PERFSTATUS', annotations='[txt],[txt]')
def perfstatus(acid=None, view=None):
    """PERFSTATUS [acid] [CURRENT|BOUNDS|ALL] reports grouped performance."""
    perf = PyBadaTEM.implinstance()
    views = {'CURRENT', 'BOUNDS', 'ALL'}
    aliases = {'MAXS': 'BOUNDS', 'MAX': 'BOUNDS'}
    if acid is not None and str(acid).upper() in views | set(aliases):
        if view is not None:
            return False, 'PERFSTATUS accepts one view: CURRENT, BOUNDS, or ALL'
        view, acid = acid, None
    selected_view = aliases.get(str(view or 'ALL').upper(), str(view or 'ALL').upper())
    if selected_view not in views:
        return False, 'PERFSTATUS view must be CURRENT, BOUNDS, or ALL'
    if acid is None:
        indices = range(len(bs.traf.id))
    else:
        idx = bs.traf.id2idx(acid)
        if idx < 0:
            return False, f'Aircraft {acid} not found'
        indices = (idx,)
    if not bs.traf.id:
        return True, f'PYBADATEM BADA {perf.version}: no aircraft'
    reports = []
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
        flight = perf.flight_bounds(idx) if hasattr(perf, 'flight_bounds') else None
        def value_text(value, digits):
            try:
                return ('unknown' if value is None or not np.isfinite(value)
                        else f'{value:.{digits}f}')
            except TypeError:
                return 'unknown'
        def altitude_text(value):
            try:
                if value is None or not np.isfinite(value):
                    return 'unknown'
                altitude_ft = float(value) / ft
                return (f'{float(value):.1f} m '
                        f'({altitude_ft:.0f} ft/FL{altitude_ft / 100.0:03.0f})')
            except TypeError:
                return 'unknown'
        def array_value(owner, name, default=None):
            values = getattr(owner, name, ())
            return values[idx] if idx < len(values) else default
        vertical = perf.vertical_bounds(idx) if hasattr(perf, 'vertical_bounds') else None
        rod_max = (None if vertical is None or vertical.minimum_rocd is None
                   else abs(vertical.minimum_rocd))
        lateral = perf.lateral_bounds(idx) if hasattr(perf, 'lateral_bounds') else None
        bank = perf.effective_bank_angle(idx) if hasattr(perf, 'effective_bank_angle') else None
        load = (None if bank is None or not np.isfinite(bank) or abs(bank) >= 90.0
                else 1.0 / np.cos(np.radians(abs(bank))))
        config = (getattr(lateral, 'configuration', '') or
                  getattr(flight, 'configuration', '') or 'unknown')
        mode = array_value(perf, 'bada_configuration_mode', 'PYBADA')
        dynamics_mode = _DYNAMICS_NAMES.get(int(array_value(perf, 'dyn_mode', 0)), 'unknown')
        lines = [f'Performance on {bs.traf.id[idx]} {resolution.resolved}:',
                 f'Model: BADA {perf.version} ({resolution.method})  '
                 f'Dynamics: {dynamics_mode}  Configuration mode: {mode}']
        if selected_view in ('CURRENT', 'ALL'):
            lines.extend((
                'CURRENT',
                f'  State: mass={value_text(array_value(perf, "mass"), 1)} kg  '
                f'CAS={value_text(array_value(bs.traf, "cas"), 1)} m/s  '
                f'Mach={value_text(array_value(bs.traf, "M"), 3)}  '
                f'alt={altitude_text(array_value(bs.traf, "alt"))}  '
                f'VS={value_text(array_value(bs.traf, "vs"), 2)} m/s',
                f'  Aero: config={config}  HLid={value_text(getattr(lateral, "high_lift_id", None), 0)}  '
                f'gear={getattr(lateral, "landing_gear", "") or "unknown"}  '
                f'bank={value_text(bank, 2)} deg  load={value_text(load, 3)}',
                f'  Forces: thrust={value_text(array_value(perf, "thrust"), 1)} N  '
                f'rated={value_text(array_value(perf, "rated_thrust"), 1)} N  '
                f'drag={value_text(array_value(perf, "drag"), 1)} N  '
                f'fuel={value_text(array_value(perf, "fuelflow"), 3)} kg/s'))
        if selected_view in ('BOUNDS', 'ALL'):
            mass_text = ('unknown' if bounds is None or not bounds.known else
                         f'{bounds.minimum:.1f}..{bounds.maximum:.1f} kg')
            lateral_source = (
                'source=GPF-civilian/derived' if str(getattr(perf, 'family', '')) == '3'
                else f'DLM={getattr(lateral, "minimum_limit_name", "") or "unknown"}/'
                     f'{getattr(lateral, "maximum_limit_name", "") or "unknown"}')
            lines.extend((
                'BOUNDS',
                f'  Mass: {mass_text}',
                f'  Flight: CAS={value_text(getattr(flight, "minimum_cas", None), 1)}..'
                f'{value_text(getattr(flight, "maximum_cas", None), 1)} m/s  '
                f'Mach={value_text(getattr(flight, "minimum_mach", None), 3)}..'
                f'{value_text(getattr(flight, "maximum_mach", None), 3)}  '
                f'alt_max={altitude_text(getattr(flight, "maximum_altitude", None))}',
                f'  Vertical: ROC_MAX={value_text(getattr(vertical, "maximum_rocd", None), 2)} m/s  '
                f'ROD_MAX={value_text(rod_max, 2)} m/s',
                f'  Lateral: {lateral_source}  '
                f'load={value_text(getattr(lateral, "minimum_load_factor", None), 2)}..'
                f'{value_text(getattr(lateral, "maximum_load_factor", None), 2)}  '
                f'bank_max={value_text(getattr(lateral, "maximum_bank_angle_deg", None), 2)} deg'))
        if selected_view == 'ALL':
            lines.extend((
                'QUALITY',
                f'  Envelope: {policy}/{status}  checks={_checks_text(checks)}',
                f'  Last: {action}/{reason or "-"}  events={int(events)}  '
                f'violations={int(violations)}  valid={not bool(array_value(perf, "invalid", False))}  '
                f'misses={int(array_value(perf, "failure_count", 0))}'))
        reports.append('\n'.join(lines))
    return True, '\n\n'.join(reports)
