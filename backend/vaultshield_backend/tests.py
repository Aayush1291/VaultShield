"""Tests for the project package's own wiring.

Task 23.1 — the Celery/Redis wiring smoke checks design.md's Testing
Strategy lists for Requirement 18.2. There is nothing generative here: the
whole subject is a fixed set of module-level statements, so what is worth
asserting is that each one has the effect the async scan path depends on.

None of these need a database or a running Redis. Broker configuration is
read, never dialled: `app.conf` is a settings mapping, and
`import_default_modules()` imports task modules the same way a starting
worker does, before any connection is attempted. That is deliberate — a
wiring check that needed the stack up could not run on a fresh clone, which
is the same reason `settings_test.py` exists.

Task 23.2's enqueue call from the diff-ingest view, and Task 23.3's test
that diff ingest performs it, are the other half of Requirement 18 and are
not covered here.

Task 33.4 adds the Beat schedule checks from `TestBeatSchedule` onward — the
"WHILE Celery Beat is running on a configured schedule" clause both halves of
Requirement 25 open with. What the two tasks *do* once they run is covered by
`tasks/tests.py` against a database; what is left here is whether Beat and
the worker can find them at all, which is a question about configuration and
needs no rows.
"""

import inspect
from datetime import timedelta

import pytest
import vaultshield_backend
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from security_scan.tasks import run_security_scan
from tasks.scheduled import (
    DEFAULT_APPROVAL_MAX_AGE_HOURS,
    DEFAULT_SANDBOX_TTL_MINUTES,
    cleanup_orphaned_sandboxes,
    flag_stuck_approvals,
)
from vaultshield_backend.celery import app
from vaultshield_backend.settings import _env_int, _redis_url


class TestCeleryAppExposure:
    """`celery -A vaultshield_backend` and `@shared_task` both need this."""

    def test_package_re_exports_the_app_as_celery_app(self):
        # The attribute name is Celery's convention for resolving `-A
        # <package>`, so the compose worker/beat services find the app
        # through it.
        assert vaultshield_backend.celery_app is app
        assert vaultshield_backend.__all__ == ("celery_app",)

    def test_importing_django_settings_constructs_the_app(self):
        # `@shared_task` binds to whatever app is current, so an app that is
        # only built inside the worker would leave `run_security_scan.delay()`
        # in a Django view (Task 23.2) talking to an unconfigured default
        # app. Importing the settings module means importing the package,
        # which imports the app — and this test run proves it, since it is
        # `settings_test` that pytest loaded.
        assert app.tasks is not None
        assert run_security_scan.app is app


class TestBrokerAndResultBackend:
    """Requirement 18.2: the task runs on *the configured* broker/backend."""

    def test_celery_reads_both_from_django_settings(self):
        # The `CELERY_` prefix stripped by `namespace="CELERY"` is what makes
        # these two settings visible to Celery at all; without it the app
        # would silently keep its own `amqp://` default.
        assert app.conf.broker_url == settings.CELERY_BROKER_URL
        assert app.conf.result_backend == settings.CELERY_RESULT_BACKEND

    def test_both_point_at_redis_on_separate_logical_databases(self):
        assert app.conf.broker_url.startswith("redis://")
        assert app.conf.result_backend.startswith("redis://")
        # Same server, different keyspace: inspecting or flushing the broker
        # while debugging a queue cannot discard stored results.
        assert app.conf.broker_url != app.conf.result_backend

    def test_redis_url_defaults_to_localhost_and_takes_the_compose_host(self, monkeypatch):
        # The two addresses the same settings module is imported under: a
        # host-run `manage.py` reaching a published port, and a compose
        # service reaching design.md's `redis` service by name.
        monkeypatch.delenv("REDIS_HOST", raising=False)
        monkeypatch.delenv("REDIS_PORT", raising=False)
        assert _redis_url(0) == "redis://localhost:6379/0"

        monkeypatch.setenv("REDIS_HOST", "redis")
        monkeypatch.setenv("REDIS_PORT", "6380")
        assert _redis_url(1) == "redis://redis:6380/1"


class TestTaskAutodiscovery:
    """The scan task has to be registered in the worker that runs it."""

    def test_run_security_scan_is_discovered_from_installed_apps(self):
        # `import_default_modules()` is what a starting worker calls, and the
        # signal it sends is what `autodiscover_tasks()` deferred its
        # `INSTALLED_APPS` scan onto. Calling it here reproduces the worker's
        # registration step without starting one.
        app.loader.import_default_modules()

        assert run_security_scan.name == "security_scan.tasks.run_security_scan"
        assert run_security_scan.name in app.tasks

    def test_the_scheduled_tasks_are_discovered_from_their_own_module_name(self):
        # This app's task module is `tasks/scheduled.py`, so the default
        # `related_name="tasks"` scan looks for `tasks.tasks` and finds
        # nothing. `celery.py`'s second `autodiscover_tasks` call is the only
        # reason these two are in the registry — and a worker that lacks them
        # answers every message Beat enqueues with `NotRegistered`, since a
        # schedule entry carries a name rather than an import.
        app.loader.import_default_modules()

        assert cleanup_orphaned_sandboxes.name in app.tasks
        assert flag_stuck_approvals.name in app.tasks

    def test_no_task_module_is_named_explicitly_in_configuration(self):
        # Autodiscovery is therefore the only path by which the lines above
        # can hold in a fresh worker process: nothing lists
        # `security_scan.tasks` or `tasks.scheduled` for import, so a new task
        # module in either convention needs no configuration edit.
        assert not app.conf.imports
        assert not app.conf.include


