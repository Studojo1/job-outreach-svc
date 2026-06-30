"""Mesa ORM models — saved LinkedIn job searches per B2B client + scraped jobs.

Reuses the shared declarative Base so these register on the same metadata.
Tables are created by migrations/038_mesa_job_scraper.sql.
"""

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from database.models import Base


class MesaSearch(Base):
    __tablename__ = "mesa_searches"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True, nullable=False)  # owning B2B client
    name = Column(Text, nullable=False)
    keywords = Column(Text, nullable=False)
    location = Column(Text, default="")
    date_posted = Column(String(20), default="24h")        # 24h | week | month | any
    workplace_types = Column(ARRAY(Text), default=list)    # on-site | remote | hybrid
    experience_levels = Column(ARRAY(Text), default=list)  # internship..executive
    is_active = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MesaJob(Base):
    __tablename__ = "mesa_jobs"

    id = Column(Integer, primary_key=True)
    search_id = Column(Integer, ForeignKey("mesa_searches.id", ondelete="CASCADE"),
                       index=True, nullable=False)
    linkedin_job_id = Column(String, nullable=False)
    title = Column(Text)
    company = Column(Text)
    location = Column(Text)
    posted_date = Column(String(40))  # LinkedIn's listdate, e.g. '2026-06-24'
    url = Column(Text)
    scraped_at = Column(DateTime, default=datetime.utcnow)
