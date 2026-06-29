from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Numeric, ARRAY
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
    last_reply_check_at = Column(DateTime)             # Last time we polled inbox for replies
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
    flex_notes = Column(JSONB)  # post-payment: best_project + outcome answers for email personalisation
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
    enrichment_fail_count = Column(Integer, default=0)
    company_description = Column(Text)  # Apollo organization.short_description for email hook
    company_domain = Column(Text, index=True)  # join key for company_profiles enrichment cache
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
    justification_json = Column(JSONB)  # LLM-generated per-lead reasoning for top-K
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="scores")


class CompanyProfile(Base):
    """Globally cached company enrichment data — Apollo /organizations/enrich
    plus optional homepage scrape. Keyed by domain so the same company doesn't
    get re-enriched per user."""
    __tablename__ = "company_profiles"
    id = Column(Integer, primary_key=True, index=True)
    domain = Column(Text, unique=True, nullable=False, index=True)
    name = Column(Text)
    apollo_org_id = Column(Text, index=True)
    short_description = Column(Text)
    industries = Column(JSONB)
    keywords = Column(JSONB)
    technologies = Column(JSONB)
    employee_count = Column(Integer)
    founded_year = Column(Integer)
    headquarters_city = Column(Text)
    website_summary = Column(Text)
    scrape_meta_title = Column(Text)
    scrape_meta_description = Column(Text)
    scrape_hero_text = Column(Text)
    last_apollo_enriched_at = Column(DateTime)
    last_scraped_at = Column(DateTime)
    scrape_failed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Round-2 quality upgrade — multi-page scrape sections (migration 027).
    product_page_summary = Column(Text)   # /products | /platform | /solutions
    blog_summary = Column(Text)           # /blog hero / latest posts
    careers_summary = Column(Text)        # /careers — job-page text

    # Deeper Apollo enrich fields (migration 027).
    recent_news_summary = Column(Text)
    latest_funding_round_date = Column(DateTime)
    latest_funding_amount = Column(Integer)  # BIGINT in Postgres
    linkedin_url = Column(Text)

    # Apollo job postings list — {title, snippet, posted_at} per posting.
    recent_job_postings = Column(JSONB)

    # Structured per-company facts produced by company_fact_extractor LLM.
    extracted_facts = Column(JSONB)


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
    selected_styles = Column(JSONB)  # persist style choices for JIT email generation
    generation_mode = Column(String(20), default="ai")  # 'ai' or 'template'
    paused_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)     # set when status → running
    completed_at = Column(DateTime, nullable=True)   # set when status → completed
    expires_at = Column(DateTime, nullable=True)     # set for duration-limited plans (e.g. email_50 = 8 days)
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
    enrichment_status = Column(String(20), default="pending")  # pending, enriched, failed, skipped
    scheduled_at = Column(DateTime)  # When this email should be sent (timezone-aware scheduling)
    sent_at = Column(DateTime)
    status = Column(String(50), default="queued")
    error_message = Column(Text)
    thread_id = Column(String(255))                    # Gmail threadId for reply matching
    reply_text = Column(Text)                          # First reply body text
    reply_received_at = Column(DateTime)               # When first reply was received
    reply_sentiment = Column(String(20))               # positive, negative, neutral
    bounce_reason = Column(Text)                       # Bounce reason if bounced
    is_test = Column(Boolean, default=False)           # True for "Send Test Emails" feature
    followup_number = Column(Integer, nullable=False, default=0)  # 0 = Touch 1, 1 = Touch 2, 2 = Touch 3
    parent_email_id = Column(Integer, ForeignKey("emails_sent.id", ondelete="CASCADE"))  # Points to Touch 1 for follow-ups
    message_id_header = Column(String(500))            # Gmail RFC 5322 Message-ID header for In-Reply-To threading
    # Auto-replenishment lineage. NULL on the original row, set on a replacement
    # row to the id of the row that triggered it. enrichment_status may also use
    # the value 'credit_paused' when an Apollo credit/rate-limit error pauses an
    # email instead of advancing it toward the 3-attempt cap.
    replacement_for_id = Column(Integer, ForeignKey("emails_sent.id", ondelete="SET NULL"))
    replacement_reason = Column(String(40))             # 'bounce' | 'enrichment_exhausted'
    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="emails_sent")
    lead = relationship("Lead", back_populates="emails_sent")


