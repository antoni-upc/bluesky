"""Versioned streaming CSV recorder with legacy command aliases."""

import bluesky as bs
from bluesky import stack
from bluesky.plugins.meteo.recorder import StreamingRecorder


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
