import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from api.app.config import get_settings
from api.app.database import Base, get_db
from api.app.dependencies import reset_rate_limits
from api.app.main import app
from api.app.routes.participants import reset_registration_limits


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    """The limiters are process-global, so counts would otherwise leak between tests."""
    reset_rate_limits()
    reset_registration_limits()
    yield
    reset_rate_limits()
    reset_registration_limits()


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")

    # SQLite ignores foreign keys unless this is switched on per connection, and PostgreSQL
    # does not. That gap hid a real bug for the whole project: the models declare ForeignKey
    # columns but no relationship(), so SQLAlchemy's unit of work has no mapper-level
    # dependency and flushes in alphabetical mapper order - Event before Participant. Every
    # test inserted events whose parent row did not exist yet and passed; PostgreSQL rejected
    # the first one it ever saw.
    #
    # With this on, the fast suite catches that class of bug instead of production doing it.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):  # noqa: ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)

    def override_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        # Hung on the client so a test can inspect the rows the API just wrote, against the SAME
        # SQLite file. Reaching for `SessionLocal` instead connects to the real PostgreSQL and
        # silently bypasses this override - which is how one test came to fail with "column
        # closed_at does not exist" while the feature worked perfectly.
        test_client.sessions = sessions
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_headers():
    return {"X-API-Key": get_settings().development_api_key}