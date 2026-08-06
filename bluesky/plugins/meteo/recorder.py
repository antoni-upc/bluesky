"""Bounded-memory, versioned research recorder."""

import csv
import importlib.metadata
import json
import platform
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import bluesky as bs


SCHEMA_VERSION = 'samples-v7'
FIELDS = (
    'schema_version', 'run_id', 'sim_time_s', 'sample_interval_s', 'sim_utc', 'acid', 'actype',
    'lat_deg', 'lon_deg', 'geometric_alt_m', 'pressure_alt_m', 'tas_m_s',
    'cas_m_s', 'mach', 'vertical_speed_m_s', 'heading_deg', 'track_deg',
    'temperature_k', 'pressure_pa', 'density_kg_m3', 'wind_north_m_s',
    'wind_east_m_s', 'atmosphere_source', 'atmosphere_valid',
    'dataset_time', 'fallback_reason', 'performance_model', 'performance_dataset_version',
    'performance_aircraft', 'performance_resolution', 'performance_dummy',
    'dynamics_mode', 'performance_valid', 'performance_miss_count', 'thrust_n',
    'rated_thrust_n', 'drag_n', 'fuel_flow_kg_s', 'mass_kg',
    'envelope_policy', 'envelope_profile', 'envelope_checks',
    'envelope_status', 'envelope_failed_checks', 'envelope_last_action',
    'envelope_last_reason', 'envelope_event_count', 'envelope_violation_count',
    'mass_min_kg', 'mass_max_kg', 'envelope_configuration',
    'minimum_cas_m_s', 'maximum_cas_m_s', 'minimum_mach', 'maximum_mach',
    'maximum_altitude_m', 'minimum_rocd_m_s', 'maximum_rocd_m_s',
    'envelope_lateral_configuration', 'bank_angle_deg', 'load_factor',
    'minimum_load_factor', 'maximum_load_factor', 'maximum_bank_angle_deg')
UNITS = {
    'sim_time_s': 's', 'sample_interval_s': 's', 'lat_deg': 'deg', 'lon_deg': 'deg',
    'geometric_alt_m': 'm', 'pressure_alt_m': 'm', 'tas_m_s': 'm/s',
    'cas_m_s': 'm/s', 'mach': '1', 'vertical_speed_m_s': 'm/s',
    'heading_deg': 'deg', 'track_deg': 'deg', 'temperature_k': 'K',
    'pressure_pa': 'Pa', 'density_kg_m3': 'kg/m^3', 'wind_north_m_s': 'm/s',
    'wind_east_m_s': 'm/s', 'thrust_n': 'N', 'rated_thrust_n': 'N', 'drag_n': 'N',
    'fuel_flow_kg_s': 'kg/s', 'mass_kg': 'kg', 'mass_min_kg': 'kg',
    'mass_max_kg': 'kg', 'minimum_cas_m_s': 'm/s', 'maximum_cas_m_s': 'm/s',
    'minimum_mach': '1', 'maximum_mach': '1', 'maximum_altitude_m': 'm',
    'minimum_rocd_m_s': 'm/s', 'maximum_rocd_m_s': 'm/s',
    'bank_angle_deg': 'deg', 'load_factor': '1', 'minimum_load_factor': '1',
    'maximum_load_factor': '1', 'maximum_bank_angle_deg': 'deg'}


def _finite(value):
    try:
        return value if np.isfinite(value) else ''
    except TypeError:
        return value if value is not None else ''


