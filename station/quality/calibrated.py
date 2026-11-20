from collections.abc import Iterable
from fractions import Fraction

from station.capture.calibration import ReferenceGroup
from station.capture.frames import CapturedFrame


def normalized_frame_total(frame_data: bytes, dark_data: bytes, bright_data: bytes) -> Fraction:
    """Sum the per-pixel dark/bright normalized values clamped to [0, 1], exactly."""
    if not (len(frame_data) == len(dark_data) == len(bright_data)):
        raise ValueError('frame and reference lengths differ')
    total = Fraction(0)
    for pixel, dark, bright in zip(frame_data, dark_data, bright_data):
        value = Fraction(pixel - dark, bright - dark)
        if value < 0:
            value = Fraction(0)
        elif value > 1:
            value = Fraction(1)
        total += value
    return total


def measure_calibrated_window(
    frames: Iterable[tuple[CapturedFrame, ReferenceGroup]], threshold: Fraction
) -> tuple[int, Fraction, int]:
    """Measure one source-ordered window of frames paired with their reference groups."""
    count = 0
    total = Fraction(0)
    over_limit = 0
    previous = None
    for frame, group in frames:
        if previous is not None and frame.sequence <= previous:
            raise ValueError('window frames must be in strictly increasing source order')
        frame_total = normalized_frame_total(frame.data, group.dark.data, group.bright.data)
        count += 1
        total += frame_total
        if frame_total > threshold:
            over_limit += 1
        previous = frame.sequence
    return (count, total, over_limit)
