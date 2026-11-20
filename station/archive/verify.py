from fractions import Fraction

from station.archive.manifest import (
    ArchiveHeader,
    ArchiveIntegrityError,
    ArchivedFrame,
    CalibratedFrame,
    CalibratedHeader,
    CalibratedReference,
    calibrated_frame_digest,
    calibrated_reference_digest,
    frame_digest,
)
from station.capture.calibration import ReferenceGroup, ReferencePiece
from station.capture.frames import CapturedFrame
from station.quality.calibrated import measure_calibrated_window
from station.quality.window import measure_window


def verify_stopped_evidence(header: ArchiveHeader, frames: tuple[ArchivedFrame, ...]) -> None:
    count = header.values.frame_count
    if count < 1 or count > header.config.capacity or len(frames) != count:
        raise ArchiveIntegrityError('manifest does not cover the fixed frame count')
    if frames[-1].sequence != header.cutoff.high_water:
        raise ArchiveIntegrityError('manifest does not end at the pause cutoff')

    for position, frame in enumerate(frames):
        if type(frame.position) is not int or frame.position != position:
            raise ArchiveIntegrityError('manifest positions are incomplete or out of order')
        if type(frame.data) is not bytes or len(frame.data) != header.config.frame_length:
            raise ArchiveIntegrityError(f'frame at position {position} has invalid pixel bytes')
        if frame_digest(position, frame.sequence, frame.data) != frame.digest:
            raise ArchiveIntegrityError(f'frame at position {position} disagrees with its digest')

    try:
        measured = measure_window(
            (CapturedFrame(frame.sequence, frame.data) for frame in frames), header.config
        )
    except ValueError as exc:
        raise ArchiveIntegrityError('manifest source order or frame shape is invalid') from exc
    if measured != header.values:
        raise ArchiveIntegrityError('fixed quality values disagree with the original frames')


def _is_reference_id(reference_id) -> bool:
    return (
        type(reference_id) is tuple
        and len(reference_id) == 2
        and all(type(part) is int for part in reference_id)
    )


def verify_calibrated_evidence(
    header: CalibratedHeader,
    frames: tuple[CalibratedFrame, ...],
    references: tuple[CalibratedReference, ...],
) -> tuple[int, Fraction, int]:
    """Recheck the fixed calibrated evidence and return the recomputed values."""
    config = header.config
    count, _, _ = header.values
    if type(count) is not int or count < 1 or count > config.capacity or len(frames) != count:
        raise ArchiveIntegrityError('manifest does not cover the fixed frame count')
    if frames[-1].sequence != header.cutoff.high_water:
        raise ArchiveIntegrityError('manifest does not end at the pause cutoff')

    for position, frame in enumerate(frames):
        if type(frame.position) is not int or frame.position != position:
            raise ArchiveIntegrityError('manifest positions are incomplete or out of order')
        if type(frame.data) is not bytes or len(frame.data) != config.frame_length:
            raise ArchiveIntegrityError(f'frame at position {position} has invalid pixel bytes')
        if not _is_reference_id(frame.reference_id):
            raise ArchiveIntegrityError(f'frame at position {position} has an invalid reference binding')
        expected = calibrated_frame_digest(position, frame.sequence, frame.reference_id, frame.data)
        if expected != frame.digest:
            raise ArchiveIntegrityError(f'frame at position {position} disagrees with its digest')

    groups: dict[tuple[int, int], ReferenceGroup] = {}
    for reference in references:
        reference_id = reference.reference_id
        if not _is_reference_id(reference_id) or reference_id in groups:
            raise ArchiveIntegrityError('reference group identifiers are invalid or duplicated')
        if (reference.dark_sequence, reference.bright_sequence) != reference_id:
            raise ArchiveIntegrityError('reference group sequences disagree with their binding')
        if type(reference.dark_data) is not bytes or len(reference.dark_data) != config.frame_length:
            raise ArchiveIntegrityError('dark reference has invalid pixel bytes')
        if type(reference.bright_data) is not bytes or len(reference.bright_data) != config.frame_length:
            raise ArchiveIntegrityError('bright reference has invalid pixel bytes')
        if not all(bright > dark for dark, bright in zip(reference.dark_data, reference.bright_data)):
            raise ArchiveIntegrityError('reference pair violates strict bright-over-dark ordering')
        expected = calibrated_reference_digest(
            reference_id, reference.dark_sequence, reference.dark_data,
            reference.bright_sequence, reference.bright_data,
        )
        if expected != reference.digest:
            raise ArchiveIntegrityError('reference group disagrees with its digest')
        groups[reference_id] = ReferenceGroup(
            ReferencePiece('dark', reference.dark_sequence, reference.dark_data),
            ReferencePiece('bright', reference.bright_sequence, reference.bright_data),
        )

    if set(groups) != {frame.reference_id for frame in frames}:
        raise ArchiveIntegrityError('reference groups do not match the frame bindings')

    try:
        measured = measure_calibrated_window(
            (
                (CapturedFrame(frame.sequence, frame.data), groups[frame.reference_id])
                for frame in frames
            ),
            config.threshold,
        )
    except ValueError as exc:
        raise ArchiveIntegrityError('manifest source order or frame shape is invalid') from exc
    if measured != tuple(header.values):
        raise ArchiveIntegrityError('fixed calibrated values disagree with the original frames')
    return measured