class StreamingRecorder:
    def __init__(self):
        self.stream = None
        self.writer = None
        self.path = None
        self.run_id = ''
        self.rows = 0
        self.started_utc = ''
        self.sample_intervals = set()
        self.event_stream = None
        self.event_path = None
        self.event_count = 0
        self.reason_totals = {}
        self.quality_status = 'VALID'

    @property
    def active(self):
        return self.stream is not None

    def start(self, path):
        self.stop()
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = self.path.stem
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.stream = self.path.open('w', newline='', encoding='utf-8')
        self.event_path = self.path.with_suffix('.events.jsonl')
        self.event_stream = self.event_path.open('w', encoding='utf-8')
        self.writer = csv.DictWriter(self.stream, fieldnames=FIELDS)
        self.writer.writeheader()
        self.rows = 0
        self.sample_intervals.clear()
        self.event_count = 0
        self.reason_totals.clear()
        self.quality_status = 'VALID'

    def observe_event(self, event):
        """Synchronously persist a published event only while recording."""
        if not self.active:
            return
        self.event_stream.write(json.dumps(event.as_dict(), sort_keys=True) + '\n')
        self.event_stream.flush()
        self.event_count += 1
        self.reason_totals[event.reason] = self.reason_totals.get(event.reason, 0) + 1
        self.quality_status = 'ABORTED' if event.action == 'ABORTED' else (
            'DEGRADED' if self.quality_status == 'VALID' else self.quality_status)
        if event.action == 'ABORTED':
            # Capture the state that caused the event and finalize all evidence
            # synchronously. The publisher calls sim.hold only after this
            # subscriber returns.
            self.sample()
            self.stop()

    def sample(self):
        if not self.active:
            return
        perf = bs.traf.perf
        # Traffic exposes replaceable performance implementations through a
        # Proxy.  Record the selected implementation, not the Proxy class.
        try:
            from bluesky.core.entity import getproxied
            perf_impl = getproxied(perf)
        except (ImportError, AttributeError):
            perf_impl = perf
        try:
            from bluesky.core.simtime import Timer
            timer = Timer.gettimer('RESEARCHRECORDER.update')
            sample_interval = float(timer.dt_act) if timer else None
        except (ImportError, AttributeError, TypeError):
            sample_interval = None
        if sample_interval is not None:
            self.sample_intervals.add(sample_interval)
        for idx, acid in enumerate(bs.traf.id):
            def perf_value(name):
                value = getattr(perf, name, None)
                return '' if value is None or idx >= len(value) else _finite(value[idx])
            family = getattr(perf_impl, 'family', '')
            model_name = type(perf_impl).__name__
            if family and model_name == 'PyBadaTEM':
                model_name = f'PYBADATEM-BADA{family}'
            resolutions = getattr(perf_impl, 'resolutions', ())
            resolution = resolutions[idx] if idx < len(resolutions) else None
            dyn_modes = getattr(perf_impl, 'dyn_mode', ())
            dyn_mode = int(dyn_modes[idx]) if idx < len(dyn_modes) else None
            invalid = getattr(perf_impl, 'invalid', ())
            misses = getattr(perf_impl, 'failure_count', ())
            policies = getattr(perf_impl, 'envelope_policy', ())
            profiles = getattr(perf_impl, 'envelope_profile', ())
            checks = getattr(perf_impl, 'envelope_checks', ())
            statuses = getattr(perf_impl, 'envelope_status', ())
            failed = getattr(perf_impl, 'envelope_failed_checks', ())
            actions = getattr(perf_impl, 'envelope_last_action', ())
            reasons = getattr(perf_impl, 'envelope_last_reason', ())
            event_counts = getattr(perf_impl, 'envelope_event_count', ())
            violation_counts = getattr(perf_impl, 'envelope_violation_count', ())
            bounds = perf_impl.bounds(idx) if hasattr(perf_impl, 'bounds') else None
            flight_bounds = perf_impl.flight_bounds(idx) if hasattr(perf_impl, 'flight_bounds') else None
            vertical_bounds = perf_impl.vertical_bounds(idx) if hasattr(perf_impl, 'vertical_bounds') else None
            lateral_bounds = perf_impl.lateral_bounds(idx) if hasattr(perf_impl, 'lateral_bounds') else None
            bank_angle = perf_impl.effective_bank_angle(idx) if hasattr(perf_impl, 'effective_bank_angle') else None
            load_factor = (None if bank_angle is None or abs(bank_angle) >= 90.0 else
                           1.0 / np.cos(np.radians(abs(bank_angle))))
            names = lambda values: ','.join(getattr(value, 'value', str(value)) for value in values)
            row = {
                'schema_version': SCHEMA_VERSION, 'run_id': self.run_id,
                'sim_time_s': bs.sim.simt, 'sample_interval_s': sample_interval,
                'sim_utc': bs.sim.utc.isoformat(),
                'acid': acid, 'actype': bs.traf.type[idx], 'lat_deg': bs.traf.lat[idx],
                'lon_deg': bs.traf.lon[idx], 'geometric_alt_m': bs.traf.alt[idx],
                'pressure_alt_m': bs.traf.pressure_alt[idx], 'tas_m_s': bs.traf.tas[idx],
                'cas_m_s': bs.traf.cas[idx], 'mach': bs.traf.M[idx],
                'vertical_speed_m_s': bs.traf.vs[idx], 'heading_deg': bs.traf.hdg[idx],
                'track_deg': bs.traf.trk[idx], 'temperature_k': bs.traf.Temp[idx],
                'pressure_pa': bs.traf.p[idx], 'density_kg_m3': bs.traf.rho[idx],
                'wind_north_m_s': bs.traf.windnorth[idx], 'wind_east_m_s': bs.traf.windeast[idx],
                'atmosphere_source': bs.traf.atmos_source[idx],
                'atmosphere_valid': bool(bs.traf.atmos_valid[idx]),
                'dataset_time': bs.traf.atmos_dataset_time[idx],
                'fallback_reason': bs.traf.atmos_fallback_reason[idx],
                'performance_model': model_name,
                'performance_dataset_version': getattr(perf_impl, 'version', ''),
                'performance_aircraft': getattr(resolution, 'resolved', ''),
                'performance_resolution': getattr(resolution, 'method', ''),
                'performance_dummy': getattr(resolution, 'dummy', ''),
                'dynamics_mode': '' if dyn_mode is None else ('TEM' if dyn_mode else 'KINEMATIC'),
                'performance_valid': '' if idx >= len(invalid) else not bool(invalid[idx]),
                'performance_miss_count': '' if idx >= len(misses) else int(misses[idx]),
                'thrust_n': perf_value('thrust'),
                'rated_thrust_n': perf_value('rated_thrust'),
                'drag_n': perf_value('drag'), 'fuel_flow_kg_s': perf_value('fuelflow'),
                'mass_kg': perf_value('mass'),
                'envelope_policy': policies[idx] if idx < len(policies) else '',
                'envelope_profile': profiles[idx] if idx < len(profiles) else '',
                'envelope_checks': names(checks[idx]) if idx < len(checks) else '',
                'envelope_status': statuses[idx] if idx < len(statuses) else '',
                'envelope_failed_checks': names(failed[idx]) if idx < len(failed) else '',
                'envelope_last_action': actions[idx] if idx < len(actions) else '',
                'envelope_last_reason': reasons[idx] if idx < len(reasons) else '',
                'envelope_event_count': int(event_counts[idx]) if idx < len(event_counts) else '',
                'envelope_violation_count': int(violation_counts[idx]) if idx < len(violation_counts) else '',
                'mass_min_kg': '' if bounds is None else _finite(bounds.minimum),
                'mass_max_kg': '' if bounds is None else _finite(bounds.maximum),
                'envelope_configuration': '' if flight_bounds is None else flight_bounds.configuration,
                'minimum_cas_m_s': '' if flight_bounds is None else _finite(flight_bounds.minimum_cas),
                'maximum_cas_m_s': '' if flight_bounds is None else _finite(flight_bounds.maximum_cas),
                'minimum_mach': '' if flight_bounds is None else _finite(flight_bounds.minimum_mach),
                'maximum_mach': '' if flight_bounds is None else _finite(flight_bounds.maximum_mach),
                'maximum_altitude_m': '' if flight_bounds is None else _finite(flight_bounds.maximum_altitude),
                'minimum_rocd_m_s': '' if vertical_bounds is None else _finite(vertical_bounds.minimum_rocd),
                'maximum_rocd_m_s': '' if vertical_bounds is None else _finite(vertical_bounds.maximum_rocd),
                'envelope_lateral_configuration': '' if lateral_bounds is None else lateral_bounds.configuration,
                'bank_angle_deg': _finite(bank_angle), 'load_factor': _finite(load_factor),
                'minimum_load_factor': '' if lateral_bounds is None else _finite(lateral_bounds.minimum_load_factor),
                'maximum_load_factor': '' if lateral_bounds is None else _finite(lateral_bounds.maximum_load_factor),
                'maximum_bank_angle_deg': '' if lateral_bounds is None else _finite(lateral_bounds.maximum_bank_angle_deg)}
            self.writer.writerow({key: _finite(value) for key, value in row.items()})
            self.rows += 1
        self.stream.flush()

    def stop(self):
        if not self.active:
            return None
        self.stream.flush()
        self.stream.close()
        self.event_stream.flush()
        self.event_stream.close()
        self.event_stream = None
        self.stream = self.writer = None
        versions = {}
        for package in ('numpy', 'scipy', 'openap', 'pyBADA', 'netCDF4', 'pygrib'):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        sources = sorted(set(getattr(bs.traf, 'atmos_source', [])))
        dataset_times = sorted(set(filter(None, getattr(bs.traf, 'atmos_dataset_time', []))))
        perf = getattr(bs.traf, 'perf', None)
        try:
            from bluesky.core.entity import getproxied
            perf = getproxied(perf)
        except (ImportError, AttributeError):
            pass
        effective = []
        for idx, acid in enumerate(getattr(bs.traf, 'id', ())):
            policies = getattr(perf, 'envelope_policy', ())
            checks = getattr(perf, 'envelope_checks', ())
            effective.append({'aircraft': acid,
                              'policy': policies[idx] if idx < len(policies) else '',
                              'checks': [getattr(c, 'value', str(c)) for c in checks[idx]]
                              if idx < len(checks) else []})
        metadata = {
            'schema_version': SCHEMA_VERSION, 'run_id': self.run_id,
            'created_utc': self.started_utc, 'rows': self.rows,
            'csv': str(self.path), 'python': platform.python_version(),
            'dependencies': versions,
            'scenario': getattr(bs.sim, 'scenname', ''),
            'sample_intervals_s': sorted(self.sample_intervals),
            'atmosphere_sources': sources, 'dataset_times': dataset_times,
            'columns': list(FIELDS), 'missing_value': 'empty CSV field',
            'units': UNITS, 'events_jsonl': str(self.event_path),
            'event_total': self.event_count, 'reason_totals': self.reason_totals,
            'quality_status': self.quality_status,
            'effective_envelope': effective}
        meta_path = self.path.with_suffix('.metadata.json')
        meta_path.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        return self.path, meta_path

    def reset(self):
        self.stop()
        self.path = None
        self.rows = 0
        self.sample_intervals.clear()
        self.event_path = None
        self.event_count = 0
        self.reason_totals.clear()
        self.quality_status = 'VALID'

    def derive(self):
        """Generate independent optional artifacts from authoritative CSV."""
        if self.path is None or not self.path.exists() or self.active:
            raise RuntimeError('Stop a recording before generating derived artifacts')
        results = {}
        try:
            import pandas as pd
            excel = self.path.with_suffix('.xlsx')
            pd.read_csv(self.path).to_excel(excel, index=False)
            results['excel'] = {'ok': True, 'path': str(excel)}
        except Exception as exc:
            results['excel'] = {'ok': False, 'error': str(exc)}
        try:
            routes = {}
            with self.path.open(newline='', encoding='utf-8') as stream:
                for row in csv.DictReader(stream):
                    routes.setdefault(row['acid'], []).append(
                        (row['lon_deg'], row['lat_deg'], row['geometric_alt_m']))
            root = ET.Element('kml', xmlns='http://www.opengis.net/kml/2.2')
            document = ET.SubElement(root, 'Document')
            for acid, coordinates in routes.items():
                placemark = ET.SubElement(document, 'Placemark')
                ET.SubElement(placemark, 'name').text = acid
                line = ET.SubElement(placemark, 'LineString')
                ET.SubElement(line, 'altitudeMode').text = 'absolute'
                ET.SubElement(line, 'coordinates').text = ' '.join(','.join(point) for point in coordinates)
            kml = self.path.with_suffix('.kml')
            ET.ElementTree(root).write(kml, encoding='utf-8', xml_declaration=True)
            ET.parse(kml)
            results['kml'] = {'ok': True, 'path': str(kml)}
        except Exception as exc:
            results['kml'] = {'ok': False, 'error': str(exc)}
        try:
            import matplotlib.pyplot as plt
            import pandas as pd
            frame = pd.read_csv(self.path)
            figure, axis = plt.subplots()
            for acid, group in frame.groupby('acid'):
                axis.plot(group['sim_time_s'], group['geometric_alt_m'], label=acid)
            axis.set(xlabel='Simulation time [s]', ylabel='Geometric altitude [m]')
            axis.legend()
            plot = self.path.with_suffix('.png')
            figure.savefig(plot)
            plt.close(figure)
            results['plot'] = {'ok': True, 'path': str(plot)}
        except Exception as exc:
            results['plot'] = {'ok': False, 'error': str(exc)}
        return results
