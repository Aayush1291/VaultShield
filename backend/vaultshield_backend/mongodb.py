"""MongoDB storage characteristics that application code has to account for.

Currently one: datetime precision.

### MongoDB stores milliseconds, not microseconds

BSON's date type is a 64-bit count of milliseconds since the Unix epoch, so a
`DateTimeField` round-trip through MongoDB truncates the microsecond component
to the nearest millisecond below. `django-mongodb-backend` documents this
("Microsecond granularity for DateTimeField values is not supported") and it is
a property of the wire format, not something a backend option can turn off.

Python's `datetime.now()` returns microseconds, so any value generated in
Python and then compared against the same value read back from MongoDB is
liable to differ:

    >>> decided_at = timezone.now()          # ...470555
    >>> Run.objects.filter(pk=pk).update(updated_at=decided_at)
    >>> Run.objects.get(pk=pk).updated_at    # ...470000

### Why this matters beyond test assertions

Under PostgreSQL, `apply_decision` wrote `updated_at=decided_at` and then set
`run.updated_at = decided_at` in memory so the caller could serialise the Run
without a re-read — the two were identical because PostgreSQL's `timestamptz`
keeps microseconds. On MongoDB they are not identical, so the approve/reject
response would report an `updated_at` ending `...470555` while every subsequent
`GET /api/runs/:id/` reported `...470000` for the same unchanged row. A client
diffing the two would see a field that appears to have changed on its own.

Truncating *before* the write closes that gap: the value held in memory is then
exactly the value stored, and the API answers consistently whether or not the
row has been re-read.

The truncation loses at most 999 microseconds. Nothing in VaultShield measures
anything at that resolution — the two scheduled tasks compare ages in minutes
and hours, and `RunEvent` ordering is carried by `sequence` rather than by
`timestamp` precisely so that events sharing a timestamp still read back in
order.
"""

from __future__ import annotations

import datetime as _datetime

# BSON dates are integer milliseconds, so this is the smallest representable
# step. Named rather than inlined as `1000` so the call site reads as a unit.
MONGODB_DATETIME_RESOLUTION_US = 1000


def to_mongo_precision(value: _datetime.datetime) -> _datetime.datetime:
    """Round `value` down to the precision MongoDB will actually store.

    Truncates toward the past (not to nearest), matching what a BSON
    round-trip does, so a truncated value never sorts *after* the value it
    was derived from. That direction matters for the scheduled tasks: their
    cutoffs are `__lt` comparisons, and rounding a stored timestamp forward
    could make a record look newer than the cutoff it was selected against.

    Returns the value unchanged when it is already millisecond-aligned, and
    preserves `tzinfo` (VaultShield runs `USE_TZ = True`, so every stored
    datetime is aware).
    """
    return value.replace(
        microsecond=(value.microsecond // MONGODB_DATETIME_RESOLUTION_US)
        * MONGODB_DATETIME_RESOLUTION_US
    )


# --- transient transaction errors ------------------------------------------
#
# MongoDB aborts a transaction that loses a write race on a document, tagging
# the failure `TransientTransactionError` and expecting the *client* to retry
# the whole transaction. This is documented, expected behaviour rather than a
# fault, and it has no PostgreSQL analogue in VaultShield: PostgreSQL's default
# READ COMMITTED isolation let the conditional `UPDATE` block briefly and then
# report zero rows matched, so a losing decision surfaced as a clean refusal
# with no retry needed.
#
# Measured on the approve/reject path with six simultaneous decisions on one
# Run, before this was added: exactly one caller won every time (the guard
# held), but 54 of 72 losers raised `DatabaseError` instead of the refusal
# Requirement 21.3 specifies — i.e. HTTP 500 rather than 409. The invariant was
# never in danger; the *reporting* was wrong.
#
# Only the approve/reject path needs this. Event ingest was measured too (36
# concurrent batches, zero conflicts) and does not contend: its sequence
# allocation is a raw `find_one_and_update` that commits outside the
# transaction, and the `RunEvent` documents each batch inserts are its own.

import random
import time

from pymongo.errors import PyMongoError

# The label MongoDB attaches to a failure the client is expected to retry.
TRANSIENT_TRANSACTION_ERROR_LABEL = "TransientTransactionError"

# Bounded so a genuinely contended document cannot spin forever. Five attempts
# is ample: each retry re-reads state, and the operations here converge — once
# a winner has committed, a retrying caller sees the committed decision and
# takes its refusal path instead of racing again.
DEFAULT_TRANSACTION_ATTEMPTS = 5

# Base for the exponential backoff between attempts, in seconds. Deliberately
# short: a write conflict is resolved in microseconds server-side, so this only
# has to break the lockstep that made the callers collide.
_BACKOFF_BASE_S = 0.01


def is_transient_transaction_error(exception: BaseException) -> bool:
    """True when `exception` is a MongoDB transaction abort worth retrying.

    `django-mongodb-backend` wraps PyMongo failures in Django's `DatabaseError`
    with the original attached as `__cause__`, so the label has to be read
    through the chain rather than off the exception it is given.
    """
    for candidate in (exception, getattr(exception, "__cause__", None)):
        if isinstance(candidate, PyMongoError) and candidate.has_error_label(
            TRANSIENT_TRANSACTION_ERROR_LABEL
        ):
            return True
    return False


def retry_on_transient_transaction_error(
    operation, *, attempts: int = DEFAULT_TRANSACTION_ATTEMPTS
):
    """Call `operation`, retrying it while MongoDB reports a transient abort.

    Args:
        operation: a zero-argument callable that opens its own transaction.
            It is re-invoked from the top on each retry, so it must re-read
            whatever it decides on — which is what makes a retry converge
            rather than repeat a stale decision.
        attempts: total tries, not retries. Must be at least 1.

    Returns:
        Whatever `operation` returns.

    Raises:
        Anything `operation` raises that is not a transient transaction error,
        immediately and unwrapped — an application-level refusal such as
        `StateTransitionError` is a real answer, not a race, and retrying it
        would turn a 409 into a delayed 409. The final transient error is
        re-raised unchanged once `attempts` is exhausted, so an unresolvable
        conflict still fails rather than being swallowed.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt == attempts or not is_transient_transaction_error(exc):
                raise
            # Jittered exponential backoff. The jitter matters more than the
            # growth here: the callers that collided arrived together, so
            # retrying them together would simply collide again.
            time.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)) * (0.5 + random.random()))
