"""
Flight Header Recorder Plugin for BlueSky

Records per-aircraft state data every second in the exact column format:
    Phase, UTC, t[s], s[NM], h[ft], TAS[kt], THR[%], VS[ft/min],
    CAS[kt], mach[-], GS[kt], FlightID, Temp[degC], Lat[deg], Lon[deg],
    heading[deg], track[deg], WP_prev, WP_next, hp[ft], Thrust[daN],
    Tidle[daN], Tmax[daN], SB[%], mass[kg], FF[kg/h], phi[º], FPA_a[º],
    nz[g], Ws[kt], Wx[kt], P[Pa]

Use command SAVEHEADER [filename] to export the collected data.
"""
import os
import math
from datetime import datetime, timezone
import pandas as pd

from bluesky import core, stack, traf, settings
import bluesky as bs

# Unit conversion constants (SI → display units)
M_TO_FT   = 1.0 / 0.3048          # metres → feet
MPS_TO_KT = 1.0 / 0.514444        # m/s    → knots
MPS_TO_FPM = 1.0 / 0.00508        # m/s    → ft/min
M_TO_NM   = 1.0 / 1852.0          # metres → nautical miles

# ISA sea-level pressure [Pa] – used for pressure altitude calculation
P0_ISA    = 101325.0

# Internal phase-code → short label mapping (from OpenAP phase module)
PHASE_LABEL = {
    0: 'NA',   # Unknown
    1: 'GD',   # Ground
    2: 'IC',   # Initial climb
    3: 'CL',   # Climb
    4: 'CR',   # Cruise
    5: 'DE',   # Descent
    6: 'AP',   # Approach
}


def init_plugin():
    ''' Plugin initialisation function. '''
    saveheader = SaveHeader()

    config = {
        'plugin_name': 'SAVEHEADER',
        'plugin_type': 'sim',
    }
    return config


def _pressure_altitude_ft(p_pa):
    """Convert static pressure [Pa] to ISA pressure altitude [ft]."""
    # Standard ISA troposphere formula inverse
    return 145442.15 * (1.0 - (p_pa / P0_ISA) ** 0.190263)


