import sqlite3
from fractions import Fraction


_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS stopped_results (
    result_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL,
    high_water TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    capacity TEXT NOT NULL,
    width TEXT NOT NULL,
    height TEXT NOT NULL,
    threshold TEXT NOT NULL,
    frame_count TEXT NOT NULL,
    grayscale_total TEXT NOT NULL,
    over_limit_count TEXT NOT NULL,
    UNIQUE (run_id, high_water),
    UNIQUE (run_id, high_water, result_id)
);
CREATE TABLE IF NOT EXISTS stopped_frames (
    result_id TEXT NOT NULL REFERENCES stopped_results(result_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    source_sequence TEXT NOT NULL,
    original_bytes BLOB NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    PRIMARY KEY (result_id, position)
);
CREATE TABLE IF NOT EXISTS handover_links (
    run_id TEXT PRIMARY KEY NOT NULL,
    high_water TEXT NOT NULL,
    result_id TEXT NOT NULL,
    FOREIGN KEY (run_id, high_water, result_id)
        REFERENCES stopped_results(run_id, high_water, result_id)
);
CREATE TABLE IF NOT EXISTS calibrated_results (
    result_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL,
    high_water TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    capacity TEXT NOT NULL,
    width TEXT NOT NULL,
    height TEXT NOT NULL,
    threshold_num TEXT NOT NULL,
    threshold_den TEXT NOT NULL,
    frame_count TEXT NOT NULL,
    total_num TEXT NOT NULL,
    total_den TEXT NOT NULL,
    over_limit_count TEXT NOT NULL,
    UNIQUE (run_id, high_water),
    UNIQUE (run_id, high_water, result_id)
);
CREATE TABLE IF NOT EXISTS calibrated_frames (
    result_id TEXT NOT NULL REFERENCES calibrated_results(result_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    source_sequence TEXT NOT NULL,
    original_bytes BLOB NOT NULL,
    reference_id TEXT NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    PRIMARY KEY (result_id, position)
);
CREATE TABLE IF NOT EXISTS calibrated_references (
    result_id TEXT NOT NULL REFERENCES calibrated_results(result_id),
    reference_id TEXT NOT NULL,
    dark_sequence TEXT NOT NULL,
    dark_bytes BLOB NOT NULL,
    bright_sequence TEXT NOT NULL,
    bright_bytes BLOB NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    PRIMARY KEY (result_id, reference_id)
);
CREATE TABLE IF NOT EXISTS calibrated_links (
    run_id TEXT PRIMARY KEY NOT NULL,
    high_water TEXT NOT NULL,
    result_id TEXT NOT NULL,
    FOREIGN KEY (run_id, high_water, result_id)
        REFERENCES calibrated_results(run_id, high_water, result_id)
);
COMMIT;
'''


def initialize_archive(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise RuntimeError('archive initialization requires an idle connection')
    connection.execute('PRAGMA foreign_keys = ON')
    try:
        connection.executescript(_SCHEMA)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def encode_exact_integer(value: int) -> str:
    if type(value) is not int:
        raise ValueError('archive integer must be an int')
    return format(value, 'x')


def decode_exact_integer(text: str) -> int:
    if type(text) is not str:
        raise ValueError('archive integer must be stored as text')
    value = int(text, 16)
    if encode_exact_integer(value) != text:
        raise ValueError('archive integer has a noncanonical encoding')
    return value


def encode_exact_rational(value: Fraction) -> tuple[str, str]:
    if type(value) is not Fraction:
        raise ValueError('archive rational must be a Fraction')
    return encode_exact_integer(value.numerator), encode_exact_integer(value.denominator)


def decode_exact_rational(numerator_text: str, denominator_text: str) -> Fraction:
    numerator = decode_exact_integer(numerator_text)
    denominator = decode_exact_integer(denominator_text)
    if denominator < 1:
        raise ValueError('archive rational denominator must be positive')
    value = Fraction(numerator, denominator)
    if value.numerator != numerator or value.denominator != denominator:
        raise ValueError('archive rational has a noncanonical encoding')
    return value


def encode_reference_id(reference_id: tuple[int, int]) -> str:
    if (
        type(reference_id) is not tuple
        or len(reference_id) != 2
        or any(type(part) is not int for part in reference_id)
    ):
        raise ValueError('a reference ID needs exactly two integer sequences')
    dark_sequence, bright_sequence = reference_id
    return f'{encode_exact_integer(dark_sequence)}:{encode_exact_integer(bright_sequence)}'


def decode_reference_id(text: str) -> tuple[int, int]:
    if type(text) is not str:
        raise ValueError('reference ID must be stored as text')
    parts = text.split(':')
    if len(parts) != 2:
        raise ValueError('reference ID must hold exactly two sequences')
    reference_id = (decode_exact_integer(parts[0]), decode_exact_integer(parts[1]))
    if encode_reference_id(reference_id) != text:
        raise ValueError('reference ID has a noncanonical encoding')
    return reference_id
