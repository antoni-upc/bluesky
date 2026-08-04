"""Versioned streaming CSV recorder with legacy command aliases."""

import bluesky as bs
from bluesky import stack
from bluesky.plugins.meteo.recorder import StreamingRecorder
from bluesky.tools.aero import vatmos


bs.settings.set_variable_defaults(research_output_path='output', research_recording_dt=1.0)
recorder = StreamingRecorder()


def init_plugin():
    output = bs.resource(bs.settings.research_output_path)
    output.mkdir(parents=True, exist_ok=True)
    probe = output / '.write-capability'
    try:
        probe.write_text('ok', encoding='ascii')
    except OSError as exc:
        raise ImportError(f'RESEARCHRECORDER output is not writable: {output}') from exc
    finally:
        probe.unlink(missing_ok=True)
    return {'plugin_name': 'RESEARCHRECORDER', 'plugin_type': 'sim',
            'update_interval': bs.settings.research_recording_dt,
            'update': update, 'reset': reset}


def update():
    recorder.sample()


def reset():
    recorder.reset()


@stack.command(name='ATMOSSTATUS', annotations='[txt]')
def atmosstatus(acid=None):
    """Show applied atmosphere, wind, airdata, and ISA differences."""
    if not bs.traf.id:
        return True, 'ATMOSSTATUS: no aircraft'
    if acid is None:
        indices = range(bs.traf.ntraf)
    else:
        idx = bs.traf.id2idx(acid)
        if idx < 0:
            return False, f'Aircraft {acid} not found'
        indices = (idx,)
    lines = []
    for idx in indices:
        isa_p, isa_rho, isa_temp = vatmos(bs.traf.alt[idx])
        lines.append(
            f'{bs.traf.id[idx]}: source={bs.traf.atmos_source[idx]} '
            f'valid={bool(bs.traf.atmos_valid[idx])} '
            f'dataset={bs.traf.atmos_dataset_time[idx] or "-"} '
            f'fallback={bs.traf.atmos_fallback_reason[idx] or "-"}\n'
            f'  geometric_alt={bs.traf.alt[idx]:.1f} m '
            f'pressure_alt={bs.traf.pressure_alt[idx]:.1f} m\n'
            f'  T={bs.traf.Temp[idx]:.3f} K (ISA {isa_temp:.3f}, '
            f'delta {bs.traf.Temp[idx] - isa_temp:+.3f})\n'
            f'  p={bs.traf.p[idx]:.3f} Pa (ISA {isa_p:.3f}, '
            f'delta {bs.traf.p[idx] - isa_p:+.3f}) '
            f'rho={bs.traf.rho[idx]:.6f} kg/m3 (ISA {isa_rho:.6f})\n'
            f'  wind_north={bs.traf.windnorth[idx]:.3f} m/s '
            f'wind_east={bs.traf.windeast[idx]:.3f} m/s\n'
            f'  TAS={bs.traf.tas[idx]:.3f} m/s CAS={bs.traf.cas[idx]:.3f} m/s '
            f'Mach={bs.traf.M[idx]:.5f} GS={bs.traf.gs[idx]:.3f} m/s')
    return True, '\n'.join(lines)


@stack.command(name='RECORDRESEARCH')
def record(action: str = 'STATUS', filename: str = ''):
    action = action.upper()
    if action == 'START':
        if not filename:
            return False, 'RECORDRESEARCH START requires a CSV filename'
        recorder.start(bs.resource(bs.settings.research_output_path) / filename)
        return True, f'Recording to {recorder.path}'
    if action == 'STOP':
        paths = recorder.stop()
        return (True, 'Recorder was not active') if paths is None else \
            (True, f'Recorded CSV {paths[0]} and metadata {paths[1]}')
    if action == 'RESET':
        recorder.reset()
        return True, 'Recorder reset'
    if action == 'STATUS':
        from bluesky.core.simtime import Timer
        timer = Timer.gettimer('RESEARCHRECORDER.update')
        interval = timer.dt_act if timer else bs.settings.research_recording_dt
        return True, f'Recorder active={recorder.active}, rows={recorder.rows}, interval={interval} s'
    if action == 'INTERVAL':
        try:
            interval = float(filename)
        except (TypeError, ValueError):
            return False, 'RECORDRESEARCH INTERVAL requires seconds'
        if not 0.0 < interval:
            return False, 'Recording interval must be positive'
        from bluesky.core.simtime import setdt
        return setdt(interval, 'RESEARCHRECORDER.update')
    return False, 'Use START, STOP, RESET, STATUS, or INTERVAL'


@stack.command(name='EXPORTRESEARCH')
def exportresearch():
    try:
        results = recorder.derive()
    except RuntimeError as exc:
        return False, str(exc)
    lines = [f'{name}: ' + (value.get('path') if value['ok'] else f"FAILED: {value['error']}")
             for name, value in results.items()]
    return True, '\n'.join(lines)


def _legacy(filename):
    if recorder.active:
        return record('STOP')
    return record('START', filename or 'research-samples.csv')


@stack.command(name='SAVEMETEO')
def savemeteo(filename: str = ''):
    return _legacy(filename)


@stack.command(name='SAVEATMOS')
def saveatmos(filename: str = ''):
    return _legacy(filename)


@stack.command(name='SAVEHEADER')
def saveheader(filename: str = ''):
    return _legacy(filename)


@stack.command(name='SAVETRAJ')
def savetraj(filename: str = ''):
    return _legacy(filename)
