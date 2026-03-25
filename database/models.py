from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Numeric
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()


class User(Base):
    __tablename__ = "user"
    id = Column(Text, primary_key=True, index=True)
    email = Column(Text, unique=True, index=True, nullable=False)
    name = Column(Text, nullable=False)
    image = Column(Text)
    role = Column(String(50))
    email_verified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    email_accounts = relationship("EmailAccount", back_populates="user", cascade="all, delete-orphan")
    candidates = relationship("Candidate", back_populates="user", cascade="all, delete-orphan")
    outreach_orders = relationship("OutreachOrder", back_populates="user", cascade="all, delete-orphan")
    payment_orders = relationship("PaymentOrder", back_populates="user", cascade="all, delete-orphan")
    user_credits = relationship("UserCredit", back_populates="user", cascade="all, delete-orphan")


class BetterAuthSession(Base):
    """Maps to the BetterAuth 'session' table (shared DB)."""
    __tablename__ = "session"
    id = Column(Text, primary_key=True)
    token = Column(Text, unique=True, nullable=False, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    ip_address = Column(Text)
    user_agent = Column(Text)
    impersonated_by = Column(Text)


class EmailAccount(Base):
    __tablename__ = "email_accounts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"))
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
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"))
    resume_text = Column(Text)
    parsed_json = Column(JSONB)
    resume_profile = Column(JSONB)  # pre-extracted intelligence for adaptive quiz
    dream_companies = Column(JSONB)  # user-specified target companies from quiz
    psychometric_profile = Column(JSONB)  # 4-dimension scoring + traits + confidence
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
    user_timezone = Column(String(50), default="Asia/Kolkata")
    paused_at = Column(DateTime, nullable=True)
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
    scheduled_at = Column(DateTime)  # When this email should be sent (timezone-aware scheduling)
    sent_at = Column(DateTime)
    status = Column(String(50), default="queued")
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="emails_sent")
    lead = relationship("Lead", back_populates="emails_sent")


class OutreachOrder(Base):
    """Tracks the full lifecycle of an outreach run.

    State machine:
      CREATED → LEADS_GENERATING → LEADS_READY → CAMPAIGN_SETUP
      → EMAIL_CONNECTED → CAMPAIGN_RUNNING → COMPLETED

    Enables users to leave mid-process and resume later via My Orders.
    """
    __tablename__ = "outreach_orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)
    email_account_id = Column(Integer, ForeignKey("email_accounts.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(50), default="created", nullable=False)
    leads_collected = Column(Integer, default=0)
    leads_target = Column(Integer, default=500)

    # Logs — JSONB array of timestamped action entries
    action_log = Column(JSONB, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="outreach_orders")


class Coupon(Base):
    __tablename__ = "coupons"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False)
    discount_type = Column(String(20), default="percent", nullable=False)
    discount_value = Column(Numeric(10, 2), nullable=False)
    max_uses = Column(Integer, nullable=True)
    uses = Column(Integer, default=0, nullable=False)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_until = Column(DateTime, nullable=True)
    distributor_name = Column(String(255))
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    payment_orders = relationship("PaymentOrder", back_populates="coupon")


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    razorpay_order_id = Column(String(255), unique=True)
    razorpay_payment_id = Column(String(255))
    razorpay_signature = Column(String(512))
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    tier = Column(Integer, nullable=False)
    coupon_id = Column(Integer, ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(50), default="created", nullable=False)
    credits_granted = Column(Integer, default=0, nullable=False)
    idempotency_key = Column(String(255), unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="payment_orders")
    coupon = relationship("Coupon", back_populates="payment_orders")


class UserCredit(Base):
    __tablename__ = "user_credits"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False)
    total_credits = Column(Integer, default=0, nullable=False)
    used_credits = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="user_credits")
