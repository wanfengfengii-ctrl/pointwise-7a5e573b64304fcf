import json
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from station.archive.issue import issue_calibrated, issue_stopped
from station.archive.readback import read_calibrated
from station.archive.store import ArchiveStore
from station.capture.source import CameraFrameEvent, SimulatedCamera
from station.handover.board import handover_board
from station.handover.receive import receive_stopped
from station.runs.session import RecordingSession


def require_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f'{label}: expected {expected!r}, received {actual!r}')


def run_shift_demo():
    sample_path = Path(__file__).resolve().parent / 'examples' / 'shift.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    camera_id = sample['camera_id']
    config = sample['config']
    expected = sample['expected']
    expected_capture = {'writes': expected['writes'], 'live': expected['live']}
    expected_review = {
        'config': config,
        'values': expected['live'],
        'handover': expected['handover'],
        'frames': expected['archived_frames'],
    }

    with TemporaryDirectory() as folder:
        path = Path(folder) / 'shift.sqlite3'
        with RecordingSession([camera_id]) as session, ArchiveStore(path) as archive:
            run = session.open_run(camera_id, **config)
            with SimulatedCamera(session.capture, run.run_id) as camera:
                writes = [
                    asdict(camera.step(CameraFrameEvent(frame['sequence'], bytes(frame['pixels']))))
                    for frame in sample['frames']
                ]
                capture = {'writes': writes, 'live': asdict(session.live_quality(run.run_id))}
                require_equal(capture, expected_capture, 'camera rotation and live quality')
                cutoff = session.pause_run(run.run_id)
                pending, = handover_board(archive, run.run_id, session)
                require_equal((pending.status.value, pending.result_id), ('pending', None), 'before issue')
                result_id = issue_stopped(session, archive, run.run_id, cutoff)
                require_equal(issue_stopped(session, archive, run.run_id, cutoff), result_id, 'issue retry')

        with ArchiveStore(path) as reopened:
            ticket, = handover_board(reopened, run.run_id)
            require_equal(ticket.result_id, result_id, 'offline handover result')
            review = receive_stopped(reopened, run.run_id, ticket.result_id)
            require_equal(review.header.cutoff, cutoff, 'offline cutoff')
            offline = {
                'config': asdict(review.header.config),
                'values': asdict(review.header.values),
                'handover': ticket.status.value,
                'frames': [{'sequence': f.sequence, 'pixels': list(f.data)} for f in review.frames],
            }
            require_equal(offline, expected_review, 'offline review')

    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'v2.sparse_sequences',
        'input': {'camera_id': camera_id, 'config': config, 'frames': sample['frames']},
        'expected': expected_capture,
        'actual': capture,
        'downstream_expected': expected_review,
        'downstream_actual': offline,
        'code_paths': [
            'station/capture/ring.py', 'station/quality/window.py', 'station/archive/issue.py',
            'station/archive/store.py', 'station/handover/receive.py', 'station/archive/verify.py',
        ],
        'oracle': 'Three successful writes in two slots visit 0, 1, 0. The remaining byte sums are 5 and 7. Only 7 exceeds 5.',
    }, sort_keys=True))


def replay_events(session, source, run_id, events):
    writes = []
    for event in events:
        data = bytes(event['pixels'])
        if event['op'] == 'frame':
            action = lambda: source.step(CameraFrameEvent(event['sequence'], data))
        else:
            action = lambda: session.submit_reference(run_id, event['kind'], event['sequence'], data)
        if event.get('reject'):
            try:
                action()
            except ValueError:
                continue
            raise AssertionError(f'event should have been rejected: {event!r}')
        if event['op'] == 'frame':
            writes.append(asdict(action()))
        else:
            action()
    return writes


def values_projection(values):
    count, total, over_limit = values
    return [count, [total.numerator, total.denominator], over_limit]