class SaveHeader(core.Entity):
    ''' Entity that logs per-aircraft state in the reference header format. '''

    def __init__(self):
        super().__init__()
        self.flight_data = []   # list of dicts, one per aircraft per second
        self.last_phase = {}    # mapping from acid to last known phase

    def create(self, n=1):
        super().create(n)

    def delete(self, acidx):
        super().delete(acidx)

    # ------------------------------------------------------------------ #
    @core.timed_function(name='saveheader_update', dt=1.0)
    def update(self):
        ''' Collect one snapshot per active aircraft every simulated second. '''
        simt = bs.sim.simt

        # UTC time formatted as H:MM:SS based on sim time
        h = int(simt // 3600)
        m = int((simt % 3600) // 60)
        s = int(simt % 60)
        utc_now = f"{h}:{m:02d}:{s:02d}"

        for i in range(traf.ntraf):
            acid  = traf.id[i]
            lat   = traf.lat[i]
            lon   = traf.lon[i]
            alt_m = traf.alt[i]

            # Speeds
            tas_kt  = traf.tas[i]  * MPS_TO_KT
            cas_kt  = traf.cas[i]  * MPS_TO_KT
            gs_kt   = traf.gs[i]   * MPS_TO_KT
            vs_fpm  = traf.vs[i]   * MPS_TO_FPM
            mach    = traf.M[i]

            # Altitude in feet
            h_ft    = alt_m * M_TO_FT

            # Distance flown in NM
            s_nm    = traf.distflown[i] * M_TO_NM

            # Throttle: traf.thr holds 0-1 (or -999 for auto). Show as %
            thr_raw = traf.thr[i]
            thr_pct = round(thr_raw * 100.0, 1) if thr_raw >= 0.0 else ''

            # Heading and track
            hdg = traf.hdg[i]
            trk = traf.trk[i]

            # Temperature: Kelvin → Celsius
            temp_c = traf.Temp[i] - 273.15

            # Pressure altitude [ft]
            hp_ft = _pressure_altitude_ft(traf.p[i])

            # Flight phase label
            try:
                phase_int = int(traf.perf.phase[i])
                phase_lbl = PHASE_LABEL.get(phase_int, 'NA')
            except Exception:
                phase_lbl = 'NA'

            if phase_lbl == 'NA':
                phase_lbl = self.last_phase.get(acid, 'NA')
            else:
                self.last_phase[acid] = phase_lbl

            # Waypoints from active flight plan
            wp_prev = ''
            wp_next = ''
            try:
                route  = traf.ap.route[i]
                iactwp = route.iactwp
                if 0 <= iactwp < route.nwp:
                    wp_next = route.wpname[iactwp]
                    if iactwp > 0:
                        wp_prev = route.wpname[iactwp - 1]
            except Exception:
                pass   # leave blank if no route / route not initialised

            thrust_dan = ''
            try: thrust_dan = round(float(traf.perf.thrust[i]) / 10.0, 2)
            except Exception: pass
            
            tidle_dan = ''
            try: tidle_dan = round(float(traf.perf.thr_idle[i]) / 10.0, 2)
            except Exception:
                try: tidle_dan = round(float(traf.perf.Tidle[i]) / 10.0, 2)
                except Exception: pass

            tmax_dan = ''
            try: tmax_dan = round(float(traf.perf.thr_max[i]) / 10.0, 2)
            except Exception:
                try: tmax_dan = round(float(traf.perf.Tmax[i]) / 10.0, 2)
                except Exception: pass

            sb_pct = ''
            try: sb_pct = round(float(traf.sb[i]) * 100.0, 1)
            except Exception:
                try: sb_pct = round(float(traf.perf.sb[i]) * 100.0, 1)
                except Exception: pass

            mass_kg = ''
            try: mass_kg = round(float(traf.mass[i]), 1)
            except Exception:
                try: mass_kg = round(float(traf.perf.mass[i]), 1)
                except Exception: pass

            ff_kgh = ''
            try: ff_kgh = round(float(traf.perf.fuelflow[i]) * 3600.0, 2) 
            except Exception: pass

            phi_deg = ''
            try: phi_deg = round(math.degrees(float(traf.phi[i])), 2)
            except Exception:
                try: phi_deg = round(math.degrees(float(traf.bank[i])), 2)
                except Exception: pass

            fpa_deg = ''
            try: fpa_deg = round(math.degrees(float(traf.gam[i])), 2)
            except Exception:
                try: fpa_deg = round(math.degrees(float(traf.gamma[i])), 2)
                except Exception:
                    try: fpa_deg = round(math.degrees(float(traf.fpa[i])), 2)
                    except Exception: pass

            nz_g = ''
            try: nz_g = round(float(traf.nz[i]), 2)
            except Exception:
                try:
                    phi_rad = float(traf.phi[i] if hasattr(traf, 'phi') else traf.bank[i])
                    nz_g = round(1.0 / math.cos(phi_rad), 2)
                except Exception: pass

            ws_kt = ''
            wx_kt = ''
            try:
                wn = float(traf.windnorth[i])
                we = float(traf.windeast[i])
                ws = math.sqrt(wn**2 + we**2)
                ws_kt = round(ws * MPS_TO_KT, 2)
                
                trk_rad = math.radians(float(traf.trk[i]))
                wx = we * math.cos(trk_rad) - wn * math.sin(trk_rad)
                wx_kt = round(wx * MPS_TO_KT, 2)
            except Exception: pass

            # Pressure [Pa] – overridden by meteo plugins (WINDECMWF / WINDGFS / WINDECAC)
            # via apply_atmosphere(); falls back to the ISA value stored in traf.p
            p_pa = ''
            try:
                p_pa = round(float(traf.p[i]), 2)
            except Exception: pass

            self.flight_data.append({
                'Phase':          phase_lbl,
                'UTC':            utc_now,
                't[s]':           simt,
                's[NM]':          round(s_nm, 4),
                'h[ft]':          round(h_ft, 2),
                'TAS[kt]':        round(tas_kt, 2),
                'THR[%]':         thr_pct,
                'VS[ft/min]':     round(vs_fpm, 1),
                'CAS[kt]':        round(cas_kt, 2),
                'mach[-]':        round(mach, 4),
                'GS[kt]':         round(gs_kt, 2),
                'FlightID':       acid,
                'Temp[degC]':     round(temp_c, 10),
                'Lat[deg]':       lat,
                'Lon[deg]':       lon,
                'heading[deg]':   round(hdg, 2),
                'track[deg]':     round(trk, 2),
                'WP_prev':        wp_prev,
                'WP_next':        wp_next,
                'hp[ft]':         round(hp_ft, 2),
                'Thrust[daN]':    thrust_dan,
                'Tidle[daN]':     tidle_dan,
                'Tmax[daN]':      tmax_dan,
                'SB[%]':          sb_pct,
                'mass[kg]':       mass_kg,
                'FF[kg/h]':       ff_kgh,
                'phi[º]':         phi_deg,
                'FPA_a[º]':       fpa_deg,
                'nz[g]':          nz_g,
                'Ws[kt]':         ws_kt,
                'Wx[kt]':         wx_kt,
                'P[Pa]':          p_pa,
            })

    # ------------------------------------------------------------------ #
    @stack.command(name='SAVEHEADER')
    def save_header(self, filename: str = ''):
        ''' Save the recorded flight header data to an Excel (.xlsx) or CSV file.
            Usage: SAVEHEADER [filename]
        '''
        if not self.flight_data:
            return False, 'No flight header data recorded yet.'

        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'flightheader_{timestamp}'

        # Strip extension if user provided it
        for ext in ('.xlsx', '.xls', '.csv'):
            if filename.lower().endswith(ext):
                filename = filename[:filename.rfind('.')]
                break

        out_dir = bs.resource(settings.log_path)
        os.makedirs(out_dir, exist_ok=True)

        excel_path = os.path.join(out_dir, f'{filename}.xlsx')
        csv_path   = os.path.join(out_dir, f'{filename}.csv')

        df = pd.DataFrame(self.flight_data)

        success_msg = ''
        df.to_csv(csv_path, index=False)
        success_msg = (
            f'Data saved as CSV:\n{csv_path}'
        )
        try:
            df.to_excel(excel_path, index=False)
            success_msg = f'Flight header data saved to Excel:\n{excel_path}'
        except ImportError:
            pass
        except Exception as e:
            return False, f'Failed to save Excel file: {e}'

        # Phase-based KML sampling intervals [simulated seconds]
        #   GD = ground (taxi turns)
        #   IC = initial climb / takeoff SID (tight turns)
        #   CL = climb
        #   CR = cruise (mostly straight)
        #   DE = descent / STAR (some turns)
        #   AP = approach / final (many turns, precision needed)
        KML_PHASE_DT = {
            'GD': 3,
            'IC': 3,
            'CL': 3,
            'CR': 30,
            'DE': 7,
            'AP': 3,
            'NA': 30,   # fallback
        }

        # Generate KML
        kml_path = os.path.join(out_dir, f'{filename}.kml')
        try:
            kml_lines = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<kml xmlns="http://www.opengis.net/kml/2.2">',
                '  <Document>',
                f'    <name>{filename}</name>'
            ]

            for acid, group in df.groupby('FlightID'):

                # ── "Trajectory" LineString Placemark ────────────────────────
                # Lateral_Plot.py searches for a segment named "Trajectory"
                # with a LineString geometry to draw the route and auto-zoom.
                # Phase-based sampling keeps the line smooth where turns are
                # frequent (takeoff, climb, approach) and sparse in cruise.
                coord_parts = []
                last_traj_t = -9999.0   # force first point always included
                for _, row in group.iterrows():
                    sim_t  = float(row['t[s]'])
                    phase  = str(row.get('Phase', 'NA'))
                    dt_min = KML_PHASE_DT.get(phase, 30)
                    if sim_t - last_traj_t < dt_min:
                        continue
                    last_traj_t = sim_t
                    lon   = row['Lon[deg]']
                    lat   = row['Lat[deg]']
                    alt_m = float(row['h[ft]']) * 0.3048
                    coord_parts.append(f'{lon},{lat},{alt_m}')
                coord_str = ' '.join(coord_parts)

                kml_lines.append('    <Placemark>')
                kml_lines.append('      <name>Trajectory</name>')
                kml_lines.append('      <LineString>')
                kml_lines.append('        <altitudeMode>absolute</altitudeMode>')
                kml_lines.append(f'        <coordinates>{coord_str}</coordinates>')
                kml_lines.append('      </LineString>')
                kml_lines.append('    </Placemark>')

                # ── Per-aircraft phase-sampled Point folder ───────────────────
                kml_lines.append(f'    <Folder><name>{acid} Route</name>')

                last_kml_t = -9999.0   # force first point always included

                for _, row in group.iterrows():
                    sim_t   = float(row['t[s]'])
                    phase   = str(row.get('Phase', 'NA'))
                    dt_min  = KML_PHASE_DT.get(phase, 30)

                    # Skip this row if not enough simulated time has elapsed
                    if sim_t - last_kml_t < dt_min:
                        continue
                    last_kml_t = sim_t

                    lon   = row['Lon[deg]']
                    lat   = row['Lat[deg]']
                    alt_m = float(row['h[ft]']) * 0.3048

                    kml_lines.append('      <Placemark>')
                    kml_lines.append(f'        <name>{int(sim_t)}s [{phase}]</name>')
                    kml_lines.append('        <Point>')
                    kml_lines.append('          <altitudeMode>absolute</altitudeMode>')
                    kml_lines.append(f'          <coordinates>{lon},{lat},{alt_m}</coordinates>')
                    kml_lines.append('        </Point>')
                    kml_lines.append('      </Placemark>')

                kml_lines.append('    </Folder>')
                
            kml_lines.append('  </Document>')
            kml_lines.append('</kml>')
            
            with open(kml_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(kml_lines))
            
            success_msg += f'\nKML route saved:\n{kml_path}'
        except Exception as e:
            # If KML fails, we still exported the CSV/XLS, so just append the warning
            success_msg += f'\nWarning: Failed to save KML file: {e}'

        return True, success_msg
