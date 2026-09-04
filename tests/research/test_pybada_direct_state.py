from types import SimpleNamespace

import numpy as np

from bluesky.stack.cmdparser import CommandRejected
from bluesky.traffic.traffic import Traffic


def provisional_traffic(accepted):
    values = dict(
        id=['A1'], lat=np.array([41.0]), lon=np.array([2.0]),
        alt=np.array([3000.0]), selalt=np.array([3000.0]),
        hdg=np.array([90.0]), tas=np.array([150.0]), selspd=np.array([150.0]),
        vs=np.array([0.0]), p=np.array([70000.0]), rho=np.array([0.9]),
        Temp=np.array([270.0]), pressure_alt=np.array([3000.0]),
        cas=np.array([140.0]), M=np.array([0.5]), dtemp=np.array([0.0]),
        swvnav=np.array([True]), ap=SimpleNamespace(trk=np.array([90.0])))
    traffic = SimpleNamespace(**values)
    traffic.update_atmosphere = lambda: None
    traffic.perf = SimpleNamespace(
        requires_synced_direct_state=True,
        assess_direct_state=lambda idx, previous: (accepted, 'policy=ENFORCE, reason=ALTITUDE_MAX'))
    return traffic


def test_move_enforce_rejection_rolls_back_every_provisional_field():
    traffic = provisional_traffic(False)
    result = Traffic.move(traffic, 0, 42.0, 3.0, alt=20_000.0, hdg=180.0, vspd=5.0)
    assert not result[0] and isinstance(result[1], CommandRejected)
    assert 'A1' in result[1] and 'ALTITUDE_MAX' in result[1]
    assert traffic.lat[0] == 41.0 and traffic.lon[0] == 2.0
    assert traffic.alt[0] == 3000.0 and traffic.selalt[0] == 3000.0
    assert traffic.hdg[0] == 90.0 and traffic.ap.trk[0] == 90.0
    assert traffic.vs[0] == 0.0 and traffic.swvnav[0]


def test_move_report_accepts_complete_provisional_state():
    traffic = provisional_traffic(True)
    assert Traffic.move(traffic, 0, 42.0, 3.0, alt=5000.0, hdg=180.0, vspd=5.0)
    assert traffic.lat[0] == 42.0 and traffic.lon[0] == 3.0
    assert traffic.alt[0] == 5000.0 and traffic.selalt[0] == 5000.0
    assert traffic.hdg[0] == 180.0 and traffic.ap.trk[0] == 180.0
    assert traffic.vs[0] == 5.0 and not traffic.swvnav[0]
