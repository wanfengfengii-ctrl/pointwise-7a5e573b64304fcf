from fractions import Fraction

from station.capture.frames import CapturedFrame
from station.capture.recording import CaptureRecorder
from station.quality.calibrated import measure_calibrated_window
from station.quality.window import WindowValues, measure_window
from station.runs.config import is_calibrated_config
from station.runs.registry import RunRegistry


def live_quality(registry: RunRegistry, recorder: CaptureRecorder, run_id: str) -> WindowValues:
    run = registry.get_run(run_id)
    frames = (
        CapturedFrame(ref.sequence, recorder.read_page(run_id, ref.page))
        for ref in recorder.slot_index(run_id)
    )
    return measure_window(frames, run.config)


def live_calibrated(
    registry: RunRegistry, recorder: CaptureRecorder, run_id: str
) -> tuple[int, Fraction, int]:
    run = registry.get_run(run_id)
    if not is_calibrated_config(run.config):
        raise ValueError('live_calibrated requires a calibrated run')
    return measure_calibrated_window(recorder.calibrated_window(run_id), run.config.threshold)
