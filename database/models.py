from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    google_sub = Column(String(255), unique=True)
    name = Column(String(255))
    avatar_url = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    email_accounts = relationship("EmailAccount", back_populates="user", cascade="all, delete-orphan")
    candidates = relationship("Candidate", back_populates="user", cascade="all, delete-orphan")


class EmailAccount(Base):
    __tablename__ = "email_accounts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    email_address = Column(String(255), unique=True, nullable=False)
    provider = Column(String(50), default="gmail", nullable=False)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text)
    token_expiry = Column(DateTime)
    daily_send_limit = Column(Integer, default=10, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="email_accounts")
    campaigns = relationship("Campaign", back_populates="email_account", cascade="all, delete-orphan")


class Candidate(Base):
    __tablename__ = "candidates"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    resume_text = Column(Text)
    parsed_json = Column(JSONB)
    target_roles = Column(JSONB)
    target_industries = Column(JSONB)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="candidates")
    leads = relationship("Lead", back_populates="candidate", cascade="all, delete-orphan")
    campaigns = relationship("Campaign", back_populates="candidate", cascade="all, delete-orphan")


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"))
    apollo_id = Column(String(255))
    name = Column(String(255))
    title = Column(String(255))
    company = Column(String(255))
    industry = Column(String(255))
    location = Column(String(255))
    linkedin_url = Column(Text)
    email = Column(String(255))
    company_size = Column(String(50))
    email_verified = Column(Boolean, default=False)
    status = Column(String(50), default="new")
    created_at = Column(DateTime, default=datetime.utcnow)

    candidate = relationship("Candidate", back_populates="leads")
    scores = relationship("LeadScore", back_populates="lead", cascade="all, delete-orphan")
    emails_sent = relationship("EmailSent", back_populates="lead", cascade="all, delete-orphan")


class LeadScore(Base):
    __tablename__ = "lead_scores"
    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"))
    overall_score = Column(Integer, nullable=False)
    title_relevance = Column(Integer, nullable=False)
    department_relevance = Column(Integer, nullable=False)
    industry_relevance = Column(Integer, nullable=False)
    seniority_relevance = Column(Integer, nullable=False)
    location_relevance = Column(Integer, nullable=False)
    explanation = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="scores")


class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"))
    email_account_id = Column(Integer, ForeignKey("email_accounts.id", ondelete="CASCADE"))
    name = Column(String(255), nullable=False)
    status = Column(String(50), default="draft")
    subject_template = Column(Text)
    body_template = Column(Text)
    daily_limit = Column(Integer, default=20)
    created_at = Column(DateTime, default=datetime.utcnow)

    candidate = relationship("Candidate", back_populates="campaigns")
    email_account = relationship("EmailAccount", back_populates="campaigns")
    emails_sent = relationship("EmailSent", back_populates="campaign", cascade="all, delete-orphan")


class EmailSent(Base):
    __tablename__ = "emails_sent"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"))
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"))
    message_id = Column(String(255))
    subject = Column(Text)
    body = Column(Text)
    to_email = Column(String(255))
    assigned_style = Column(String(50))  # Email style: warm_intro, value_prop, company_curiosity, peer_to_peer, direct_ask
    sent_at = Column(DateTime)
    status = Column(String(50), default="queued")
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="emails_sent")
    lead = relationship("Lead", back_populates="emails_sent")
