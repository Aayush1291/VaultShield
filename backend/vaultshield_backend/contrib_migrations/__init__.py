"""Project-local migration histories for the `django.contrib` apps.

`settings.MIGRATION_MODULES` redirects `admin`, `auth`, and `contenttypes`
here. Two reasons this is necessary rather than tidy:

1. Those apps' shipped migrations declare `id` as `AutoField`, which MongoDB
   cannot use as a primary key. The models are corrected at runtime by the
   AppConfig subclasses in `vaultshield_backend/apps.py`, but a migration
   history still describing an integer key would keep `makemigrations`
   generating `AlterField` operations forever.

2. Without this redirect, `makemigrations` writes those operations *into the
   installed Django package* under `.venv/lib/.../django/contrib/`. That is
   invisible to git, lost on every fresh `pip install`, and different on every
   developer's machine.

The histories here are fresh `0001_initial` migrations with `ObjectIdAutoField`
primary keys, not the stock history plus a patch. VaultShield's MongoDB
database is greenfield, so there is nothing to migrate *from* and no reason to
replay a decade of Django's own schema evolution to arrive at the same state.

`sessions` and `rest_framework.authtoken` are deliberately absent: both already
use an explicit non-auto primary key (`Session.session_key`, `Token.key`), so
their shipped migrations apply to MongoDB unchanged.
"""
