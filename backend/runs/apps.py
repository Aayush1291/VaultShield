from django.apps import AppConfig


class RunsConfig(AppConfig):
    # MongoDB uses the BSON ObjectId in each document's `_id` as the primary
    # key; `BigAutoField` (a sequential integer) has no equivalent and
    # `django-mongodb-backend` rejects it with `mongodb.E001`. Restated per app
    # because an AppConfig's own `default_auto_field` overrides
    # `settings.DEFAULT_AUTO_FIELD`, which is the same reason the contrib apps
    # need `vaultshield_backend/apps.py`.
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "runs"
