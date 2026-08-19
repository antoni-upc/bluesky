"""Model-neutral requests exchanged with replaceable performance models."""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SpeedStepRequest:
    """Resolved BlueSky selected-speed request for one simulation tick."""

    target_tas: np.ndarray
    requested_acceleration: np.ndarray
    capture: np.ndarray
    next_tas: np.ndarray

    def validate(self, count):
        arrays = (self.target_tas, self.requested_acceleration,
                  self.capture, self.next_tas)
        if any(np.asarray(value).shape != (count,) for value in arrays):
            raise ValueError('Speed-step request has an invalid array shape')
        if not np.all(np.isfinite(self.target_tas)):
            raise ValueError('Speed-step target TAS is non-finite')
        if not np.all(np.isfinite(self.requested_acceleration)):
            raise ValueError('Speed-step acceleration is non-finite')
        if not np.all(np.isfinite(self.next_tas)):
            raise ValueError('Speed-step next TAS is non-finite')
        return self
