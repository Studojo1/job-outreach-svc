-- Per-account residential-proxy country targeting.
-- The customer's exit country is geolocated from their real IP at connect and
-- stored here, so all proxied LinkedIn activity for that account egresses from
-- their own country (account-safety). Country-level; city is informational.

ALTER TABLE linkedin_tokens ADD COLUMN IF NOT EXISTS proxy_country TEXT;
ALTER TABLE linkedin_tokens ADD COLUMN IF NOT EXISTS proxy_city TEXT;
