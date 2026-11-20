from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256

from station.capture.calibration import ReferenceGroup
from station.capture.frames import CapturedFrame
from station.quality.window import WindowValues
from station.runs.config import CalibratedRunConfig, RunConfig
from station.runs.lifecycle import PausePoint


class ArchiveIntegrityError(ValueError):
    """The fixed archive evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class ArchiveHeader:
    result_id: str
    cutoff: PausePoint
    camera_id: str
    config: RunConfig
    values: WindowValues


@dataclass(frozen=True, slots=True)
class ArchivedFrame:
    position: int
    sequence: int
    data: bytes
    digest: str


@dataclass(frozen=True, slots=True)
class CalibratedHeader:
    result_id: str
    cutoff: PausePoint
    camera_id: str
    config: CalibratedRunConfig
    values: tuple[int, Fraction, int]


@dataclass(frozen=True, slots=True)
class CalibratedFrame:
    position: int
    sequence: int
    data: bytes
    reference_id: tuple[int, int]
    digest: str


@dataclass(frozen=True, slots=True)
class CalibratedReference:
    reference_id: tuple[int, int]
    dark_sequence: int
    dark_data: bytes
    bright_sequence: int
    bright_data: bytes
    digest: str


def frame_digest(position: int, sequence: int, data: bytes) -> str:
    digest = sha256()
    digest.update(f'{position:x}:{sequence:x}:'.encode('ascii'))
    digest.update(data)
    return digest.hexdigest()


def calibrated_frame_digest(position: int, sequence: int, reference_id: tuple[int, int], data: bytes) -> str:
    digest = sha256()
    digest.update(f'{position:x}:{sequence:x}:{reference_id[0]:x}:{reference_id[1]:x}:'.encode('ascii'))
    digest.update(data)
    return digest.hexdigest()


def calibrated_reference_digest(
    reference_id: tuple[int, int],
    dark_sequence: int,
    dark_data: bytes,
    bright_sequence: int,
    bright_data: bytes,
) -> str:
    digest = sha256()
    digest.update(f'{reference_id[0]:x}:{reference_id[1]:x}:{dark_sequence:x}:'.encode('ascii'))
    digest.update(dark_data)
    digest.update(f'{bright_sequence:x}:'.encode('ascii'))
    digest.update(bright_data)
    return digest.hexdigest()


def build_manifest(frames: Iterable[CapturedFrame]) -> tuple[ArchivedFrame, ...]:
    return tuple(
        ArchivedFrame(position, frame.sequence, frame.data, frame_digest(position, frame.sequence, frame.data))
        for position, frame in enumerate(frames)
    )


def build_calibrated_manifest(
    frames: Iterable[tuple[CapturedFrame, ReferenceGroup]],
) -> tuple[tuple[CalibratedFrame, ...], tuple[CalibratedReference, ...]]:
    """Freeze frames with their accepted-time groups plus the groups they use."""
    manifest = []
    used: dict[tuple[int, int], ReferenceGroup] = {}
    for position, (frame, group) in enumerate(frames):
        reference_id = group.reference_id
        manifest.append(CalibratedFrame(
            position,
            frame.sequence,
            frame.data,
            reference_id,
            calibrated_frame_digest(position, frame.sequence, reference_id, frame.data),
        ))
        if reference_id not in used:
            used[reference_id] = group
    references = tuple(
        CalibratedReference(
            reference_id,
            group.dark.sequence,
            group.dark.data,
            group.bright.sequence,
            group.bright.data,
            calibrated_reference_digest(
                reference_id, group.dark.sequence, group.dark.data,
                group.bright.sequence, group.bright.data,
            ),
        )
        for reference_id, group in sorted(used.items())
    )
    return tuple(manifest), references
