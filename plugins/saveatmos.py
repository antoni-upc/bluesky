"""
Atmosphere Recorder Plugin for BlueSky

Records the pressure and temperature values of active aircraft at every time step 
of the simulation and provides a command to export these to an Excel (.xlsx/.xls)
or CSV file.
"""
import os
import csv
from datetime import datetime
import pandas as pd
import numpy as np

# Import the global bluesky objects
from bluesky import core, stack, traf, settings
import bluesky as bs

def init_plugin():
    ''' Plugin initialisation function. '''
    # Instantiate our entity
    saveatmos = SaveAtmos()

    # Configuration parameters
    config = {
        'plugin_name':     'SAVEATMOS',
        'plugin_type':     'sim',
    }

    return config

class SaveAtmos(core.Entity):
    ''' Entity object to log and save atmospheric conditions per aircraft. '''
    def __init__(self):
        super().__init__()
        # A dictionary to hold atmosphere logic: acid -> list of dicts
        self.atmos_data = []

    def create(self, n=1):
        ''' This function gets called automatically when new aircraft are created. '''
        super().create(n)

    def delete(self, acidx):
        ''' Ensure we keep track or drop? Usually we keep it until SAVEATMOS is called. '''
        super().delete(acidx)

    # Functions that need to be called periodically
    @core.timed_function(name='saveatmos_update', dt=1.0)
    def update(self):
        ''' Periodic update function for storing atmospheric variables. '''
        simt = bs.sim.simt
        
        # We need to iterate over all active aircraft
        for i in range(traf.ntraf):
            windnorth = traf.windnorth[i]
            windeast = traf.windeast[i]
            acid = traf.id[i]
            lat = traf.lat[i]
            lon = traf.lon[i]
            alt = traf.alt[i]
            
            # Atmospheric variables
            p = traf.p[i]
            temp = traf.Temp[i]
            rho = traf.rho[i]
            
            self.atmos_data.append({
                'Time [s]': simt,
                'Aircraft_ID': acid,
                'Wind_North [m/s]': windnorth,
                'Wind_East [m/s]': windeast,
                'Latitude [deg]': lat,
                'Longitude [deg]': lon,
                'Altitude [m]': alt,
                'Pressure [Pa]': p,
                'Temperature [K]': temp,
                'Density [kg/m^3]': rho
            })

    @stack.command(name='SAVEATMOS')
    def save_atmosphere(self, filename: str = ''):
        ''' Save the recorded atmosphere data to a .xls or .csv file. 
            Usage: SAVEATMOS [filename]
        '''
        if not self.atmos_data:
            return False, 'No atmospheric data recorded yet.'

        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"atmosphere_{timestamp}"
            
        # Strip extension if user provided it
        if filename.lower().endswith('.xls') or filename.lower().endswith('.xlsx') or filename.lower().endswith('.csv'):
            filename = filename[:filename.rfind('.')]

        out_dir = bs.resource(settings.log_path)
        os.makedirs(out_dir, exist_ok=True)
        
        excel_path = os.path.join(out_dir, f"{filename}.xlsx")
        csv_path = os.path.join(out_dir, f"{filename}.csv")
        
        # Create a Pandas DataFrame for easy export
        df = pd.DataFrame(self.atmos_data)
        
        # 1. Try to save to Excel (requires openpyxl or xlwt depending on format)
        success_msg = ""
        try:
            df.to_excel(excel_path, index=False)
            success_msg = f"Atmospheric data successfully saved to Excel:\n{excel_path}"
        except ImportError:
            # Fallback to CSV if exact excel writing libraries aren't installed
            try:
                df.to_csv(csv_path, index=False)
                success_msg = f"Excel engine not found. Data saved as Excel-compatible CSV instead:\n{csv_path}"
            except Exception as e:
                return False, f"Failed to save CSV: {e}"
        except Exception as e:
            return False, f"Failed to save Excel file: {e}"

        return True, success_msg
