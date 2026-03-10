from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from job_outreach_tool.core.config import settings

# If settings object is unavailable (no .env), default to a stub
SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL if settings and hasattr(settings, "DATABASE_URL") else "postgresql://username:password@localhost/dbname"

engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """Dependency to yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
