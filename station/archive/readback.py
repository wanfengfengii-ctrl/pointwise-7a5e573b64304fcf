from dataclasses import dataclass

from station.archive.manifest import ArchiveHeader, ArchivedFrame
from station.archive.store import ArchiveStore
from station.archive.verify import verify_calibrated_evidence, verify_stopped_evidence


@dataclass(frozen=True, slots=True)
class StoppedReview:
    header: ArchiveHeader
    frames: tuple[ArchivedFrame, ...]


def read_stopped(store: ArchiveStore, result_id: str) -> StoppedReview:
    header, frames = store.load_stopped(result_id)
    verify_stopped_evidence(header, frames)
    return StoppedReview(header, frames)


def read_calibrated(store: ArchiveStore, result_id: str) -> dict | None:
    """Deliver one calibrated result by number; None for a grayscale (V1) result."""
    loaded = store.load_calibrated(result_id)
    if loaded is None:
        return None
    header, frames, references = loaded
    values = verify_calibrated_evidence(header, frames, references)
    config = header.config
    return {
        'result_id': header.result_id,
        'camera_id': header.camera_id,
        'cutoff': header.cutoff,
        'capacity': config.capacity,
        'width': config.width,
        'height': config.height,
        'threshold': config.threshold,
        'values': values,
        'frames': tuple(
            {'sequence': frame.sequence, 'data': frame.data, 'reference_id': frame.reference_id}
            for frame in frames
        ),
        'references': {
            reference.reference_id: {
                'dark': (reference.dark_sequence, reference.dark_data),
                'bright': (reference.bright_sequence, reference.bright_data),
            }
            for reference in references
        },
    }
