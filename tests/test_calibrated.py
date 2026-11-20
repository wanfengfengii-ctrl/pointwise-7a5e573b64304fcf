import json
import sqlite3
import unittest
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from station.archive.issue import issue_calibrated, issue_stopped
from station.archive.manifest import ArchiveIntegrityError, calibrated_reference_digest
from station.archive.readback import read_calibrated
from station.archive.store import ArchiveStore
from station.capture.source import CameraFrameEvent, SimulatedCamera
from station.handover.board import handover_board
from station.handover.tickets import TicketStatus
from station.runs.lifecycle import PausePoint
from station.runs.session import RecordingSession


class CalibratedFixture:
    def __init__(self, case, *, capacity=2, width=1, height=1, threshold=Fraction(1, 2)):
        self.session = RecordingSession(['line-cam', 'backup-cam'])
        case.addCleanup(self.session.close)
        self.run = self.session.open_calibrated_run('line-cam', capacity, width, height, threshold)
        self.source = SimulatedCamera(self.session.capture, self.run.run_id)
        case.addCleanup(self.source.close)

    def step(self, sequence, data):
        return self.source.step(CameraFrameEvent(sequence, data))

    def submit(self, kind, sequence, data):
        return self.session.submit_reference(self.run.run_id, kind, sequence, data)

    def pair(self, dark_sequence, dark_data, bright_sequence, bright_data):
        self.submit('dark', dark_sequence, dark_data)
        self.submit('bright', bright_sequence, bright_data)


class CalibratedCase(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder) / 'station.sqlite3'
        self.store = self.enterContext(ArchiveStore(self.path))
        self.camera = CalibratedFixture(self)
        self.session = self.camera.session
        self.run_id = self.camera.run.run_id

    def calibrated_counts(self, result_id=None):
        tables = ('calibrated_results', 'calibrated_frames', 'calibrated_references', 'calibrated_links')
        with closing(sqlite3.connect(self.path)) as connection:
            if result_id is None:
                return tuple(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables)
            return tuple(
                connection.execute(f'SELECT COUNT(*) FROM {table} WHERE result_id = ?', (result_id,)).fetchone()[0]
                for table in tables
            )

    def change_archive(self, sql, parameters=()):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute(sql, parameters)