def review_projection(review):
    return {
        'frames': [[frame['sequence'], *frame['reference_id']] for frame in review['frames']],
        'values': values_projection(review['values']),
        'references': {
            f'{dark}:{bright}': {
                'dark': [reference['dark'][0], list(reference['dark'][1])],
                'bright': [reference['bright'][0], list(reference['bright'][1])],
            }
            for (dark, bright), reference in review['references'].items()
        },
    }


def run_calibration_demo():
    sample_path = Path(__file__).resolve().parent / 'examples' / 'calibration.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    camera_id = sample['camera_id']
    config = sample['config']
    threshold = Fraction(config['threshold'][0], config['threshold'][1])
    # Hand-computed from the sample events: each checkpoint's paused window keeps
    # only the groups its frames actually use; frames 20 and 30 measure against
    # different groups whose original bytes are identical.
    expected_references = [
        {'1:2': {'dark': [1, [0]], 'bright': [2, [10]]},
         '3:4': {'dark': [3, [5]], 'bright': [4, [15]]}},
        {'3:4': {'dark': [3, [5]], 'bright': [4, [15]]},
         '7:8': {'dark': [7, [5]], 'bright': [8, [15]]}},
    ]

    expected_reviews = []
    actual_reviews = []
    with TemporaryDirectory() as folder:
        path = Path(folder) / 'calibration.sqlite3'
        issued = []
        with RecordingSession([camera_id]) as session, ArchiveStore(path) as archive:
            for checkpoint in sample['checkpoints']:
                run = session.open_calibrated_run(
                    camera_id, config['capacity'], config['width'], config['height'], threshold)
                with SimulatedCamera(session.capture, run.run_id) as camera:
                    writes = replay_events(session, camera, run.run_id,
                                           sample['events'][:checkpoint['after_events']])
                    live = session.live_calibrated(run.run_id)
                cutoff = session.pause_run(run.run_id)
                result_id = issue_calibrated(session, archive, run.run_id, cutoff)
                require_equal(issue_calibrated(session, archive, run.run_id, cutoff), result_id,
                              'calibrated issue retry')
                issued.append((run.run_id, checkpoint, writes, live, cutoff, result_id))

        with ArchiveStore(path) as reopened:
            for index, (run_id, checkpoint, writes, live, cutoff, result_id) in enumerate(issued):
                ticket, = handover_board(reopened, run_id)
                require_equal(ticket.result_id, result_id, 'calibrated handover result')
                review = read_calibrated(reopened, ticket.result_id)
                require_equal(review['cutoff'], cutoff, 'calibrated offline cutoff')
                require_equal(review['threshold'], threshold, 'calibrated offline threshold')
                require_equal(review['values'], live, 'live and archived calibrated values')
                expected = {
                    'frames': checkpoint['frames'],
                    'values': checkpoint['values'],
                    'references': expected_references[index],
                }
                actual = review_projection(review)
                require_equal(actual, expected, f'checkpoint {index} offline review')
                expected_reviews.append({'writes': writes, **expected})
                actual_reviews.append({'writes': writes, **actual})

    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'v3.cross_group_calibration',
        'input': {'camera_id': camera_id, 'config': config, 'events': sample['events']},
        'expected': expected_reviews,
        'actual': actual_reviews,
        'code_paths': [
            'station/capture/calibration.py', 'station/quality/calibrated.py',
            'station/archive/issue.py', 'station/archive/store.py',
            'station/archive/readback.py', 'station/archive/verify.py',
        ],
        'oracle': 'Rejected frames and references change nothing. Frames 10, 20, 30 bind groups '
                  '(1,2), (3,4), (7,8) as accepted; capacity 2 overwrites frame 10. Each checkpoint '
                  'archives only the groups its window uses; (3,4) and (7,8) share bytes but keep '
                  'their own provenance. Pixel (6-5)/(15-5) sums to 7/10 then 1/5.',
    }, sort_keys=True))


def main():
    run_shift_demo()
    run_calibration_demo()


if __name__ == '__main__':
    main()
