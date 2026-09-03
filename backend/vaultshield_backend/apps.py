"""AppConfig overrides that force MongoDB-compatible primary keys.

### Why this module has to exist

`DEFAULT_AUTO_FIELD` in `settings.py` covers VaultShield's own apps, but it is
only a *default*: Django ignores it for any app whose own `AppConfig` sets
`default_auto_field` explicitly, and every `django.contrib` app does. Those
apps therefore keep `AutoField`, which `django-mongodb-backend` rejects
outright — `manage.py migrate` fails the system check with, once per model:

    auth.User.id: (mongodb.E001) MongoDB does not support AutoField.
        HINT: Use django_mongodb_backend.fields.ObjectIdAutoField instead.

Subclassing each contrib `AppConfig` and restating `default_auto_field` is the
documented fix, and it is what the upstream `django-mongodb-project` template
does. `settings.INSTALLED_APPS` names these subclasses instead of the app
modules, which is how Django picks them up.

### `rest_framework.authtoken` is deliberately NOT subclassed

It needs no override: `Token`'s primary key is `key = CharField(max_length=40,
primary_key=True)`, an explicit non-auto field, so `mongodb.E001` never fires
for it.

Subclassing it is also actively harmful. `authtoken` ships a `TokenProxy`
proxy model whose `Meta.proxy` resolution is tied to the app config Django
loaded it under; registering the app by a subclassed config makes `TokenProxy`
resolve as abstract and `authtoken/admin.py` dies at import:

    ImproperlyConfigured: The model TokenProxy is abstract, so it cannot be
    registered with admin.

So `settings.INSTALLED_APPS` keeps the plain `"rest_framework.authtoken"`
string. Verified working: the token collection migrates and token auth passes
its tests.

### `sessions`, `messages`, `staticfiles`

Not listed here either. `messages` and `staticfiles` define no models at all,
and `django.contrib.sessions`' `Session` uses `session_key = CharField(
primary_key=True)` — again an explicit primary key, so no override is needed.
"""

from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig
from django.contrib.auth.apps import AuthConfig
from django.contrib.contenttypes.apps import ContentTypesConfig

# The one field type MongoDB can use as a primary key: it maps to the BSON
# ObjectId that every MongoDB document carries in `_id`.
OBJECT_ID_AUTO_FIELD = "django_mongodb_backend.fields.ObjectIdAutoField"


class MongoAdminConfig(AdminConfig):
    """`django.contrib.admin` with an ObjectId primary key on `LogEntry`."""

    default_auto_field = OBJECT_ID_AUTO_FIELD


class MongoAuthConfig(AuthConfig):
    """`django.contrib.auth` with ObjectId primary keys.

    Covers `User`, `Group`, and `Permission` — all three are `AutoField` by
    default and all three are load-bearing here: `User` is VaultShield's
    Reviewer identity (design.md keeps Django's built-in model rather than a
    custom one), and `Permission` is what `create_permissions` populates on
    `post_migrate`.
    """

    default_auto_field = OBJECT_ID_AUTO_FIELD


class MongoContentTypesConfig(ContentTypesConfig):
    """`django.contrib.contenttypes` with an ObjectId primary key.

    Needed even though VaultShield never queries `ContentType` itself:
    `Permission.content_type` is a `ForeignKey` to it, and the admin's
    `LogEntry` carries another, so the whole `auth`/`admin` schema depends on
    this model migrating.
    """

    default_auto_field = OBJECT_ID_AUTO_FIELD


class VaultShieldBackendConfig(AppConfig):
    """The project package's own AppConfig, for MongoDB-wide wiring.

    Holds no models — `vaultshield_backend` is the project package, not a data
    app. It exists so that `drf_mongodb.install()` has somewhere correct to run:
    `AppConfig.ready()` fires during `django.setup()`, after the app registry is
    populated and before any serializer is instantiated or any request served,
    which is exactly the window that patch needs.

    The alternative — a module-level side effect on import — would depend on
    something importing the module early enough, and would be silently skipped
    if nothing did.
    """

    name = "vaultshield_backend"
    label = "vaultshield_backend"
    default_auto_field = OBJECT_ID_AUTO_FIELD

    def ready(self) -> None:
        # Registers `ObjectIdField` / `ObjectIdRelatedField` on
        # `ModelSerializer`. Without it, DRF resolves `ObjectIdAutoField` to
        # `IntegerField` and every `/api/` response raises `TypeError: int()
        # argument must be ... not 'ObjectId'`. See `drf_mongodb.py`.
        from vaultshield_backend import drf_mongodb

        drf_mongodb.install()
