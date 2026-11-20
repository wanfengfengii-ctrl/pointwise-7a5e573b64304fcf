from dataclasses import dataclass

from station.capture.calibration import CalibrationLedger, ReferenceGroup
from station.capture.frames import CapturedFrame, validate_frame
from station.capture.pages import PageStore
from station.capture.ring import FrameRing, SlotRef
from station.runs.config import is_calibrated_config
from station.runs.registry import RunRegistry


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    slot: int
    high_water: int


class CaptureRecorder:
    def __init__(self, registry: RunRegistry) -> None:
        self._registry = registry
        self._rings: dict[str, FrameRing] = {}
        self._calibrations: dict[str, CalibrationLedger] = {}
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError('capture recorder is closed')

    def _current_ring(self, run_id: str) -> FrameRing | None:
        self._require_open()
        self._registry.get_run(run_id)
        return self._rings.get(run_id)

    def submit_reference(self, run_id: str, kind: str, sequence: int, data: bytes) -> None:
        self._require_open()
        run = self._registry.get_run(run_id)
        if not is_calibrated_config(run.config):
            raise ValueError('references require a calibrated run')
        run.lifecycle.require_running()
        ledger = self._calibrations.get(run_id)
        if ledger is None:
            ledger = CalibrationLedger()
            self._calibrations[run_id] = ledger
        ledger.submit(kind, sequence, data, run.config.frame_length)

    def append_frame(self, run_id: str, sequence: int, data: bytes) -> WriteReceipt:
        self._require_open()
        run = self._registry.get_run(run_id)
        run.lifecycle.require_running()
        frame = validate_frame(sequence, data, run.config.frame_length)
        ledger = self._calibrations.get(run_id)
        if is_calibrated_config(run.config) and (ledger is None or ledger.current_group is None):
            raise ValueError('no reference group is in effect yet')
        ring = self._rings.get(run_id)
        if ring is None:
            ring = FrameRing(run.config.capacity, PageStore(run.config.frame_length))
        ref = ring.append(frame)
        self._rings[run_id] = ring
        if ledger is not None:
            ledger.record_frame(frame.sequence, tuple(slot.sequence for slot in ring.iter_slot_refs()))
        return WriteReceipt(ref.slot, ref.sequence)

    def calibrated_window(self, run_id: str) -> tuple[tuple[CapturedFrame, ReferenceGroup], ...]:
        """Live frames of one calibrated run paired with their accepted-time groups."""
        self._require_open()
        run = self._registry.get_run(run_id)
        if not is_calibrated_config(run.config):
            raise ValueError('a calibrated window requires a calibrated run')
        ring = self._rings.get(run_id)
        ledger = self._calibrations.get(run_id)
        if ring is None or ledger is None:
            return ()
        return tuple(
            (CapturedFrame(ref.sequence, ring.read_page(ref.page)), ledger.group_for(ref.sequence))
            for ref in ring.iter_slot_refs()
        )

    def retained_references(self, run_id: str) -> tuple[tuple[int, int], ...]:
        """Reference group identifiers currently retained for one calibrated run."""
        self._require_open()
        run = self._registry.get_run(run_id)
        if not is_calibrated_config(run.config):
            raise ValueError('retained references require a calibrated run')
        ledger = self._calibrations.get(run_id)
        return () if ledger is None else tuple(sorted(ledger.retained_groups()))

    def high_water(self, run_id: str) -> int | None:
        ring = self._current_ring(run_id)
        return None if ring is None else ring.high_water

    def slot_index(self, run_id: str) -> tuple[SlotRef, ...]:
        ring = self._current_ring(run_id)
        return () if ring is None else tuple(ring.iter_slot_refs())

    def read_page(self, run_id: str, address: int) -> bytes:
        ring = self._current_ring(run_id)
        if ring is None:
            raise KeyError(address)
        return ring.read_page(address)

    def page_count(self, run_id: str) -> int:
        ring = self._current_ring(run_id)
        return 0 if ring is None else ring.page_count

    def close(self) -> None:
        self._closed = True
        for ring in self._rings.values():
            ring.close()
        self._rings.clear()
        self._calibrations.clear()
