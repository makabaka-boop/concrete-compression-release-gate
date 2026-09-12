"""SQLite-backed idempotency ledger for POST /evaluate.

When a request carries an ``evaluation_id``, the endpoint computes a
fingerprint over the normalized business input (design strength, the three
specimens' *effective* areas and loads, and the calibration factor —
including whether the factor was explicitly provided), then persists
``evaluation_id -> (fingerprint, response body, request snapshot)`` in a
local SQLite database. A specimen whose loaded face was entered as
``width_mm`` x ``depth_mm`` is converted to its effective area before
fingerprinting, so an area entry and a numerically equivalent dimensions
entry produce the same fingerprint and replay instead of a false conflict.

The request snapshot stored on the first write is the normalized input
itself (design strength, effective area + load per specimen, calibration
factor and whether it was explicitly submitted), serialized with the same
exact-Decimal JSON rendering as the response body. It backs
``GET /evaluations/{evaluation_id}``: reviewers can retrieve the first
verdict together with the input that produced it, without trusting any
caller-side cache. Replays and conflicts never rewrite the snapshot or
the record timestamp — the first stored verdict always stands.

Replay semantics:

* same id + same fingerprint  -> return the stored body with replayed=true
* same id + other fingerprint -> conflict; the stored record is never
  overwritten
* new id                      -> compute, persist, return with replayed=false

Fingerprint normalization uses Decimal semantics: numerically equal values
produce identical tokens regardless of their textual representation, so
``30.0``, ``30.00`` and ``3E+1`` are the same request, while ``1``
(implicit factor) and ``1.0`` (explicitly sent) are not — the explicit
factor changes the response body via ``applied_calibration_factor``.

Schema evolution is a compatible migration applied on every connect (and
eagerly at application startup via :func:`init_db`): ledger files written
before snapshots existed gain the nullable ``request_snapshot`` column in
place, and their rows stay queryable — the snapshot simply reads as
unavailable (``snapshot_available=false``) because the original input of
those records cannot be reconstructed.

The database path comes from the ``EVALUATION_DB_PATH`` environment
variable (default ``./data/evaluations.db``); point it at a temporary file
to isolate a test run from any real ledger. A process-wide re-entrant
lock plus WAL mode keeps concurrent requests — including parallel first
submissions of the same id — from double-computing or corrupting the
ledger.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple, Sequence

DEFAULT_DB_PATH = "./data/evaluations.db"
DB_PATH_ENV = "EVALUATION_DB_PATH"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id    TEXT PRIMARY KEY,
    fingerprint      TEXT NOT NULL,
    response_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    request_snapshot TEXT
);
"""

# Process-wide lock shared by every connection to the configured database.
# SQLite serializes writers anyway; the lock exists so that the
# check-then-insert sequence of two racing first submissions cannot both
# observe "no record" and double-compute.
_LOCK = threading.RLock()


class EvaluationRecord(NamedTuple):
    """One persisted verdict.

    ``request_snapshot`` is the normalized first input as exact-Decimal
    JSON, or None for rows written before snapshots existed (their input
    cannot be reconstructed). ``created_at`` is the first-write timestamp;
    neither field is ever rewritten by replays or conflicts.
    """

    fingerprint: str
    response_json: str
    request_snapshot: str | None
    created_at: str


def _db_path() -> str:
    return os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the ledger table and migrate pre-snapshot databases in place.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op for existing ledger files, so
    the nullable ``request_snapshot`` column is added with a plain
    ``ALTER TABLE`` when missing. Existing rows keep NULL there, which the
    query endpoint reports as ``snapshot_available=false``.
    """
    connection.executescript(_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(evaluations)")
    }
    if "request_snapshot" not in columns:
        connection.execute(
            "ALTER TABLE evaluations ADD COLUMN request_snapshot TEXT"
        )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    _ensure_schema(connection)
    return connection


def init_db() -> None:
    """Open the ledger once so schema creation and migrations run at startup."""
    with _LOCK, _connect():
        pass


def _decimal_token(value: Decimal) -> str:
    """Canonical token for a Decimal: equal values -> equal tokens.

    Decimal equality is representation-independent (30.0 == 30.00 == 3E+1),
    so the token strips trailing fractional zeros at the digit-tuple level.
    Unlike ``Decimal.normalize()`` this never applies context precision, so
    high-precision inputs are not rounded into a collision, and unlike
    ``hash()`` it is stable across processes and Python versions — the
    fingerprint is persisted and must survive restarts.
    """
    if not value.is_finite():
        return f"D:{value}"
    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return "D:0"
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return f"D:{sign}:{''.join(str(d) for d in digits)}:{exponent}"


def build_fingerprint(
    design_strength_mpa: Decimal,
    specimens: Sequence[tuple[Decimal, Decimal]],
    calibration_factor: Decimal,
    calibration_explicit: bool,
) -> str:
    """Fingerprint the normalized business input of one evaluation request.

    ``specimens`` is an ordered sequence of ``(area_mm2, load_kn)`` pairs;
    specimen order is part of the fingerprint because it is part of the
    response. ``calibration_explicit`` records whether the caller sent the
    factor, since an explicit neutral factor still echoes
    ``applied_calibration_factor`` in the response.
    """
    parts = [
        "v1",
        f"design={_decimal_token(design_strength_mpa)}",
        f"calibration={_decimal_token(calibration_factor)}",
        f"calibration_explicit={int(calibration_explicit)}",
    ]
    for area_mm2, load_kn in specimens:
        parts.append(
            f"specimen=({_decimal_token(area_mm2)},{_decimal_token(load_kn)})"
        )
    return "|".join(parts)


def lookup(evaluation_id: str) -> EvaluationRecord | None:
    """Return the stored record for ``evaluation_id``, or None."""
    with _LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT fingerprint, response_json, request_snapshot, created_at"
            " FROM evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
    if row is None:
        return None
    return EvaluationRecord(*row)


def store_if_absent(
    evaluation_id: str, fingerprint: str, response_json: str, request_snapshot: str
) -> tuple[EvaluationRecord, bool]:
    """Persist a new verdict, returning the record that won the race.

    The boolean is True when this call inserted the record. If
    ``evaluation_id`` was already stored (a racing first submission won),
    the existing record is returned unchanged with False — the first
    stored verdict always stands, and neither its timestamp nor its
    snapshot is rewritten.
    """
    with _LOCK, _connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO evaluations"
            " (evaluation_id, fingerprint, response_json, created_at,"
            "  request_snapshot)"
            " VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
            (evaluation_id, fingerprint, response_json, request_snapshot),
        )
        inserted = cursor.rowcount == 1
        row = connection.execute(
            "SELECT fingerprint, response_json, request_snapshot, created_at"
            " FROM evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
    return EvaluationRecord(*row), inserted
