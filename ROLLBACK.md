# Rolling back the extension send endpoint

Everything this feature adds is additive. Nothing existing is modified, so
reverting is removing, never restoring.

## Fastest kill switch (no deploy)

Set the daily cap to zero. Every send returns 429 with a clear message and
nothing is charged or written.

```
kubectl set env deployment/job-outreach-svc EXTENSION_SEND_DISABLED=1 -n staging
```

Checked before anything else in the handler: nothing is looked up, nothing is
charged, nothing is written. Students see "Sending is paused right now. Your
draft is saved." Unset the variable to resume.

Deliberately read per-request rather than at import, so it takes effect on the
next call instead of needing a restart — a deploy is slow exactly when you most
need the feature stopped.

## Full revert

```bash
git revert <sha-of-feat/extension-send-one>   # or: reset the branch
```

Removes `api/routes_extension.py` and the two lines in `api/main.py`. The
route stops existing and returns 404. Campaign, payment, enrichment and Gmail
code are untouched by this feature, so nothing else changes.

## What stays behind in the database, and why it is harmless

The endpoint only ever INSERTs. It never deletes, and after the reuse-scoping
fix it never writes to a row another feature owns.

| Table | Rows created | After revert |
|---|---|---|
| `leads` | one per contact emailed, `status` prefixed `extension_` | inert; campaign queries filter by their own statuses |
| `emails_sent` | one per email sent, `campaign_id IS NULL` | inert; campaign queries all join on a campaign |
| `user_credits` | `used_credits` incremented by 1 per email | a real charge for a real email — correctly kept |

Nothing above is orphaned or dangling: every row points at a real lead and a
real candidate.

### If you want the rows gone too

Only ever run this having confirmed the count first.

```sql
-- Look before deleting.
SELECT count(*) FROM emails_sent WHERE campaign_id IS NULL;
SELECT count(*) FROM leads WHERE status LIKE 'extension_%';

-- Then, in this order (emails_sent references leads).
DELETE FROM emails_sent WHERE campaign_id IS NULL;
DELETE FROM leads       WHERE status LIKE 'extension_%';
```

Credits are deliberately not refunded here: each one paid for an email that
actually left a student's mailbox. Refund individually if a specific send was
faulty.

## What is NOT reversible

**Emails already sent.** Once Gmail accepts a message it is gone. This is why
the endpoint sends exactly one email per explicit button press, never in a
loop or a queue, and why `DAILY_SEND_CAP` exists.

**Apollo lookups already paid for.** Each resolved address cost one Apollo
credit. The resolved email is committed immediately so a retry does not buy it
twice — meaning a revert keeps the value you already paid for.
