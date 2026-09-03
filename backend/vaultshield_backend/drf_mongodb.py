"""Django REST Framework support for MongoDB `ObjectId` primary keys.

### The problem this solves

`ModelSerializer` picks a serializer field per model field by walking the model
field's MRO against `ModelSerializer.serializer_field_mapping`.
`ObjectIdAutoField` is not in that mapping, and it inherits from Django's
`AutoField`, so DRF resolves it to `serializers.IntegerField` and the first
response blows up inside DRF rather than anywhere in VaultShield:

    TypeError: int() argument must be a string, a bytes-like object or a real
    number, not 'ObjectId'

Every `/api/` endpoint is affected, because `id` is listed in the `fields` of
every serializer in the project. This is the concrete form of the "Django Rest
Framework: Partially Supported" note in MongoDB's own compatibility matrix.

### The fix, and why it is a global registration

Two fields are needed — one for a model's own `ObjectId` primary key, one for a
`ForeignKey` pointing at another model whose primary key is an `ObjectId` — and
both are installed into `ModelSerializer`'s class-level mappings by
`install()`.

Registering globally rather than declaring `id = ObjectIdField()` on each of
the fourteen serializers is deliberate. A per-serializer declaration is a line
someone has to remember on every serializer ever added, and forgetting it
produces a 500 at runtime rather than an error at import. The mapping makes
correct behaviour the default for serializers that do not know MongoDB is
involved, which is all of them — `projects/serializers.py` and
`runs/serializers.py` are unchanged by this module.

`install()` is called from `VaultShieldBackendConfig.ready()`
(`vaultshield_backend/apps.py`), which runs during `django.setup()` and
therefore before any serializer is instantiated. It is idempotent.

### What the API contract becomes

`id` and every foreign key render as the ObjectId's 24-character hexadecimal
string, and are accepted in the same form. Under PostgreSQL they were JSON
integers. That is the one unavoidable, client-visible change from the MongoDB
migration; it is pinned by
`vaultshield_backend/test_mongodb.py::TestRestApiAgainstMongoDB`.

The frontend needs no change for it: `project.id` is used to build a query
string and as a React key, and `RunHistoryView` keys on `run_id` in preference
to `id`. Both work with an opaque string.
"""

from __future__ import annotations

from bson import ObjectId
from bson.errors import InvalidId
from django_mongodb_backend.fields import ObjectIdAutoField, ObjectIdField as ModelObjectIdField
from rest_framework import serializers


class ObjectIdField(serializers.Field):
    """Serializes a BSON `ObjectId` as its hexadecimal string.

    A plain `Field` rather than a `CharField` subclass: `CharField` would coerce
    with `str()` on the way in and hand the database a 24-character string where
    it expects an `ObjectId`, which fails as a type error deep inside the query
    compiler instead of as a validation error on the field.
    """

    default_error_messages = {
        "invalid": "Not a valid ObjectId: expected 24 hexadecimal characters.",
    }

    def to_representation(self, value) -> str:
        # Already a string when the value came from a `values()` query rather
        # than a model instance; `str()` covers both without a branch.
        return str(value)

    def to_internal_value(self, data) -> ObjectId:
        try:
            return ObjectId(str(data))
        except (InvalidId, TypeError, ValueError):
            self.fail("invalid")


class ObjectIdRelatedField(serializers.PrimaryKeyRelatedField):
    """A `PrimaryKeyRelatedField` whose primary keys are `ObjectId`s.

    `PrimaryKeyRelatedField.to_representation` returns the target's raw primary
    key, which for MongoDB is an `ObjectId` — not JSON-serialisable, so DRF's
    renderer fails on it. Stringifying here keeps `Run.project` rendering as the
    same hex string that `id` does, so a client can round-trip a value it read
    from one response into another request.
    """

    def to_representation(self, value) -> str:
        return str(value.pk)

    def to_internal_value(self, data):
        # Reject a malformed id as a field validation error (400) rather than
        # letting an invalid value reach the queryset lookup, where it would
        # surface as a database-level type error (500).
        try:
            ObjectId(str(data))
        except (InvalidId, TypeError, ValueError):
            self.fail("incorrect_type", data_type=type(data).__name__)
        return super().to_internal_value(data)


def install() -> None:
    """Teach `ModelSerializer` about MongoDB's primary keys.

    Idempotent, so calling it more than once (a reloader, a test that reloads
    app configs) is harmless.
    """
    # `ObjectIdAutoField` is the primary key on every VaultShield model;
    # `ObjectIdField` is the backend's explicit non-auto variant, mapped too so
    # that a model declaring one directly behaves the same way.
    serializers.ModelSerializer.serializer_field_mapping[ObjectIdAutoField] = ObjectIdField
    serializers.ModelSerializer.serializer_field_mapping[ModelObjectIdField] = ObjectIdField

    # Used for every `ForeignKey`/`OneToOneField` DRF builds automatically,
    # which is how `RunSerializer.project` is produced — it is never declared.
    serializers.ModelSerializer.serializer_related_field = ObjectIdRelatedField