class TestCalibratedOpen(unittest.TestCase):
    def test_threshold_accepted_forms(self):
        session = RecordingSession(['cam-a', 'cam-b', 'cam-c'])
        self.addCleanup(session.close)
        integer = session.open_calibrated_run('cam-a', 2, 1, 1, 3)
        self.assertEqual(integer.config.threshold, Fraction(3))
        rational = session.open_calibrated_run('cam-b', 2, 1, 1, Fraction(1, 2))
        self.assertEqual(rational.config.threshold, Fraction(1, 2))
        zero = session.open_calibrated_run('cam-c', 2, 1, 1, Fraction(0, 1))
        self.assertEqual(zero.config.threshold, Fraction(0))

    def test_threshold_rejected_forms(self):
        session = RecordingSession(['line-cam'])
        self.addCleanup(session.close)
        for bad in (True, False, -1, Fraction(-1, 2), 0.5, '1/2', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                session.open_calibrated_run('line-cam', 2, 1, 1, bad)
        for settings in ((0, 1, 1), (2, 0, 1), (2, 1, 0), (2, True, 1), (2.0, 1, 1)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                session.open_calibrated_run('line-cam', *settings, Fraction(1))

    def test_camera_registration_and_occupancy(self):
        session = RecordingSession(['line-cam'])
        self.addCleanup(session.close)
        with self.assertRaises(KeyError):
            session.open_calibrated_run('unknown-cam', 2, 1, 1, 1)
        run = session.open_calibrated_run('line-cam', 2, 1, 1, 1)
        with self.assertRaises(ValueError):
            session.open_calibrated_run('line-cam', 2, 1, 1, 1)
        with self.assertRaises(ValueError):
            session.open_run('line-cam', 2, 1, 1, 1)
        session.pause_run(run.run_id)
        second = session.open_calibrated_run('line-cam', 2, 1, 1, 1)
        self.assertNotEqual(second.run_id, run.run_id)


class TestCalibratedAcceptance(CalibratedCase):
    def test_frames_rejected_before_first_group(self):
        with self.assertRaises(ValueError):
            self.camera.step(10, b'\x06')
        self.camera.submit('dark', 1, b'\x00')
        with self.assertRaises(ValueError):
            self.camera.step(10, b'\x06')
        self.camera.submit('bright', 2, b'\x0a')
        receipt = self.camera.step(10, b'\x06')
        self.assertEqual((receipt.slot, receipt.high_water), (0, 10))
        self.assertEqual(self.session.capture.high_water(self.run_id), 10)

    def test_pending_and_pairing_rules(self):
        self.camera.submit('dark', 1, b'\x05')
        with self.assertRaises(ValueError):
            self.camera.submit('dark', 2, b'\x04')
        with self.assertRaises(ValueError):
            self.camera.submit('bright', 2, b'\x05')
        self.camera.submit('bright', 2, b'\x0f')
        self.assertEqual(self.camera.step(10, b'\x06').slot, 0)

    def test_rejection_keeps_group_pending_and_sequences(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.camera.submit('dark', 3, b'\x05')
        with self.assertRaises(ValueError):
            self.camera.submit('bright', 4, b'\x05')
        with self.assertRaises(ValueError):
            self.camera.submit('bright', 3, b'\x0f')
        self.camera.submit('bright', 4, b'\x0f')
        self.camera.step(20, b'\x07')
        cutoff = self.session.pause_run(self.run_id)
        review = read_calibrated(self.store, issue_calibrated(self.session, self.store, self.run_id, cutoff))
        self.assertEqual([frame['reference_id'] for frame in review['frames']], [(1, 2), (3, 4)])

    def test_bright_may_arrive_first(self):
        self.camera.submit('bright', 1, b'\x0a')
        self.camera.submit('dark', 2, b'\x05')
        self.camera.step(10, b'\x06')
        cutoff = self.session.pause_run(self.run_id)
        review = read_calibrated(self.store, issue_calibrated(self.session, self.store, self.run_id, cutoff))
        self.assertEqual(review['frames'][0]['reference_id'], (2, 1))
        self.assertEqual(review['references'], {(2, 1): {'dark': (2, b'\x05'), 'bright': (1, b'\x0a')}})

    def test_shared_reference_sequence_is_strictly_increasing(self):
        self.camera.submit('dark', 3, b'\x00')
        with self.assertRaises(ValueError):
            self.camera.submit('bright', 3, b'\x0a')
        with self.assertRaises(ValueError):
            self.camera.submit('bright', 2, b'\x0a')
        self.camera.submit('bright', 4, b'\x0a')
        with self.assertRaises(ValueError):
            self.camera.submit('dark', 4, b'\x01')
        self.camera.submit('dark', 5, b'\x01')

    def test_invalid_reference_inputs(self):
        for kind, sequence, data in (
            ('gray', 1, b'\x00'),
            ('DARK', 1, b'\x00'),
            ('dark', True, b'\x00'),
            ('dark', 1.0, b'\x00'),
            ('dark', 1, b'\x00\x01'),
            ('dark', 1, bytearray(1)),
            ('dark', 1, 0),
        ):
            with self.subTest(kind=kind, sequence=sequence, data=data), self.assertRaises(ValueError):
                self.camera.submit(kind, sequence, data)
        with self.assertRaises(KeyError):
            self.session.submit_reference('unknown-run', 'dark', 1, b'\x00')
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.assertEqual(self.camera.step(10, b'\x06').slot, 0)

    def test_mode_specific_entries_reject_other_mode(self):
        gray = self.session.open_run('backup-cam', 2, 1, 1, 5)
        with self.assertRaises(ValueError):
            self.session.submit_reference(gray.run_id, 'dark', 1, b'\x00')
        with self.assertRaises(ValueError):
            self.session.live_calibrated(gray.run_id)
        with self.assertRaises(ValueError):
            issue_calibrated(self.session, self.store, gray.run_id, PausePoint(gray.run_id, None))
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        cutoff = self.session.pause_run(self.run_id)
        with self.assertRaises(ValueError):
            issue_stopped(self.session, self.store, self.run_id, cutoff)
        self.assertEqual(self.calibrated_counts(), (0, 0, 0, 0))

    def test_paused_run_rejects_references_and_frames(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.session.pause_run(self.run_id)
        with self.assertRaises(ValueError):
            self.camera.submit('dark', 3, b'\x01')
        with self.assertRaises(ValueError):
            self.camera.step(20, b'\x07')

    def test_closed_resources(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.source.close()
        self.session.close()
        with self.assertRaises(RuntimeError):
            self.camera.step(10, b'\x06')
        with self.assertRaises(RuntimeError):
            self.session.submit_reference(self.run_id, 'dark', 3, b'\x01')
        with self.assertRaises(RuntimeError):
            self.session.live_calibrated(self.run_id)


class TestCalibratedMeasurement(CalibratedCase):
    def test_documented_clamping_example(self):
        run = self.session.open_calibrated_run('backup-cam', 2, 2, 1, 1)
        self.session.submit_reference(run.run_id, 'dark', 1, bytes([20, 20]))
        self.session.submit_reference(run.run_id, 'bright', 2, bytes([40, 40]))
        self.session.append_frame(run.run_id, 10, bytes([19, 41]))
        self.assertEqual(self.session.live_calibrated(run.run_id), (1, Fraction(1), 0))

    def test_exact_total_and_strict_threshold(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x05')
        self.camera.step(20, b'\x06')
        values = self.session.live_calibrated(self.run_id)
        self.assertEqual(values, (2, Fraction(11, 10), 1))
        self.assertIsInstance(values[1], Fraction)

    def test_empty_window_and_unknown_run(self):
        values = self.session.live_calibrated(self.run_id)
        self.assertEqual(values, (0, 0, 0))
        self.assertIsInstance(values[1], Fraction)
        with self.assertRaises(KeyError):
            self.session.live_calibrated('unknown-run')

    def test_live_view_after_pause(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.session.pause_run(self.run_id)
        self.assertEqual(self.session.live_calibrated(self.run_id), (1, Fraction(3, 5), 1))


class TestCalibratedRetention(CalibratedCase):
    def test_only_current_pending_and_live_groups_are_kept(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.camera.pair(3, b'\x05', 4, b'\x0f')
        self.camera.step(20, b'\x07')
        self.camera.pair(5, b'\x05', 6, b'\x0f')
        self.assertEqual(self.session.capture.retained_references(self.run_id), ((1, 2), (3, 4), (5, 6)))
        self.camera.pair(7, b'\x05', 8, b'\x0f')
        self.assertEqual(self.session.capture.retained_references(self.run_id), ((1, 2), (3, 4), (7, 8)))
        self.camera.step(30, b'\x08')
        self.assertEqual(self.session.capture.retained_references(self.run_id), ((3, 4), (7, 8)))
        self.camera.submit('dark', 9, b'\x02')
        self.assertEqual(self.session.capture.retained_references(self.run_id), ((3, 4), (7, 8)))

    def test_unused_current_group_and_pending_not_delivered(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.camera.pair(3, b'\x05', 4, b'\x0f')
        self.camera.submit('dark', 5, b'\x01')
        cutoff = self.session.pause_run(self.run_id)
        review = read_calibrated(self.store, issue_calibrated(self.session, self.store, self.run_id, cutoff))
        self.assertEqual([frame['reference_id'] for frame in review['frames']], [(1, 2)])
        self.assertEqual(set(review['references']), {(1, 2)})


class TestCalibrationExample(CalibratedCase):
    @staticmethod
    def load_sample():
        path = Path(__file__).resolve().parent.parent / 'examples' / 'calibration.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def replay(self, events, source, run_id):
        receipts = []
        for event in events:
            data = bytes(event['pixels'])
            if event['op'] == 'frame':
                action = lambda: source.step(CameraFrameEvent(event['sequence'], data))
            else:
                action = lambda: self.session.submit_reference(run_id, event['kind'], event['sequence'], data)
            if event.get('reject'):
                with self.assertRaises(ValueError):
                    action()
            elif event['op'] == 'frame':
                receipts.append(action())
            else:
                action()
        return receipts

    @staticmethod
    def expected_values(checkpoint):
        count, (numerator, denominator), over_limit = checkpoint['values']
        return (count, Fraction(numerator, denominator), over_limit)

    def test_example_checkpoints(self):
        sample = self.load_sample()
        first, second = sample['checkpoints']

        receipts = self.replay(sample['events'][:first['after_events']], self.camera.source, self.run_id)
        self.assertEqual([(r.slot, r.high_water) for r in receipts], [(0, 10), (1, 20)])
        self.assertEqual(self.session.live_calibrated(self.run_id), self.expected_values(first))
        cutoff = self.session.pause_run(self.run_id)
        review = read_calibrated(self.store, issue_calibrated(self.session, self.store, self.run_id, cutoff))
        self.assertEqual(
            [(frame['sequence'],) + frame['reference_id'] for frame in review['frames']],
            [tuple(item) for item in first['frames']],
        )
        self.assertEqual(review['values'], self.expected_values(first))
        self.assertEqual(set(review['references']), {(1, 2), (3, 4)})

        run = self.session.open_calibrated_run('line-cam', 2, 1, 1, Fraction(1, 2))
        source = SimulatedCamera(self.session.capture, run.run_id)
        self.addCleanup(source.close)
        receipts = self.replay(sample['events'][:second['after_events']], source, run.run_id)
        self.assertEqual([(r.slot, r.high_water) for r in receipts], [(0, 10), (1, 20), (0, 30)])
        self.assertEqual(self.session.live_calibrated(run.run_id), self.expected_values(second))
        cutoff = self.session.pause_run(run.run_id)
        review = read_calibrated(self.store, issue_calibrated(self.session, self.store, run.run_id, cutoff))
        self.assertEqual(
            [(frame['sequence'],) + frame['reference_id'] for frame in review['frames']],
            [tuple(item) for item in second['frames']],
        )
        self.assertEqual(review['values'], self.expected_values(second))
        self.assertEqual(set(review['references']), {(3, 4), (7, 8)})
        earlier, later = review['references'][(3, 4)], review['references'][(7, 8)]
        self.assertEqual((earlier['dark'][1], earlier['bright'][1]), (later['dark'][1], later['bright'][1]))
        self.assertNotEqual((earlier['dark'][0], earlier['bright'][0]), (later['dark'][0], later['bright'][0]))


class TestCalibratedIssue(CalibratedCase):
    def prepare(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.camera.step(20, b'\x07')
        return self.session.pause_run(self.run_id)

    def test_issue_and_successful_reissue(self):
        cutoff = self.prepare()
        self.assertEqual(cutoff, PausePoint(self.run_id, 20))
        ticket, = handover_board(self.store, self.run_id, self.session)
        self.assertIs(ticket.status, TicketStatus.PENDING)
        result_id = issue_calibrated(self.session, self.store, self.run_id, cutoff)
        self.assertEqual(issue_calibrated(self.session, self.store, self.run_id, cutoff), result_id)
        ticket, = handover_board(self.store, self.run_id, self.session)
        self.assertEqual((ticket.status, ticket.result_id), (TicketStatus.SIGNED, result_id))
        self.assertEqual(self.calibrated_counts(), (1, 2, 1, 1))
        review = read_calibrated(self.store, result_id)
        self.assertEqual(review['values'], (2, Fraction(13, 10), 2))

    def test_empty_window_cannot_issue(self):
        cutoff = self.session.pause_run(self.run_id)
        self.assertIsNone(cutoff.high_water)
        with self.assertRaises(ValueError):
            issue_calibrated(self.session, self.store, self.run_id, cutoff)
        self.assertEqual(self.calibrated_counts(), (0, 0, 0, 0))

    def test_active_run_and_wrong_cutoff(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        with self.assertRaises(ValueError):
            issue_calibrated(self.session, self.store, self.run_id, PausePoint(self.run_id, 10))
        cutoff = self.session.pause_run(self.run_id)
        for wrong in (PausePoint(self.run_id, 5), PausePoint('wrong-run', cutoff.high_water)):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                issue_calibrated(self.session, self.store, self.run_id, wrong)
        with self.assertRaises(KeyError):
            issue_calibrated(self.session, self.store, 'unknown-run', cutoff)
        self.assertEqual(self.calibrated_counts(), (0, 0, 0, 0))

    def test_commit_retry(self):
        cutoff = self.prepare()
        attempts = []

        def fail_once(result_id):
            attempts.append(result_id)
            if len(attempts) == 1:
                self.assertEqual(self.calibrated_counts(), (0, 0, 0, 0))
                self.assertIsNone(self.store.find_calibrated_result(cutoff))
                raise OSError('publication interrupted before commit')

        with ArchiveStore(self.path, before_commit=fail_once) as writer:
            with self.assertRaisesRegex(OSError, 'before commit'):
                issue_calibrated(self.session, writer, self.run_id, cutoff)
            self.assertEqual(self.calibrated_counts(), (0, 0, 0, 0))
            with self.assertRaises(KeyError):
                read_calibrated(self.store, attempts[0])
            ticket, = handover_board(self.store, self.run_id, self.session)
            self.assertIs(ticket.status, TicketStatus.PENDING)
            result_id = issue_calibrated(self.session, writer, self.run_id, cutoff)
            self.assertEqual(issue_calibrated(self.session, writer, self.run_id, cutoff), result_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.calibrated_counts(), (1, 2, 1, 1))
        self.assertEqual(self.calibrated_counts(result_id), (1, 2, 1, 1))
        review = read_calibrated(self.store, result_id)
        self.assertEqual(review['values'], (2, Fraction(13, 10), 2))

    def test_write_retry(self):
        cutoff = self.prepare()
        attempts = []

        def fail_first_frame(result_id, position):
            attempts.append(position)
            if len(attempts) == 1:
                raise OSError('frame write interrupted')

        with ArchiveStore(self.path, after_frame=fail_first_frame) as writer:
            with self.assertRaisesRegex(OSError, 'frame write'):
                issue_calibrated(self.session, writer, self.run_id, cutoff)
            self.assertEqual(self.calibrated_counts(), (0, 0, 0, 0))
            result_id = issue_calibrated(self.session, writer, self.run_id, cutoff)
        self.assertEqual(attempts, [0, 0, 1])
        self.assertEqual(self.calibrated_counts(result_id), (1, 2, 1, 1))
        review = read_calibrated(self.store, result_id)
        self.assertEqual([frame['sequence'] for frame in review['frames']], [10, 20])


class TestCalibratedReadback(CalibratedCase):
    def issue_and_close(self):
        self.camera.pair(1, b'\x00', 2, b'\x0a')
        self.camera.step(10, b'\x06')
        self.camera.pair(3, b'\x05', 4, b'\x0f')
        self.camera.step(20, b'\x07')
        cutoff = self.session.pause_run(self.run_id)
        result_id = issue_calibrated(self.session, self.store, self.run_id, cutoff)
        self.camera.source.close()
        self.session.close()
        self.store.close()
        return result_id, cutoff

    def issue_same_byte_groups(self):
        self.camera.pair(1, b'\x05', 2, b'\x0f')
        self.camera.step(10, b'\x06')
        self.camera.pair(3, b'\x05', 4, b'\x0f')
        self.camera.step(20, b'\x06')
        cutoff = self.session.pause_run(self.run_id)
        result_id = issue_calibrated(self.session, self.store, self.run_id, cutoff)
        self.camera.source.close()
        self.session.close()
        self.store.close()
        return result_id

    def test_reopen_review(self):
        result_id, cutoff = self.issue_and_close()
        with ArchiveStore(self.path) as reopened:
            ticket, = handover_board(reopened, self.run_id)
            self.assertIs(ticket.status, TicketStatus.SIGNED)
            self.assertEqual(ticket.result_id, result_id)
            review = read_calibrated(reopened, ticket.result_id)
        self.assertEqual(review['result_id'], result_id)
        self.assertEqual(review['camera_id'], 'line-cam')
        self.assertEqual(review['cutoff'], cutoff)
        self.assertEqual((review['capacity'], review['width'], review['height']), (2, 1, 1))
        self.assertEqual(review['threshold'], Fraction(1, 2))
        self.assertIsInstance(review['threshold'], Fraction)
        self.assertEqual(review['values'], (2, Fraction(4, 5), 1))
        self.assertEqual(
            review['frames'],
            (
                {'sequence': 10, 'data': b'\x06', 'reference_id': (1, 2)},
                {'sequence': 20, 'data': b'\x07', 'reference_id': (3, 4)},
            ),
        )
        self.assertEqual(review['references'], {
            (1, 2): {'dark': (1, b'\x00'), 'bright': (2, b'\x0a')},
            (3, 4): {'dark': (3, b'\x05'), 'bright': (4, b'\x0f')},
        })

    def test_grayscale_result_reads_as_none(self):
        gray = self.session.open_run('backup-cam', 2, 1, 1, 5)
        self.session.append_frame(gray.run_id, 10, b'\x03')
        gray_cutoff = self.session.pause_run(gray.run_id)
        gray_result = issue_stopped(self.session, self.store, gray.run_id, gray_cutoff)
        self.assertIsNone(read_calibrated(self.store, gray_result))
        with self.assertRaises(KeyError):
            read_calibrated(self.store, 'unknown-result')

    def test_missing_frame(self):
        result_id, _ = self.issue_and_close()
        self.change_archive('DELETE FROM calibrated_frames WHERE result_id = ? AND position = 1', (result_id,))
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_rebound_frame_with_same_bytes(self):
        result_id = self.issue_same_byte_groups()
        self.change_archive(
            "UPDATE calibrated_frames SET reference_id = '1:2' WHERE result_id = ? AND position = 1",
            (result_id,),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_tampered_frame_bytes(self):
        result_id, _ = self.issue_and_close()
        self.change_archive(
            'UPDATE calibrated_frames SET original_bytes = ? WHERE result_id = ? AND position = 0',
            (b'\x05', result_id),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_missing_reference(self):
        result_id, _ = self.issue_and_close()
        self.change_archive(
            "DELETE FROM calibrated_references WHERE result_id = ? AND reference_id = '3:4'",
            (result_id,),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_extra_reference(self):
        result_id, _ = self.issue_and_close()
        digest = calibrated_reference_digest((9, 10), 9, b'\x01', 10, b'\x02')
        self.change_archive(
            '''INSERT INTO calibrated_references (
                   result_id, reference_id, dark_sequence, dark_bytes, bright_sequence, bright_bytes, digest
               ) VALUES (?, '9:a', '9', ?, 'a', ?, ?)''',
            (result_id, b'\x01', b'\x02', digest),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_tampered_reference_bytes(self):
        result_id, _ = self.issue_and_close()
        self.change_archive(
            "UPDATE calibrated_references SET bright_bytes = ? WHERE result_id = ? AND reference_id = '3:4'",
            (b'\x0e', result_id),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_tampered_threshold(self):
        result_id, _ = self.issue_and_close()
        self.change_archive(
            "UPDATE calibrated_results SET threshold_num = '1', threshold_den = '1' WHERE result_id = ?",
            (result_id,),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)

    def test_tampered_total(self):
        result_id, _ = self.issue_and_close()
        self.change_archive(
            "UPDATE calibrated_results SET total_num = '1' WHERE result_id = ?",
            (result_id,),
        )
        with ArchiveStore(self.path) as reopened, self.assertRaises(ArchiveIntegrityError):
            read_calibrated(reopened, result_id)


if __name__ == '__main__':
    unittest.main()
