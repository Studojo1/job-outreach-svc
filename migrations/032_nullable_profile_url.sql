-- Allow linkedin_connection_requests.profile_url to be NULL.
-- The automation daemon resolves the URL via Voyager search (name+company)
-- just before sending, so requests created without a known LinkedIn URL
-- are still processable.
ALTER TABLE linkedin_connection_requests
    ALTER COLUMN profile_url DROP NOT NULL;
