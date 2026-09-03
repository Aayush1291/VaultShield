"""Atomic `RunEvent.sequence` allocation on MongoDB.

The MongoDB-native replacement for the PostgreSQL mechanism that used to
assign event sequence numbers. Requirement 13.1 ("store each submitted event
and preserve the batch's original chronological order") and Property 22 rest
entirely on what this module guarantees.

### What it replaces, and why the old approach cannot survive

Under PostgreSQL, `RunViewSet.events` did this:

    with transaction.atomic():
        locked_run = Run.objects.select_for_update().get(pk=run.pk)   # row lock
        serializer.save(run=locked_run)                              # reads MAX(sequence)

`_next_sequence_for()` then ran `run.events.aggregate(Max("sequence"))` and
numbered the batch from one past it. That is a read-modify-write, and the row
lock was the only thing making it safe: two batches arriving together would
otherwise both read the same maximum, both number from it, and produce
overlapping `sequence` values. Because retrieval orders by `sequence` alone,
overlapping numbers mean the two batches come back *interleaved* — precisely
the outcome Requirement 13.1 rules out.

`django-mongodb-backend` reports `has_select_for_update = False`, and
`select_for_update()` does not raise on it — it is silently ignored and no lock
is taken. Keeping the old code would therefore have left the read-modify-write
completely unguarded while every existing test continued to pass, since none of
them exercise concurrency. That is the failure mode this module exists to
prevent.

### The mechanism

A single `find_one_and_update` against the Run's own document:

    {"$inc": {"next_event_sequence": count}}

MongoDB guarantees that an update to one document is atomic, and
`find_one_and_update` returns the document as it was *before* or *after* that
update, chosen by the caller. Returning the value from *before* the increment
hands back the first number of a block that no other caller can also receive,
because the increment and the read are one indivisible server-side operation.
Concurrent callers therefore serialise on the document itself, with no
application-level lock, no transaction, and no retry loop.

Allocating a whole block per batch — `$inc` by `count`, not `count` separate
increments — is what keeps a batch's numbers contiguous and in submitted order.

### The four guarantees, and where each comes from

- **No duplicate sequence numbers for a Run.** Two sources, deliberately
  independent. `$inc` is atomic, so no two allocations overlap; and
  `RunEvent.Meta.constraints` carries a `UniqueConstraint(run, sequence)`, so a
  duplicate would be rejected by the database even if this function were
  wrong. See that constraint's comment for why the redundancy is the point.
- **Deterministic ordering.** Numbers within a block are consecutive and
  assigned by list position, so stored order equals submitted order. Blocks are
  handed out in the order `$inc` executes, so a batch's numbers are entirely
  above every batch allocated before it and entirely below every batch after.
- **Safe concurrent ingestion.** The atomicity is MongoDB's own, per document.
  `vaultshield_backend/test_mongodb.py::TestConcurrentSequenceAllocation` drives this from real
  threads against a real Run.
- **Retry safety.** Every call allocates a *fresh* block, so a retried batch
  can never reuse the numbers of the attempt it is retrying. That leaves a gap
  in the sequence where the abandoned attempt's block was, which is harmless:
  Requirement 13.1 is about relative order, and `RunEvent.Meta.ordering`
  (`["sequence"]`) is unaffected by gaps. The alternative — reusing numbers —
  is what would risk a duplicate.

### Why this bypasses the ORM

`find_one_and_update` has no Django queryset equivalent: `QuerySet.update()`
cannot return the pre-update value, so "increment and tell me what it was"
would be two statements and the atomicity would be gone. The raw PyMongo
collection is reached through `connection.get_collection()`, which
`django-mongodb-backend` provides for exactly this.

Bypassing the ORM has one useful side effect: `Run.updated_at` carries
`auto_now`, and a raw update does not trigger it. Event ingest therefore leaves
`updated_at` alone — matching the PostgreSQL behaviour, where ingest wrote
`RunEvent` rows and never saved the `Run`. That column is the input to
`flag_stuck_approvals`, so bumping it here would have changed when a Run counts
as stuck.
"""

from __future__ import annotations

from django.db import connection

from runs.models import Run, RunEvent

# The field on `Run` that holds the allocation cursor. Named here rather than
# written as a literal at the call site so the raw-PyMongo update and the model
# field cannot drift apart.
SEQUENCE_COUNTER_FIELD = "next_event_sequence"


class SequenceAllocationError(RuntimeError):
    """Raised when a sequence block could not be allocated for a Run.

    Means the Run's document was not found — it was deleted between the view
    resolving it and this allocation. Distinct from a MongoDB connection
    failure, which surfaces as PyMongo's own exception and is not caught here:
    there is nothing this module can add to it.
    """


def allocate_event_sequences(run: Run, count: int) -> range:
    """Reserve `count` consecutive `RunEvent.sequence` values for `run`.

    Args:
        run: the Run the batch belongs to. Only its primary key is used.
        count: how many events the batch holds. Zero is valid and reserves
            nothing, so an empty batch costs no write — `RunEventBatchSerializer`
            accepts an empty list (a Run that produced no events is a
            legitimate outcome) and this is the matching no-op.

    Returns:
        A `range` of exactly `count` sequence numbers, ascending and
        consecutive. Callers assign them by list position, which is what makes
        stored order equal submitted order.

    Raises:
        ValueError: if `count` is negative.
        SequenceAllocationError: if `run`'s document no longer exists.

    The returned block is reserved the moment this returns, whether or not the
    caller goes on to insert anything. Nothing reclaims an unused block — see
    the module docstring on why a gap is preferable to a reused number.
    """
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")

    if count == 0:
        # No write at all: an empty batch must not advance the cursor, or a
        # retried empty upload would leave a gap for no reason.
        return range(0, 0)

    collection = connection.get_collection(Run._meta.db_table)

    # `return_document=BEFORE` is the load-bearing detail: the value *before*
    # the increment is the first number of this block, and reading it in the
    # same operation that increments is what makes the allocation atomic.
    # Asking for the post-increment value and subtracting `count` would be
    # equivalent, but this way the arithmetic is not part of the guarantee.
    document = collection.find_one_and_update(
        {"_id": run.pk},
        {"$inc": {SEQUENCE_COUNTER_FIELD: count}},
        projection={SEQUENCE_COUNTER_FIELD: 1},
        return_document=False,  # pymongo.ReturnDocument.BEFORE
    )

    if document is None:
        raise SequenceAllocationError(
            f"Cannot allocate event sequences: Run {run.pk} no longer exists."
        )

    # A Run created before this field existed, or one written by something that
    # did not set it, has no such key. Treating a missing key as 0 is correct
    # and is not a race: `$inc` on a missing field sets it to the increment, so
    # the document now reads `count` and the block starting at 0 is this
    # caller's alone.
    first = document.get(SEQUENCE_COUNTER_FIELD, 0)

    return range(first, first + count)


def highest_stored_sequence(run: Run) -> int | None:
    """The largest `sequence` actually stored for `run`, or `None` if it has no
    events.

    Not used by the ingest path — allocation reads the counter, never the
    stored rows, which is the whole point. This exists for tests and for
    diagnosing a Run whose counter and rows have diverged (which a gap from an
    abandoned batch makes normal, so a mismatch is not by itself a fault).
    """
    return (
        RunEvent.objects.filter(run=run)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