class TestBeatSchedule:
    """Requirements 25.1 and 25.2: both tasks run "on a configured schedule".

    `celery -A vaultshield_backend beat` reads exactly this mapping, so an
    entry that is absent, misnamed, or carrying kwargs the task does not
    accept is the difference between a periodic pass and silence.
    """

    def test_celery_reads_the_schedule_from_django_settings(self):
        # Same `CELERY_` prefix mechanism as the broker URL: without it Beat
        # keeps its own empty default and enqueues nothing.
        assert app.conf.beat_schedule == settings.CELERY_BEAT_SCHEDULE

    def test_both_periodic_tasks_are_registered(self):
        scheduled_names = {entry["task"] for entry in app.conf.beat_schedule.values()}

        assert scheduled_names == {
            "tasks.scheduled.cleanup_orphaned_sandboxes",  # Requirement 25.1
            "tasks.scheduled.flag_stuck_approvals",  # Requirement 25.2
        }

    def test_each_entry_names_a_task_the_worker_has_registered(self):
        # The schedule spells its tasks as strings, so a renamed module or
        # function would leave Beat enqueueing a name no worker knows. Pairing
        # the two directions here is what makes that a failed test rather than
        # a `NotRegistered` in a worker log.
        app.loader.import_default_modules()

        for entry in app.conf.beat_schedule.values():
            assert entry["task"] in app.tasks

    def test_each_entry_passes_kwargs_its_task_accepts(self):
        tasks_by_name = {
            cleanup_orphaned_sandboxes.name: cleanup_orphaned_sandboxes,
            flag_stuck_approvals.name: flag_stuck_approvals,
        }

        for entry in app.conf.beat_schedule.values():
            signature = inspect.signature(tasks_by_name[entry["task"]].run)
            # `bind` raises TypeError on an unknown or misspelled keyword,
            # which is otherwise only discovered when the first tick reaches a
            # worker; the assertion pins that every kwarg landed on a real
            # parameter rather than being swallowed by a `**kwargs`.
            bound = signature.bind(**entry["kwargs"])

            assert set(bound.arguments) == set(entry["kwargs"])

    def test_the_configured_thresholds_match_the_task_defaults(self):
        # settings.py restates design.md's 30 minutes and 24 hours because it
        # cannot import `tasks.scheduled` (that module imports `runs.models`,
        # and settings load before the app registry). This is the check that
        # keeps the two copies from drifting.
        assert settings.SANDBOX_TTL_MINUTES == DEFAULT_SANDBOX_TTL_MINUTES
        assert settings.APPROVAL_MAX_AGE_HOURS == DEFAULT_APPROVAL_MAX_AGE_HOURS

    def test_the_reaper_sweeps_more_often_than_the_ttl_it_enforces(self):
        # Not a correctness condition — the task is idempotent at any interval
        # — but it is what bounds how long a reapable record stays `active`,
        # so it is worth failing on rather than rediscovering.
        cleanup = app.conf.beat_schedule["cleanup-orphaned-sandboxes"]

        assert cleanup["schedule"] <= timedelta(minutes=settings.SANDBOX_TTL_MINUTES)

    def test_every_interval_is_a_positive_duration(self):
        for entry in app.conf.beat_schedule.values():
            assert entry["schedule"] > timedelta(0)


class TestScheduleIntervalsAreEnvironmentDriven:
    """The four durations are `_env_int` reads, like every other tunable here.

    A deployment adjusts them through compose's environment block rather than
    by editing the schedule, so the parsing has to hold up on the values a
    compose file or `.env` actually produces.
    """

    def test_an_unset_variable_takes_the_default(self, monkeypatch):
        monkeypatch.delenv("VAULTSHIELD_SANDBOX_TTL_MINUTES", raising=False)

        assert _env_int("VAULTSHIELD_SANDBOX_TTL_MINUTES", 30) == 30

    def test_a_set_variable_overrides_it(self, monkeypatch):
        monkeypatch.setenv("VAULTSHIELD_SANDBOX_TTL_MINUTES", "45")

        assert _env_int("VAULTSHIELD_SANDBOX_TTL_MINUTES", 30) == 45

    def test_a_blank_variable_is_treated_as_unset(self, monkeypatch):
        # `FOO:` in a compose environment block, or `FOO=` in an `.env`, both
        # arrive as the empty string — the same tolerance `_env_flag` and
        # `_env_list` have.
        monkeypatch.setenv("VAULTSHIELD_SANDBOX_TTL_MINUTES", "  ")

        assert _env_int("VAULTSHIELD_SANDBOX_TTL_MINUTES", 30) == 30

    @pytest.mark.parametrize("value", ["0", "-15", "ten", "15.5"])
    def test_a_value_that_is_not_a_positive_integer_is_refused(self, monkeypatch, value):
        # Zero or negative would make Beat tick continuously (as an interval)
        # or reap rows it was meant to spare (as a threshold), and a typo
        # would otherwise surface as a `TypeError` inside a worker.
        monkeypatch.setenv("VAULTSHIELD_SANDBOX_CLEANUP_INTERVAL_MINUTES", value)

        with pytest.raises(ImproperlyConfigured):
            _env_int("VAULTSHIELD_SANDBOX_CLEANUP_INTERVAL_MINUTES", 15)
