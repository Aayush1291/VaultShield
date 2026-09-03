# Image behind the `web`, `channels`, `celery-worker`, and `celery-beat`
# services in the root `docker-compose.yml` (design.md's compose topology).
# One image, four commands: which process runs is the compose service's
# concern, not this file's.

FROM python:3.11-slim

# `requires-python = ">=3.11"` in pyproject.toml; the slim variant is enough
# because no dependency needs a compiler — `psycopg[binary]` bundles libpq and
# Pillow ships manylinux wheels, which is why `psycopg[binary]` was chosen over
# `psycopg` in the first place.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The whole project is copied before installing rather than dependencies
# first, because hatchling resolves the `[tool.hatch.build.targets.wheel]`
# package list at install time — an editable install with the app packages
# absent fails. `.dockerignore` keeps the host `.venv/`, caches, and local
# `media/` out of the build context.
COPY . .

# `.[server]` is the same editable install the README documents for a
# developer venv, plus gunicorn and daphne. The `dev` extra (pytest,
# hypothesis) is deliberately excluded: the suite runs on the host against
# SQLite and needs no container.
RUN pip install --no-cache-dir -e ".[server]"

# `MEDIA_ROOT` defaults to `BASE_DIR / "media"`. The directory is created here
# so the compose named volume mounts onto a path owned by the runtime user,
# and so an image run without that volume still has somewhere to write.
RUN mkdir -p /app/media /app/staticfiles \
    && useradd --create-home --uid 10001 vaultshield \
    && chown -R vaultshield:vaultshield /app

USER vaultshield

# gunicorn/WSGI, matching the `web` service. Every compose service states its
# own `command`, so this is only the default for a bare `docker run`.
EXPOSE 8000
CMD ["gunicorn", "vaultshield_backend.wsgi:application", "--bind", "0.0.0.0:8000"]
