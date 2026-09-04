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


@dataclass(frozen=True)
class SpeedStepResult:
    """Performance-model result for an applied selected-speed step."""

    request: SpeedStepRequest
    applied_acceleration: np.ndarray
    capture: np.ndarray
    next_tas: np.ndarray

    def validate(self, count):
        self.request.validate(count)
        arrays = (self.applied_acceleration, self.capture, self.next_tas)
        if any(np.asarray(value).shape != (count,) for value in arrays):
            raise ValueError('Speed-step result has an invalid array shape')
        if not np.all(np.isfinite(self.applied_acceleration)):
            raise ValueError('Applied speed-step acceleration is non-finite')
        if not np.all(np.isfinite(self.next_tas)):
            raise ValueError('Applied speed-step next TAS is non-finite')
        return self
