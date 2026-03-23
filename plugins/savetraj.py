"""
Trajectory Saver Plugin for BlueSky

Records the trajectory (Lat/Lon) of active aircraft and provides a
command to export the trajectories to a CSV file and a plotted PNG image.
"""
import os
import csv
from datetime import datetime
import matplotlib.pyplot as plt
import numpy as np

# Import the global bluesky objects
from bluesky import core, stack, traf, settings
import bluesky as bs

def init_plugin():
    ''' Plugin initialisation function. '''
    # Instantiate our entity
    savetraj = SaveTraj()

    # Configuration parameters
    config = {
        'plugin_name':     'SAVETRAJ',
        'plugin_type':     'sim',
    }

    return config

class SaveTraj(core.Entity):
    ''' Entity object to log and save trajectories. '''
    def __init__(self):
        super().__init__()
        # A dictionary to hold trajectory logic: acid -> list of (simt, lat, lon)
        self.trajectories = {}

    def create(self, n=1):
        ''' This function gets called automatically when new aircraft are created. '''
        super().create(n)

    def delete(self, acidx):
        ''' Ensure we keep track or drop? Usually we keep it until SAVETRAJ is called. '''
        super().delete(acidx)

    # Functions that need to be called periodically
    @core.timed_function(name='savetraj_update', dt=1.0)
    def update(self):
        ''' Periodic update function for storing coordinates. '''
        simt = bs.sim.simt
        
        # We need to iterate over all active aircraft
        for i in range(traf.ntraf):
            acid = traf.id[i]
            lat = traf.lat[i]
            lon = traf.lon[i]
            
            if acid not in self.trajectories:
                self.trajectories[acid] = []
            
            self.trajectories[acid].append((simt, lat, lon))

    @stack.command(name='SAVETRAJ')
    def save_trajectory(self, filename: str = ''):
        ''' Save the recorded trajectories to CSV and PNG. 
            Usage: SAVETRAJ [filename]
        '''
        if not self.trajectories:
            return False, 'No trajectory data recorded yet.'

        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"trajectories_{timestamp}"
            
        # Strip extension if user provided it
        if filename.lower().endswith('.csv') or filename.lower().endswith('.png'):
            filename = filename[:-4]

        out_dir = bs.resource(settings.log_path)
        os.makedirs(out_dir, exist_ok=True)
        
        csv_path = os.path.join(out_dir, f"{filename}.csv")
        png_path = os.path.join(out_dir, f"{filename}.png")
        
        # 1. Save to CSV
        try:
            with open(csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['Aircraft_ID', 'Time', 'Latitude', 'Longitude'])
                for acid, traj in self.trajectories.items():
                    for pt in traj:
                        writer.writerow([acid, pt[0], pt[1], pt[2]])
        except Exception as e:
            return False, f"Failed to save CSV: {e}"

        # 2. Plot and save to PNG
        try:
            plt.figure(figsize=(10, 8))
            
            for acid, traj in self.trajectories.items():
                if traj:
                    coords = np.array(traj)
                    lons = coords[:, 2]
                    lats = coords[:, 1]
                    plt.plot(lons, lats, linewidth=1.5, label=acid)
            
            plt.xlabel('Longitude')
            plt.ylabel('Latitude')
            plt.title('Aircraft Trajectories')
            
            # Use a slightly larger legend out of the plot if there are many aircraft
            # Or turn off legend if n_aircraft is huge
            if len(self.trajectories) <= 20:
                plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left', fontsize='small')
                plt.tight_layout()
            
            plt.grid(True, linestyle='--', alpha=0.6)
            plt.savefig(png_path, dpi=300, bbox_inches='tight')
            plt.close()
        except Exception as e:
            return False, f"Failed to generate PNG map: {e}"

        return True, f"Trajectories saved to {csv_path} and {png_path}"
