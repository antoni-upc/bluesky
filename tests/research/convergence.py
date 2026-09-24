"""Timestep-convergence analysis shared by the gates and at-will studies.

Runs of one scenario at timesteps ``dt_1 > dt_2 > dt_3`` with a constant ratio
``r = dt_1 / dt_2 = dt_2 / dt_3`` are compared per aircraft on their common
recorder timestamps. For each field the maximum differences
``d12 = |u_1 - u_2|`` and ``d23 = |u_2 - u_3|`` give the observed order
``p = log(d12 / d23) / log(r)``; the explicit TEM integration is first order,
so ``p`` should be close to 1 where the differences exceed the field's noise
floor. A discrete event (speed capture, level-off) happens at a time known
only to within one step, and the error it leaves behind depends on where it
falls on each grid, so it does not shrink smoothly. The order is therefore
measured on the smooth segment before an aircraft's first event. Over the
whole run, where events leave noisy but converging offsets, the error against
the finest run must shrink at least as order ``WHOLE_RUN_MIN_ORDER`` predicts
from the coarsest to the middle timestep (for halving steps the reduction is
``2**p + 1``: 3 at first order, 2.41 at 0.5), and event times must converge. Fields
declared exact must agree to the noise floor at every timestep.
Discrete events (speed capture, level-off) must occur at
converging times, and an optional observable is extrapolated (Richardson)
to estimate its error at a production timestep.
"""

import csv
import math
from pathlib import Path

DEFAULT_FIELDS = ('geometric_alt_m', 'pressure_alt_m', 'tas_m_s', 'mass_kg',
                  'lat_deg', 'lon_deg')
# Resolution of a convergence claim: differences at or below these (about 1 cm
# of position or altitude, 0.1 mm/s, 10 g of fuel) are negligible and are not
# used for order estimates. Below them, competing error sources such as where a
# manoeuvre starts on each grid dominate and do not shrink smoothly.
NOISE_FLOOR = {'geometric_alt_m': 1e-2, 'pressure_alt_m': 1e-2, 'tas_m_s': 1e-4,
               'mass_kg': 1e-2, 'lat_deg': 1e-7, 'lon_deg': 1e-7}
ORDER_BAND = (0.7, 1.3)
WHOLE_RUN_MIN_ORDER = 0.5
EVENT_STEPS = 3
OBSERVABLES = ('fuel_burn_kg',) + tuple(f'final:{field}' for field in DEFAULT_FIELDS)


def dt_label(dt):
    """Stable file label: 0.10 s -> dt100, 0.025 s -> dt025."""
    return f'dt{round(dt * 1000):03d}'


def _number(row, field):
    value = float(row[field])
    if not math.isfinite(value):
        raise ValueError(f'{row.get("acid", "?")} {field} is non-finite')
    return value


