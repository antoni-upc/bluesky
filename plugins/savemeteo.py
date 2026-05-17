"""
Vertical Meteo Recorder Plugin for BlueSky
===========================================

Records the full vertical meteorological profile at every aircraft position
at a fixed sampling interval (default 30 s).  The collected data is written
to a CSV file via the `SAVEMETEO [filename]` stack command.  A companion
post-processing script (`utils/meteo_to_json.py`) converts the CSV to the
`vertical_meteo.json` format used by external tools.

CSV columns (one row per sample per aircraft)
---------------------------------------------
  simt          – simulation time [s]
  acid          – aircraft callsign
  lat           – latitude  [deg]
  lon           – longitude [deg]
  track         – aircraft track angle [deg], used for wind decomposition
  distflown     – cumulative distance flown [m] (post-processor negates this
                  to produce distance2go with negative-remaining convention)
  temp_FL{n}    – temperature [K]  at FL n*10 (n = 0 … 87)
  w_FL{n}       – total wind speed [m/s]     at FL n*10
  ws_FL{n}      – along-track wind [m/s]     at FL n*10 (tail wind positive)
  wx_FL{n}      – cross-track wind [m/s]     at FL n*10

Flight levels sampled: FL0, FL10, FL20, … FL870  (88 levels)
Sampling interval:     30 simulated seconds (configurable via METEO_DT)
"""

import os
import math
import csv
from datetime import datetime

import numpy as np

import bluesky as bs
from bluesky import core, stack, traf, settings

# ── Constants ───────────────────────────────────────────────────────────────

# Flight-level grid: FL0 … FL870 in steps of FL10 (1 FL = 100 ft)
FL_STEP   = 10            # FL units between samples
FL_MAX    = 870           # highest FL to sample
FL_LIST   = list(range(0, FL_MAX + FL_STEP, FL_STEP))  # 88 values
N_FL      = len(FL_LIST)

# Convert flight levels to metres (1 FL = 100 ft; 1 ft = 0.3048 m)
FL_ALT_M  = [fl * 100.0 * 0.3048 for fl in FL_LIST]   # metres

# ISA constants for fallback temperature calculation
T0_ISA    = 288.15    # [K]
L_ISA     = -0.0065   # [K/m] lapse rate in troposphere
H_TROP    = 11000.0   # [m] tropopause height
T_TROP    = 216.65    # [K]

# Sampling period [simulated seconds]
SAMPLE_DT = 30.0

# Unit conversion
MPS_TO_KT = 1.0 / 0.514444


# ── ISA fallback ─────────────────────────────────────────────────────────────

def _isa_temp(alt_m):
    """Return ISA temperature [K] at altitude alt_m [m]."""
    if alt_m <= H_TROP:
        return T0_ISA + L_ISA * alt_m
    return T_TROP


# ── Plugin init ───────────────────────────────────────────────────────────────

def init_plugin():
    """Plugin initialisation function."""
    global savemeteo_inst
    savemeteo_inst = SaveMeteo()

    config = {
        'plugin_name': 'SAVEMETEO',
        'plugin_type': 'sim',
    }
    return config


# ── Main entity ───────────────────────────────────────────────────────────────

class SaveMeteo(core.Entity):
    """Records vertical meteo profiles at a fixed sampling interval."""

    def __init__(self):
        super().__init__()
        self._rows    = []          # accumulated CSV rows
        self._next_t  = 0.0        # next sample time [s]

    def create(self, n=1):
        super().create(n)

    def delete(self, acidx):
        super().delete(acidx)

    # ------------------------------------------------------------------ #
    @core.timed_function(name='savemeteo_update', dt=SAMPLE_DT)
    def update(self):
        """Sample the vertical meteo profile for each active aircraft."""
        simt = bs.sim.simt

        if traf.ntraf == 0:
            return

        # Grab the wind plugin's temperature interpolator if available
        # (set by windecmwf or windgfs – may be None)
        temp_field = getattr(traf.wind, 'temp_field', None)

        for i in range(traf.ntraf):
            acid      = traf.id[i]
            lat       = traf.lat[i]
            lon       = traf.lon[i]
            trk_deg   = float(traf.trk[i])
            trk_rad   = math.radians(trk_deg)
            distflown = float(traf.distflown[i])   # [m]

            row = {
                'simt':      round(simt, 1),
                'acid':      acid,
                'lat':       round(lat, 6),
                'lon':       round(lon, 6),
                'track':     round(trk_deg, 2),
                'distflown': round(distflown, 1),
            }

            for idx, (fl, alt_m) in enumerate(zip(FL_LIST, FL_ALT_M)):
                col = f'FL{fl}'

                # ── Wind at this FL ─────────────────────────────────────
                # Pass lat/lon as 1-element arrays so windfield.getdata returns
                # ndarray (skipping its float() cast, which breaks on NumPy 2.x
                # when the RGI result has shape (1,) instead of being 0-d).
                try:
                    vn_arr, ve_arr = traf.wind.getdata(
                        np.array([lat]), np.array([lon]), alt_m)
                    vn = float(vn_arr[0])
                    ve = float(ve_arr[0])
                except Exception:
                    vn, ve = 0.0, 0.0

                w_total = math.sqrt(vn * vn + ve * ve)

                # Along-track (ws): projection of wind onto track direction
                #   track vector: (sin(trk), cos(trk)) = (east, north)
                #   tail-wind positive → negate cross-product sign convention
                ws = ve * math.sin(trk_rad) + vn * math.cos(trk_rad)

                # Cross-track (wx): wind component perpendicular to track
                #   positive to the right of track
                wx = ve * math.cos(trk_rad) - vn * math.sin(trk_rad)

                # ── Temperature at this FL ──────────────────────────────
                if temp_field is not None:
                    try:
                        temp_k = float(temp_field([[alt_m, lat, lon]])[0])
                        if math.isnan(temp_k):
                            raise ValueError
                    except Exception:
                        temp_k = _isa_temp(alt_m)
                else:
                    temp_k = _isa_temp(alt_m)

                row[f'temp_{col}'] = round(temp_k, 2)
                row[f'w_{col}']    = round(w_total, 2)
                row[f'ws_{col}']   = round(ws, 2)
                row[f'wx_{col}']   = round(wx, 2)

            self._rows.append(row)

    # ------------------------------------------------------------------ #
    @stack.command(name='SAVEMETEO')
    def save_meteo(self, filename: str = ''):
        """Save the recorded vertical meteo data to a CSV file.

        Usage: SAVEMETEO [filename]
        """
        if not self._rows:
            return False, 'No meteo data recorded yet.'

        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename  = f'meteo_{timestamp}'

        if filename.lower().endswith('.csv'):
            filename = filename[:-4]

        out_dir  = bs.resource(settings.log_path)
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, f'{filename}.csv')

        # Build ordered fieldnames
        base_fields = ['simt', 'acid', 'lat', 'lon', 'track', 'distflown']
        fl_fields   = []
        for fl in FL_LIST:
            col = f'FL{fl}'
            fl_fields += [f'temp_{col}', f'w_{col}', f'ws_{col}', f'wx_{col}']
        fieldnames = base_fields + fl_fields

        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self._rows)
        except Exception as e:
            return False, f'Failed to write meteo CSV: {e}'

        return True, f'Meteo data saved to:\n{csv_path}'
