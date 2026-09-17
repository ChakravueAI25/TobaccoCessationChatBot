"""Alembic environment.

**The database URL comes from the application's own settings, not from `alembic.ini`.**
`api.app.config.Settings` reads `.env` (and lets a real environment variable override it), so
migrations and the running service can never disagree about which database they mean.

This is deliberately not the generated Alembic wiring, which was wrong in two ways that
compounded into "PostgreSQL has never been connected":

* it read `DATABASE_URL` from `os.environ` only, so the password in `.env` was ignored and it
  fell back to the `alembic.ini` placeholder — the failure surfaced as
  ``password authentication failed for user "unused"``, which names a user nobody configured;
* `config.set_main_option` writes through ConfigParser, which treats ``%`` as interpolation
  syntax. A URL-encoded password — and any password containing ``@`` has to be percent-encoded
  to survive URL parsing at all — raised ``invalid interpolation syntax`` even when the value
  was correct.

Using the engine directly avoids both, because nothing round-trips through the ini file.
"""
from logging.config import fileConfig

from alembic import context

from api.app.config import get_settings
from api.app.database import Base, engine
from api.app import models  # noqa: F401  - imported for its side effect: registers every table

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The application engine, so a migration can never run against a different database than
    # the service it is migrating for.
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
