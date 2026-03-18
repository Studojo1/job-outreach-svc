-- Migration: Create payment tables for Razorpay integration
-- Tables: coupons, payment_orders, user_credits

CREATE TABLE IF NOT EXISTS coupons (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,
    discount_type VARCHAR(20) NOT NULL DEFAULT 'percent',  -- 'percent' or 'flat'
    discount_value NUMERIC(10, 2) NOT NULL,                -- percentage (0-100) or flat amount in USD cents
    max_uses INTEGER DEFAULT NULL,                         -- NULL = unlimited
    uses INTEGER NOT NULL DEFAULT 0,
    valid_from TIMESTAMP DEFAULT NOW(),
    valid_until TIMESTAMP DEFAULT NULL,                    -- NULL = no expiry
    distributor_name VARCHAR(255),                         -- who distributed this coupon
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_orders (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    razorpay_order_id VARCHAR(255) UNIQUE,
    razorpay_payment_id VARCHAR(255),
    razorpay_signature VARCHAR(512),
    amount_cents INTEGER NOT NULL,                         -- amount in smallest currency unit (paise or cents)
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    tier INTEGER NOT NULL,                                 -- 200, 350, or 500
    coupon_id INTEGER REFERENCES coupons(id) ON DELETE SET NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'created',         -- created, paid, failed, refunded
    credits_granted INTEGER NOT NULL DEFAULT 0,
    idempotency_key VARCHAR(255) UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_credits (
    id SERIAL PRIMARY KEY,
    user_id TEXT UNIQUE NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    total_credits INTEGER NOT NULL DEFAULT 0,
    used_credits INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_payment_orders_user_id ON payment_orders(user_id);
CREATE INDEX idx_payment_orders_status ON payment_orders(status);
CREATE INDEX idx_payment_orders_razorpay_order_id ON payment_orders(razorpay_order_id);
CREATE INDEX idx_coupons_code ON coupons(code);
CREATE INDEX idx_user_credits_user_id ON user_credits(user_id);
