from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from core.config import settings

# If settings object is unavailable (no .env), default to a stub
SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL if settings and hasattr(settings, "DATABASE_URL") else "postgresql://username:password@localhost/dbname"

# Sized explicitly rather than by SQLAlchemy's defaults. 5 + 10 matches what
# each pod already had, so the database sees no more connections than before.
# pool_timeout is the change: the default 30s wait for a free connection landed
# exactly on the frontend's 30s request timeout, so a starved request hung and
# then retried. 10s fails it fast instead.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=10,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """Dependency to yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
