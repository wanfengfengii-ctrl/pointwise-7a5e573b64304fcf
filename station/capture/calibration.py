from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReferencePiece:
    """One accepted dark or bright reference original."""

    kind: str
    sequence: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ReferenceGroup:
    """A paired dark/bright reference, identified by both source sequences."""

    dark: ReferencePiece
    bright: ReferencePiece

    @property
    def reference_id(self) -> tuple[int, int]:
        return (self.dark.sequence, self.bright.sequence)


class CalibrationLedger:
    """Reference state of one calibrated run.

    Keeps at most one pending piece, the currently effective group, and the
    groups still referenced by live frames; nothing else is retained.
    """

    def __init__(self) -> None:
        self._pending: ReferencePiece | None = None
        self._current: ReferenceGroup | None = None
        self._groups: dict[tuple[int, int], ReferenceGroup] = {}
        self._high_water: int | None = None
        self._attribution: dict[int, tuple[int, int]] = {}

    @property
    def current_group(self) -> ReferenceGroup | None:
        return self._current

    @property
    def pending(self) -> ReferencePiece | None:
        return self._pending

    def retained_groups(self) -> dict[tuple[int, int], ReferenceGroup]:
        return dict(self._groups)

    def submit(self, kind: str, sequence: int, data: bytes, frame_length: int) -> None:
        if type(kind) is not str or kind not in ('dark', 'bright'):
            raise ValueError("reference kind must be 'dark' or 'bright'")
        if type(sequence) is not int:
            raise ValueError('reference sequence must be an integer')
        if type(data) is not bytes or len(data) != frame_length:
            raise ValueError(f'reference must contain exactly {frame_length} bytes')
        if self._high_water is not None and sequence <= self._high_water:
            raise ValueError('reference sequence must exceed the last accepted reference sequence')
        piece = ReferencePiece(kind, sequence, data)
        if self._pending is None:
            self._pending = piece
            self._high_water = sequence
            return
        if self._pending.kind == kind:
            raise ValueError('another reference of the same kind is still pending')
        dark = self._pending if kind == 'bright' else piece
        bright = piece if kind == 'bright' else self._pending
        if not all(bright_pixel > dark_pixel for dark_pixel, bright_pixel in zip(dark.data, bright.data)):
            raise ValueError('every bright pixel must strictly exceed the dark pixel')
        self._current = ReferenceGroup(dark, bright)
        self._groups[self._current.reference_id] = self._current
        self._pending = None
        self._high_water = sequence
        self._prune_groups()

    def record_frame(self, sequence: int, live_sequences: tuple[int, ...]) -> None:
        live = set(live_sequences)
        self._attribution = {seq: ref for seq, ref in self._attribution.items() if seq in live}
        self._attribution[sequence] = self._current.reference_id
        self._prune_groups()

    def group_for(self, sequence: int) -> ReferenceGroup:
        return self._groups[self._attribution[sequence]]

    def _prune_groups(self) -> None:
        referenced = set(self._attribution.values())
        current_id = None if self._current is None else self._current.reference_id
        self._groups = {
            reference_id: group
            for reference_id, group in self._groups.items()
            if reference_id == current_id or reference_id in referenced
        }