class OutreachOrder(Base):
    """Tracks the full lifecycle of an outreach run.

    State machine (JIT enrichment — enrichment happens during campaign_running):
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

    # JIT credit tracking
    credits_reserved = Column(Integer, default=0)   # total credits purchased for this order
    credits_used = Column(Integer, default=0)        # credits consumed by successful enrichments
    credits_refunded = Column(Integer, default=0)    # credits returned on cancel/failure

    # Logs — JSONB array of timestamped action entries
    action_log = Column(JSONB, default=list)

    # Per-stage funnel timestamps. The `status` column drives the state machine;
    # these timestamps are append-only history of when the user reached each
    # stage. Used by the admin dashboard to show drop-off across the whole
    # journey, not just current state.
    resume_uploaded_at      = Column(DateTime, nullable=True)
    quiz_started_at         = Column(DateTime, nullable=True)
    quiz_completed_at       = Column(DateTime, nullable=True)
    leads_generated_at      = Column(DateTime, nullable=True)
    payment_page_reached_at = Column(DateTime, nullable=True)
    payment_made_at         = Column(DateTime, nullable=True)
    gmail_connected_at      = Column(DateTime, nullable=True)
    email_style_selected_at = Column(DateTime, nullable=True)
    campaign_setup_at       = Column(DateTime, nullable=True)
    campaign_launched_at    = Column(DateTime, nullable=True)
    campaign_paused_at      = Column(DateTime, nullable=True)
    campaign_completed_at   = Column(DateTime, nullable=True)

    # LinkedIn channel
    plan_type = Column(Text, nullable=False, default="email")  # "email" | "linkedin" | "both"
    linkedin_campaign_id = Column(Integer, ForeignKey("linkedin_campaigns.id", ondelete="SET NULL"), nullable=True)
    linkedin_connected_at = Column(DateTime, nullable=True)
    linkedin_credits_reserved = Column(Integer, nullable=False, default=0)
    linkedin_credits_used = Column(Integer, nullable=False, default=0)

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
    # When set, this coupon is bound to a single buyer (per-recipient founder
    # coupons minted by emailer-service). A leaked code can't be redeemed by
    # anyone else. NULL = unbound (legacy blanket codes, partner codes, etc.).
    user_id = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    payment_orders = relationship("PaymentOrder", back_populates="coupon")


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(20), default="razorpay", nullable=False)
    razorpay_order_id = Column(String(255), unique=True)
    razorpay_payment_id = Column(String(255))
    razorpay_signature = Column(String(512))
    dodo_checkout_id = Column(String(255))
    dodo_payment_id = Column(String(255))
    geo_country = Column(String(5))
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    tier = Column(Integer, nullable=False)
    plan_id = Column(Text, nullable=True)  # e.g. "email_200", "linkedin_350", "both_500"
    coupon_id = Column(Integer, ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True)
    outreach_order_id = Column(Integer, ForeignKey("outreach_orders.id", ondelete="SET NULL"), nullable=True)
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


class LinkedInToken(Base):
    __tablename__ = "linkedin_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False)
    li_at_enc = Column(Text, nullable=False)       # base64(AES-256-GCM ciphertext)
    jsessionid_enc = Column(Text, nullable=False)  # base64(AES-256-GCM ciphertext)
    nonce = Column(Text, nullable=False)           # base64 GCM nonce (12 bytes)
    linkedin_name = Column(Text)
    proxy_session_id = Column(Text)               # sticky residential proxy session ID per account
    proxy_country = Column(Text, nullable=True)   # ISO-2 country to pin the proxy exit to (geolocated at connect)
    proxy_city = Column(Text, nullable=True)      # city (informational; Evomi targeting is country-level)
    # 'proxy' = cookies created via our Playwright login through proxy (works server-side).
    # 'extension' = cookies from browser extension bound to user's home IP — only the
    # extension itself can use them; server-side sends will hit a redirect loop.
    connection_mode = Column(Text, nullable=False, default="proxy")
    # Encrypted JSON of the full LinkedIn cookie jar captured by the extension
    # (bcookie, bscookie, lidc, li_mc, lang, etc.). When set, replayed verbatim
    # to satisfy LinkedIn's anti-fraud cookie completeness check on the proxy IP.
    cookies_blob_enc = Column(Text, nullable=True)
    cookies_blob_nonce = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    search_jobs = relationship("LinkedInSearchJob", back_populates="token_user", cascade="all, delete-orphan",
                               foreign_keys="LinkedInSearchJob.user_id",
                               primaryjoin="LinkedInToken.user_id == LinkedInSearchJob.user_id")


class LinkedInSearchJob(Base):
    __tablename__ = "linkedin_search_jobs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    target_role = Column(Text, nullable=False)
    target_companies = Column(ARRAY(Text), default=[])
    location = Column(Text)
    status = Column(String(20), default="queued", nullable=False)  # queued|running|done|failed
    result_count = Column(Integer, default=0)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    token_user = relationship("LinkedInToken", back_populates="search_jobs",
                              foreign_keys=[user_id],
                              primaryjoin="LinkedInSearchJob.user_id == LinkedInToken.user_id")
    leads = relationship("LinkedInOutreachLead", back_populates="search_job", cascade="all, delete-orphan")


class LinkedInOutreachLead(Base):
    __tablename__ = "linkedin_outreach_leads"
    id = Column(Integer, primary_key=True, index=True)
    search_job_id = Column(Integer, ForeignKey("linkedin_search_jobs.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    headline = Column(Text)
    company = Column(Text)
    profile_url = Column(Text, nullable=False)
    profile_image_url = Column(Text)
    suggested_message = Column(Text)
    message_copied_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    search_job = relationship("LinkedInSearchJob", back_populates="leads")


class LinkedInCampaign(Base):
    __tablename__ = "linkedin_campaigns"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    # Optional link to a candidate (student) profile — when present, the campaign
    # targeting is auto-derived from resume_profile + quiz output, and per-lead
    # message + match-reason use the student's context.
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True)
    name = Column(Text, nullable=False)
    status = Column(String(20), default="draft", nullable=False)  # draft|running|paused|completed
    # Quiz / ICP config
    target_role = Column(Text, nullable=False)
    target_industries = Column(JSONB, default=list)   # ["SaaS", "Fintech"]
    target_locations = Column(JSONB, default=list)    # ["India", "US"]
    target_company_sizes = Column(JSONB, default=list) # ["1-50", "51-200"]
    target_keywords = Column(Text)                    # free-text extra filter
    # Message templates
    connection_note = Column(Text)     # ≤300 chars, personalised per lead by AI
    followup_message = Column(Text)    # sent after connection accepted
    # Automation settings
    daily_limit = Column(Integer, default=12)
    # Acceptance rate is ~10pt higher without a connection note. Default off,
    # surfaced in UI with a disclaimer if the user opts in.
    send_with_note = Column(Boolean, nullable=False, default=False)
    # Per-campaign rolling-7-day invite cap. Initialised to a random 92-97 at
    # launch so we stay safely under LinkedIn's ~100/week soft limit without
    # every campaign hitting the same round number.
    weekly_invite_limit = Column(Integer, nullable=False, default=95)
    # Optional: visit the lead's recent activity and like their latest post
    # before sending the invite. Bumps acceptance for warm leads.
    like_post_before_connect = Column(Boolean, nullable=False, default=False)
    # Aggregate stats (denormalised for fast reads)
    total_leads = Column(Integer, default=0)
    total_sent = Column(Integer, default=0)
    total_accepted = Column(Integer, default=0)
    total_followups_sent = Column(Integer, default=0)
    total_replied = Column(Integer, default=0)
    launched_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    requests = relationship("LinkedInConnectionRequest", back_populates="campaign", cascade="all, delete-orphan")


class LinkedInConnectionRequest(Base):
    __tablename__ = "linkedin_connection_requests"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("linkedin_campaigns.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Text, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    # Lead info
    name = Column(Text, nullable=False)
    headline = Column(Text)
    company = Column(Text)
    location = Column(Text)
    profile_url = Column(Text, nullable=True)   # resolved by daemon via Voyager if None
    profile_urn = Column(Text)         # fsd_profile URN for Voyager API
    profile_image_url = Column(Text)
    # Outreach content
    connection_note = Column(Text)     # personalised note sent with request
    followup_message = Column(Text)    # personalised follow-up
    match_reason = Column(Text)        # one-line "why this lead is a match" for the student
    # Status tracking
    status = Column(String(30), default="pending", nullable=False)
    # pending | sent | accepted | declined | ignored | followup_sent | replied
    sent_at = Column(DateTime)
    accepted_at = Column(DateTime)
    followup_sent_at = Column(DateTime)
    reply_text = Column(Text)
    reply_received_at = Column(DateTime)
    reply_sentiment = Column(String(10))  # positive|negative|neutral
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign = relationship("LinkedInCampaign", back_populates="requests")
    user = relationship("User")
