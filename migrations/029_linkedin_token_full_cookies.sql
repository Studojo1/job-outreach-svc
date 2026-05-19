-- Store the full LinkedIn cookie jar (encrypted JSON) captured by the extension.
-- LinkedIn binds session validity to a complete cookie set; partial cookies
-- (just li_at + JSESSIONID) trigger a "stolen cookies / new device" risk flag
-- which causes ERR_TOO_MANY_REDIRECTS through our proxy. Storing all cookies
-- (bcookie, bscookie, lidc, li_mc, lang, etc.) and replaying them lets the
-- proxy session pass LinkedIn's anti-fraud checks.

ALTER TABLE linkedin_tokens
  ADD COLUMN IF NOT EXISTS cookies_blob_enc TEXT,
  ADD COLUMN IF NOT EXISTS cookies_blob_nonce TEXT;
