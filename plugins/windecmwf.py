from pathlib import Path
import cdsapi
import datetime
import numpy as np
import bluesky as bs
import netCDF4 as nc
from bluesky import stack
from bluesky.core import timed_function
from bluesky.traffic.windsim import WindSim
from bluesky.tools.aero import R
from scipy.interpolate import RegularGridInterpolator


datadir = Path('')


def init_plugin():
    global datadir
    datadir = bs.resource(bs.settings.data_path) / 'NetCDF'

    if not datadir.is_dir():
        datadir.mkdir()

    global windecmwf
    windecmwf = WindECMWF()
    
    # Crucial: Must select this implementation to replace the default WindSim singleton
    # and properly bind the @timed_function and @stack.command methods!
    windecmwf.select()

    config = {
        'plugin_name': 'WINDECMWF',
        'plugin_type': 'sim'
    }

    return config

class WindECMWF(WindSim):
    def __init__(self):
        super().__init__()
        self.year  = 0
        self.month = 0
        self.day   = 0
        self.hour  = 0
        self.lat0  = -90
        self.lon0  = -180
        self.lat1  = 90
        self.lon1  = 180

        # 3D atmosphere interpolators (same pattern as WindGFS)
        self.temp_field = None
        self.pres_field = None

        # Switch for periodic loading of new ECMWF data
        self.autoload = True

        # Current datetime cursor used by periodic updates.
        # Advanced by 3h on each update instead of querying bs.sim.utc.
        self._current_dt = None
        
    def fetch_nc(self, year, month, day):
        """
        Retrieve weather data via the CDS API for multiple pressure levels.
        The downloaded file contains all 3-hourly snapshots for the entire day,
        so one download per day is sufficient.
        Temperature ('t') is now also downloaded alongside wind components so
        the atmosphere (Temp, p, rho) can be overridden for each aircraft.
        """
        
        ymd = "%04d%02d%02d" % (year, month, day)
        
        fname = f'p_levels_{ymd}.nc'
        fpath = datadir / fname
        
        if not fpath.is_file():
            stack.echo("Downloading file, please wait...")
    
            # Set client
            c = cdsapi.Client()
            
            # Retrieve data 
            try:
                c.retrieve(
                    'reanalysis-era5-pressure-levels',
                    {
                        'product_type': 'reanalysis',
                        'format': 'netcdf',
                        'pressure_level': [
                            '100', '125', '150', 
                            '175', '200', '225',
                            '250', '300', '350',
                            '400', '450', '500',
                            '550', '600', '650', 
                            '700', '750', '775',
                            '800'
                        ],
                        'year': year,
                        'month': month,
                        'day': day,
                        'time': [
                            '00:00', '03:00', '06:00',
                            '09:00', '12:00', '15:00',
                            '18:00', '21:00',
                        ],
                        'variable': [
                            'u_component_of_wind',
                            'v_component_of_wind',
                            'temperature',          # ERA5 short name: 't'
                            'geopotential',         # ERA5 short name: 'z' [m²/s²] – used for altitude
                        ],
                    },
                    fpath)
            except Exception as e:
                stack.echo(f"Failed to fetch ECMWF data: {e}")
                if fpath.is_file():
                    fpath.unlink()
                return None
    
        stack.echo("Download completed.")
        try:
            netcdf = nc.Dataset(fpath, mode='r')
        except Exception as e:
            stack.echo(f"Failed to open NetCDF file: {e}")
            return None
    
        return netcdf


    def extract_wind(self, netcdf, lat0, lon0, lat1, lon1, hour):
        """Extract wind (u, v), temperature (t) and pressure from ERA5 NetCDF.

        Returns a (5, N) array: [lat, lon, alt_m, u, v, temp_K, pres_Pa]
        """
        # Load variables
        level = netcdf['pressure_level'][:].data   # hPa
        lats  = netcdf['latitude'][:].data
        lons  = netcdf['longitude'][:].data
        
        # Time index (0-7 for 3-hourly snapshots in ERA5 day file)
        hour_idx = round(hour / 3)

        vxs_  = netcdf['u'][hour_idx, :, :, :].data   # (level, lat, lon)
        vys_  = netcdf['v'][hour_idx, :, :, :].data

        # Temperature [K] – variable name in ERA5 CDS netCDF is 't'
        try:
            ts_ = netcdf['t'][hour_idx, :, :, :].data
        except Exception:
            ts_ = np.full_like(vxs_, np.nan)

        # Geopotential [m²/s²] – kept for potential future use (e.g. Approach B remapping)
        # but NO LONGER used to assign the altitude axis of the RGI.
        # Reason: averaging z_ over (lat, lon) to get one altitude per level collapses
        # the spatial dimension and makes all aircraft at the same FL see the same T.
        # See implementation plan notes for full explanation.
        try:
            z_ = netcdf['z'][hour_idx, :, :, :].data   # (level, lat, lon)
        except Exception:
            z_ = None  # not available in all cached files; ignored below

        # Close data for performance
        netcdf.close()

        # Convert pressure levels hPa → Pa.
        # Altitude axis: ISA pressure-altitude (hp) — the altitude at which the
        # standard atmosphere has the same pressure as each level.
        # hp is unique per level by definition and requires no spatial averaging,
        # so the lat/lon axes of the RGI retain the full spatial resolution of T.
        # apply_atmosphere() must query with the same hp axis for consistency.
        p_pa = level * 100.0
        h_m = (1.0 - (p_pa / 101325.0) ** 0.190264) * 44330.76923   # ISA hp [m]

        # Expand all fields into flat [N] arrays arranged as (level, lat, lon) → flat
        nlev  = len(level)
        nlat  = len(lats)
        nlon  = len(lons)

        # Build coordinate arrays
        lats_ = np.tile(np.repeat(lats, nlon), nlev)        # (nlev*nlat*nlon,)
        lons_ = np.tile(lons, nlat * nlev)
        alts_ = np.repeat(h_m, nlat * nlon)
        ps_   = np.repeat(p_pa, nlat * nlon)

        vxs_  = vxs_.flatten()
        vys_  = vys_.flatten()
        ts_   = ts_.flatten()
            
        # Convert longitudes: 0→360  →  -180→180
        lons_ = (lons_ + 180.0) % 360.0 - 180.0

        # Spatial bounding box
        lat0_ = min(lat0, lat1)
        lat1_ = max(lat0, lat1)
        lon0_ = min(lon0, lon1)
        lon1_ = max(lon0, lon1)

        mask = (lats_ >= lat0_) & (lats_ <= lat1_) & (lons_ >= lon0_) & (lons_ <= lon1_)

        # 7-row array: lat, lon, alt, u, v, temp_K, pres_Pa
        data = np.array([
            lats_[mask], lons_[mask], alts_[mask],
            vxs_[mask],  vys_[mask],
            ts_[mask],   ps_[mask],
        ])

        return data

    def _apply_wind(self, year, month, day, hour):
        """Load (or reuse) the NetCDF for the given day, apply wind AND atmosphere
        fields for the specified hour.  Returns a (success, message) tuple."""

        txt = "Loading wind field for %04d-%02d-%02d %02d:00..." % (year, month, day, hour)
        stack.echo(txt)

        netcdf = self.fetch_nc(year, month, day)

        if netcdf is None or self.lat0 == self.lat1 or self.lon0 == self.lon1:
            return False, "Wind data non-existent in area [%d, %d], [%d, %d]. " \
                % (self.lat0, self.lat1, self.lon0, self.lon1) \
                + "time: %04d-%02d-%02d %02d:00" \
                % (year, month, day, hour)

        # First clear existing wind field
        self.clear()
        self.temp_field = None
        self.pres_field = None

        data = self.extract_wind(netcdf, self.lat0, self.lon0, self.lat1, self.lon1, hour).T

        data = data[np.lexsort((data[:, 2], data[:, 1], data[:, 0]))]   # Sort by lat, lon, alt

        lats_uniq = np.unique(data[:, 0])
        lons_uniq = np.unique(data[:, 1])
        reshapefactor = len(lats_uniq) * len(lons_uniq)

        lat     = np.reshape(data[:, 0], (reshapefactor, -1)).T[0, :]
        lon     = np.reshape(data[:, 1], (reshapefactor, -1)).T[0, :]
        veast   = np.reshape(data[:, 3], (reshapefactor, -1)).T
        vnorth  = np.reshape(data[:, 4], (reshapefactor, -1)).T
        windalt = np.reshape(data[:, 2], (reshapefactor, -1)).T[:, 0]

        temp_data = np.reshape(data[:, 5], (reshapefactor, -1)).T   # shape (nlev, nlat*nlon)
        pres_data = np.reshape(data[:, 6], (reshapefactor, -1)).T

        # Append a high-altitude cap layer at 25 000 m mirroring the topmost data level.
        # WindField's internal RGI uses fill_value=0.0, so queries above windalt[-1]
        # would return zero wind. The cap ensures interp1d clamping takes effect
        # before the RGI boundary is reached.
        CAP_ALT = 25000.0
        windalt_cap = np.append(windalt, CAP_ALT)
        vnorth_cap  = np.vstack([vnorth,  vnorth[-1:, :]])
        veast_cap   = np.vstack([veast,   veast[-1:, :]])
        temp_cap    = np.vstack([temp_data, temp_data[-1:, :]])
        pres_cap    = np.vstack([pres_data, pres_data[-1:, :]])

        self.addpointvne(lat, lon, vnorth_cap, veast_cap, windalt_cap)

        # Build 3D atmosphere interpolators matching the WindGFS pattern
        try:
            t_values = temp_cap.reshape((len(windalt_cap), len(lats_uniq), len(lons_uniq)))
            p_values = pres_cap.reshape((len(windalt_cap), len(lats_uniq), len(lons_uniq)))

            self.temp_field = RegularGridInterpolator(
                (windalt_cap, lats_uniq, lons_uniq), t_values,
                bounds_error=False, fill_value=None)
            self.pres_field = RegularGridInterpolator(
                (windalt_cap, lats_uniq, lons_uniq), p_values,
                bounds_error=False, fill_value=None)
        except Exception as e:
            stack.echo(f"Warning: Failed to build atmosphere interpolators: {e}")
            self.temp_field = None
            self.pres_field = None

        return True, "Wind and Atmosphere fields updated in area [%d, %d], [%d, %d]. " \
            % (self.lat0, self.lat1, self.lon0, self.lon1) \
            + "time: %04d-%02d-%02d %02d:00" \
            % (year, month, day, hour)

    @stack.command(name='WINDECMWF')
    def loadwind(self, lat0: 'lat', lon0: 'lon', lat1: 'lat', lon1: 'lon',
               year: int=None, month: int=None, day: int=None, hour: int=None):
        ''' WINDECMWF: Load a windfield directly from ECMWF/ERA5 database.

            Arguments:
            - lat0, lon0, lat1, lon1 [deg]: Bounding box in which to generate wind field
            - year, month, day, hour: Date and time of wind data (optional, will use
              current simulation UTC if not specified).
        '''
        self.lat0, self.lon0, self.lat1, self.lon1 = min(lat0, lat1), \
                              min(lon0, lon1), max(lat0, lat1), max(lon0, lon1)

        # Determine the requested base datetime
        req_year  = year  or bs.sim.utc.year
        req_month = month or bs.sim.utc.month
        req_day   = day   or bs.sim.utc.day
        req_hour  = hour if hour is not None else bs.sim.utc.hour

        # Round to the nearest 3-hour slot
        req_hour = round(req_hour / 3) * 3

        base_dt = datetime.datetime(req_year, req_month, req_day, 0, 0) + \
                  datetime.timedelta(hours=req_hour)

        # Rounding to hour 24 → roll over to next day at 00:00
        if req_hour == 24:
            base_dt = datetime.datetime(req_year, req_month, req_day) + \
                      datetime.timedelta(days=1)

        # Store the cursor so the periodic update knows where to start
        self._current_dt = base_dt

        # Pre-fetch the NetCDF for the starting day (no-op if already on disk)
        self.fetch_nc(base_dt.year, base_dt.month, base_dt.day)

        ok, msg = self._apply_wind(base_dt.year, base_dt.month, base_dt.day, base_dt.hour)
        return ok, msg

    @timed_function(name='WINDECMWF_update', dt=3600)
    def update(self):
        if not self.autoload or self._current_dt is None:
            return

        # Advance the time cursor by 3 hours (ERA5 data resolution)
        self._current_dt += datetime.timedelta(hours=3)

        dt = self._current_dt
        _, txt = self._apply_wind(dt.year, dt.month, dt.day, dt.hour)
        stack.echo("%s" % txt)

    def apply_atmosphere(self):
        ''' Override ISA atmosphere with ERA5/ECMWF data.
            Called synchronously by Traffic.update() right after vatmos(),
            before any performance calculations.
        '''
        if self.temp_field is None or self.pres_field is None or bs.traf.ntraf == 0:
            return

        # Coordinate array for interpolation.
        # Use ISA pressure-altitude (hp) — the same axis used in extract_wind() —
        # rather than geometric altitude. This ensures that the lat/lon dimensions
        # of the RGI field are the only source of spatial variation, correctly
        # returning different temperatures at the same FL for different positions.
        # vatmos() has already computed bs.traf.p, so we derive hp from that.
        p_now = np.maximum(1.0, bs.traf.p)          # Pa; guard against zero
        hp_m  = (1.0 - (p_now / 101325.0) ** 0.190264) * 44330.76923
        hp_m  = np.maximum(0.0, hp_m)
        coords = np.vstack((hp_m, bs.traf.lat, bs.traf.lon)).T

        try:
            new_t = self.temp_field(coords)
            new_p = self.pres_field(coords)

            # Only override where the interpolated values are valid (within data bounds)
            valid = ~np.isnan(new_t) & ~np.isnan(new_p)
            if np.any(valid):
                bs.traf.Temp[valid] = new_t[valid]
                bs.traf.p[valid]    = new_p[valid]
                bs.traf.rho[valid]  = new_p[valid] / (R * new_t[valid])
        except Exception:
            pass