def load(path):
    """Rows per aircraft keyed by a rounded simulation time."""
    aircraft = {}
    with Path(path).open(newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            aircraft.setdefault(row['acid'], {})[round(float(row['sim_time_s']), 6)] = row
    return aircraft


def _events(rows, times):
    """Times at which speed capture starts or a climb/descent levels off.

    The thrust-limitation flag is a diagnostic that chatters at a thrust bound;
    it marks no physical event and is not compared.
    """
    events = {'speed_capture': [], 'level_off': []}
    previous = None
    for time in times:
        row = rows[time]
        if previous is not None:
            if row.get('speed_capture') == 'True' and previous.get('speed_capture') != 'True':
                events['speed_capture'].append(time)
            if (abs(float(previous['vertical_speed_m_s'])) > 1e-6 and
                    abs(float(row['vertical_speed_m_s'])) <= 1e-6):
                events['level_off'].append(time)
        previous = row
    return events


def _observable(name, rows, times):
    if name == 'fuel_burn_kg':
        return _number(rows[times[0]], 'mass_kg') - _number(rows[times[-1]], 'mass_kg')
    if name.startswith('final:'):
        return _number(rows[times[-1]], name.split(':', 1)[1])
    raise ValueError(f'unknown observable {name!r}; expected one of {OBSERVABLES}')


def analyse(paths, *, fields=DEFAULT_FIELDS, exact=None, order_band=ORDER_BAND,
            event_steps=EVENT_STEPS, observable=None, production_dt=None, tolerance=None):
    """Compare runs ``{dt: csv_path}`` and return ``(errors, report)``.

    ``exact`` maps an aircraft to fields that must agree to the noise floor at
    every timestep. ``observable`` with ``production_dt`` and ``tolerance``
    bounds the extrapolated error of that observable at the production step.
    """
    exact = exact or {}
    dts = sorted(paths, reverse=True)
    if len(dts) != 3:
        raise ValueError('a convergence study needs exactly three timesteps')
    ratio = dts[0] / dts[1]
    if not math.isclose(ratio, dts[1] / dts[2], rel_tol=1e-6) or ratio <= 1.0:
        raise ValueError('timesteps must decrease with one constant ratio')
    runs = {dt: load(paths[dt]) for dt in dts}
    # Error-vs-finest reduction implied by the minimum whole-run order.
    required_reduction = ((dts[0] ** WHOLE_RUN_MIN_ORDER - dts[2] ** WHOLE_RUN_MIN_ORDER) /
                          (dts[1] ** WHOLE_RUN_MIN_ORDER - dts[2] ** WHOLE_RUN_MIN_ORDER))
    errors, report = [], {'timesteps': dts, 'ratio': ratio, 'aircraft': {}}
    aircraft = set.intersection(*(set(run) for run in runs.values()))
    for acid in sorted(set.union(*(set(run) for run in runs.values())) - aircraft):
        errors.append(f'{acid} is missing from at least one timestep run')
    for acid in sorted(aircraft):
        series = [runs[dt][acid] for dt in dts]
        times = sorted(set.intersection(*(set(rows) for rows in series)))
        if len(times) < 10:
            errors.append(f'{acid}: only {len(times)} common timestamps')
            continue
        cadence = min(b - a for a, b in zip(times, times[1:]))
        events = [_events(rows, times) for rows in series]
        window = event_steps * dts[0] + cadence
        first_event = min((time for run in events for kind in run.values() for time in kind),
                          default=math.inf)
        smooth = [i for i, time in enumerate(times) if time < first_event - window]
        entry = report['aircraft'][acid] = {'common_samples': len(times),
                                            'smooth_samples': len(smooth), 'fields': {},
                                            'events': {}}
        for field in fields:
            floor = NOISE_FLOOR.get(field, 0.0)
            values = [[_number(rows[time], field) for time in times] for rows in series]
            error_vs_finest = [max(abs(a - b) for a, b in zip(values[i], values[2]))
                               for i in range(3)]
            result = {'error_vs_finest': error_vs_finest, 'noise_floor': floor}
            if field in exact.get(acid, ()):
                worst = max(error_vs_finest[:2])
                result['status'] = 'exact' if worst <= floor else 'not exact'
                if result['status'] != 'exact':
                    errors.append(f'{acid} {field}: expected exact integration, '
                                  f'difference {worst:.3g} exceeds {floor}')
                entry['fields'][field] = result
                continue
            if error_vs_finest[1] > floor:
                reduction = error_vs_finest[0] / error_vs_finest[1]
                result['whole_run_reduction'] = reduction
                if reduction < required_reduction:
                    errors.append(f'{acid} {field}: whole-run error shrinks only {reduction:.2f}x '
                                  f'from {dts[0]} to {dts[1]} s (order '
                                  f'{WHOLE_RUN_MIN_ORDER} requires {required_reduction:.2f}x)')
            if len(smooth) < 10:
                result['status'] = 'no smooth segment'
                entry['fields'][field] = result
                continue
            d12 = max(abs(values[0][i] - values[1][i]) for i in smooth)
            d23 = max(abs(values[1][i] - values[2][i]) for i in smooth)
            result.update(d12=d12, d23=d23)
            if d23 <= floor:
                # Already resolved at the middle timestep; no order to measure.
                result['status'] = 'resolved'
            else:
                order = math.log(max(d12, floor) / d23) / math.log(ratio)
                result['observed_order'] = order
                result['status'] = ('first order' if order_band[0] <= order <= order_band[1]
                                    else 'order outside band')
                if result['status'] != 'first order':
                    errors.append(f'{acid} {field}: observed order {order:.2f} outside '
                                  f'{order_band[0]}..{order_band[1]}')
            entry['fields'][field] = result
        for kind in events[0]:
            counts = [len(event[kind]) for event in events]
            entry['events'][kind] = {'times': [event[kind] for event in events]}
            if len(set(counts)) != 1:
                errors.append(f'{acid} {kind}: event counts differ between timesteps {counts}')
                continue
            for n in range(counts[0]):
                reference = events[2][kind][n]
                for i in range(2):
                    gap = abs(events[i][kind][n] - reference)
                    if gap > event_steps * dts[i] + cadence + 1e-8:
                        errors.append(f'{acid} {kind} #{n + 1}: {dts[i]} s run is {gap:.3f} s '
                                      f'from the finest run')
        if observable:
            values = [_observable(observable, rows, times) for rows in series]
            estimate = _richardson(values, ratio)
            entry['observable'] = {'name': observable, 'values': values, **estimate}
            if production_dt is not None:
                production_error = _production_error(values, dts, estimate, production_dt)
                entry['observable']['production_dt'] = production_dt
                entry['observable']['estimated_error_at_production_dt'] = production_error
                if tolerance is not None and production_error > tolerance:
                    errors.append(f'{acid} {observable}: estimated error {production_error:.4g} '
                                  f'at {production_dt} s exceeds tolerance {tolerance}')
    report['passed'] = not errors
    report['errors'] = errors
    return errors, report


def _richardson(values, ratio):
    """Observed order and extrapolated limit of one observable (coarse to fine)."""
    d12, d23 = values[0] - values[1], values[1] - values[2]
    if d23 == 0.0 or d12 * d23 <= 0.0:
        return {'observed_order': None, 'extrapolated': values[2]}
    order = math.log(abs(d12 / d23)) / math.log(ratio)
    return {'observed_order': order,
            'extrapolated': values[2] - d23 / (ratio ** order - 1.0)}


def _production_error(values, dts, estimate, production_dt):
    """Error of the run at ``production_dt`` against the extrapolated limit."""
    for value, dt in zip(values, dts):
        if math.isclose(dt, production_dt, rel_tol=1e-9):
            return abs(value - estimate['extrapolated'])
    raise ValueError(f'production timestep {production_dt} s is not one of {dts}')


def summary(report):
    """One line per aircraft for console output."""
    lines = []
    for acid, entry in report['aircraft'].items():
        parts = []
        for field, result in entry['fields'].items():
            order = result.get('observed_order')
            parts.append(f'{field}={result["status"]}' +
                         (f'(p={order:.2f})' if order is not None else ''))
        if 'observable' in entry:
            observable = entry['observable']
            parts.append(f'{observable["name"]}='
                         f'{observable.get("estimated_error_at_production_dt", float("nan")):.4g}'
                         f'@{observable.get("production_dt")}s')
        lines.append(f'{acid}: ' + ', '.join(parts))
    return lines
