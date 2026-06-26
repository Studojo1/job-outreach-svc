-- Per-recipient founder coupons: bind a coupon to a single buyer.
--
-- emailer-service mints UNIQUE single-use codes (max_uses=1) per recipient for
-- the founder-coupon emails (cc-outreach-coupon, cc-cart-goat) and sets user_id
-- to the recipient. The 10h expiry clock (valid_until) starts when the recipient
-- opens the email. This column lets validate_coupon / create-order reject a
-- leaked code redeemed by anyone other than the bound user.
--
-- NULL = unbound (legacy blanket codes like STUDOJO20, partner codes) and keeps
-- working exactly as before. Idempotent: safe to re-run.

ALTER TABLE coupons
  ADD COLUMN IF NOT EXISTS user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_coupons_user_id ON coupons(user_id);
