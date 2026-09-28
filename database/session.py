from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings

# If settings object is unavailable (no .env), default to a stub
SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL if settings and hasattr(settings, "DATABASE_URL") else "postgresql://username:password@localhost/dbname"

# Sized explicitly rather than by SQLAlchemy's defaults. 5 + 10 matches what
# each pod already had, so the database sees no more connections than before.
# pool_timeout is the change: the default 30s wait for a free connection landed
# exactly on the frontend's 30s request timeout, so a starved request hung and
# then retried. 10s fails it fast instead.
# lock_timeout (audit P15): production ran with 0, so a request waiting on a
# row lock waited forever and could stall a pod's only event loop. 10s turns
# that into an error the handler reports. Postgres only; tests use SQLite.
_connect_args = (
    {"options": "-c lock_timeout=10000"}
    if SQLALCHEMY_DATABASE_URL.startswith("postgresql") else {}
)
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=10,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """Dependency to yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
