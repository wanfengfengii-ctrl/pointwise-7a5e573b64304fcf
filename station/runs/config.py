from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True, slots=True)
class RunConfig:
    capacity: int
    width: int
    height: int
    threshold: int

    def __post_init__(self) -> None:
        for name in ('capacity', 'width', 'height'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if type(self.threshold) is not int or self.threshold < 0:
            raise ValueError('threshold must be a nonnegative integer')

    @property
    def frame_length(self) -> int:
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class CalibratedRunConfig:
    """Fixed shape and rational over-limit threshold of one calibrated run."""

    capacity: int
    width: int
    height: int
    threshold: Fraction

    def __post_init__(self) -> None:
        for name in ('capacity', 'width', 'height'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if type(self.threshold) is not Fraction or self.threshold < 0:
            raise ValueError('threshold must be a nonnegative rational')

    @property
    def frame_length(self) -> int:
        return self.width * self.height


def make_run_config(capacity: int, width: int, height: int, threshold: int) -> RunConfig:
    return RunConfig(capacity, width, height, threshold)


def make_calibrated_config(
    capacity: int, width: int, height: int, threshold: int | Fraction
) -> CalibratedRunConfig:
    if type(threshold) is int:
        threshold = Fraction(threshold)
    elif type(threshold) is not Fraction:
        raise ValueError('threshold must be a nonnegative integer or Fraction')
    return CalibratedRunConfig(capacity, width, height, threshold)


def is_calibrated_config(config: RunConfig | CalibratedRunConfig) -> bool:
    return type(config) is CalibratedRunConfig
