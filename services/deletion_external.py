"""The parts of account deletion that live outside our database.

Privacy Policy v2.0 §15: deleting an account deletes the resume files, and
"we also ask our analytics providers to delete your data". Each call here is
best effort, runs after the database deletion has committed, and reports what
it did. One that is not configured is skipped with a warning naming the
missing settings, so a gap shows in the logs instead of passing silently.

Files (Azure Blob Storage, the account the frontend and control-plane upload to):
  resumes/application-uploads/<user>/...     resume uploads (frontend)
  humanizer-temp/<user id>/...               control-plane uploads
  ticket-screenshots/<ts>-<rand>-<user>.<ext> ticket screenshots (frontend)
  and every blob URL on our account found in the user's rows (cp.jobs
  payload/result, application_resume_uploads.url, ...), collected before
  those rows are deleted.
"""

import logging
import re
from typing import Iterable, Optional
from urllib.parse import unquote

import requests

from core.config import settings

logger = logging.getLogger(__name__)

BLOB_URL_RE = re.compile(r"https://([a-z0-9]+)\.blob\.core\.windows\.net/([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/([^\s\"'?<>\\]+)")
HUMANIZER_TEMP_CONTAINER = "humanizer-temp"
TIMEOUT = (5, 20)


def safe_user(user_id: str) -> str:
    """The frontend's path segment for a user (blob-storage.server.ts)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)[:40]


def blob_refs(values: Iterable[object], account: Optional[str] = None) -> set[tuple[str, str]]:
    """(container, blob name) for every blob URL on our account in values."""
    account = account or settings.AZURE_STORAGE_ACCOUNT_NAME
    out = set()
    for v in values:
        if v is None:
            continue
        for acct, container, name in BLOB_URL_RE.findall(str(v)):
            if account and acct != account:
                continue
            out.add((container, unquote(name)))
    return out


def _blob_service():
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential={"account_name": settings.AZURE_STORAGE_ACCOUNT_NAME,
                    "account_key": settings.AZURE_STORAGE_ACCOUNT_KEY},
    )


def delete_user_blobs(user_id: str, refs: Iterable[tuple[str, str]] = ()) -> dict:
    if not (settings.AZURE_STORAGE_ACCOUNT_NAME and settings.AZURE_STORAGE_ACCOUNT_KEY):
        logger.warning("[ACCOUNT_DELETE] resume files NOT deleted for %s: set AZURE_STORAGE_ACCOUNT_NAME "
                       "and AZURE_STORAGE_ACCOUNT_KEY", user_id)
        return {"status": "skipped", "missing": ["AZURE_STORAGE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT_KEY"]}

    from azure.core.exceptions import ResourceNotFoundError

    svc = _blob_service()
    su = safe_user(user_id)
    targets = set(refs)
    errors = 0

    def by_prefix(container: str, prefix: str) -> None:
        nonlocal errors
        try:
            for b in svc.get_container_client(container).list_blobs(name_starts_with=prefix):
                targets.add((container, b.name))
        except ResourceNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            errors += 1
            logger.warning("[ACCOUNT_DELETE] listing %s/%s failed: %s", container, prefix, e)

    by_prefix(settings.AZURE_STORAGE_CONTAINER_NAME, f"application-uploads/{su}/")
    by_prefix(HUMANIZER_TEMP_CONTAINER, f"{user_id}/")
    try:
        tickets = svc.get_container_client(settings.AZURE_STORAGE_TICKETS_CONTAINER)
        suffix = re.compile(rf"-{re.escape(su)}\.[a-z0-9]{{1,10}}$")
        for b in tickets.list_blobs():
            if suffix.search(b.name):
                targets.add((settings.AZURE_STORAGE_TICKETS_CONTAINER, b.name))
    except ResourceNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        errors += 1
        logger.warning("[ACCOUNT_DELETE] listing ticket screenshots failed: %s", e)

    deleted = 0
    for container, name in sorted(targets):
        try:
            svc.get_container_client(container).delete_blob(name, delete_snapshots="include")
            deleted += 1
        except ResourceNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            errors += 1
            logger.warning("[ACCOUNT_DELETE] deleting blob %s/%s failed: %s", container, name, e)
    return {"status": "ok" if not errors else "partial", "deleted": deleted, "errors": errors}


def _posthog_api_host() -> str:
    if settings.POSTHOG_API_HOST:
        return settings.POSTHOG_API_HOST.rstrip("/")
    return (settings.POSTHOG_HOST or "https://eu.i.posthog.com").rstrip("/").replace(".i.posthog.com", ".posthog.com")


def delete_posthog_person(user_id: str) -> dict:
    if not (settings.POSTHOG_PERSONAL_API_KEY and settings.POSTHOG_PROJECT_ID):
        logger.warning("[ACCOUNT_DELETE] PostHog data NOT deleted for %s: set POSTHOG_PERSONAL_API_KEY "
                       "and POSTHOG_PROJECT_ID", user_id)
        return {"status": "skipped", "missing": ["POSTHOG_PERSONAL_API_KEY", "POSTHOG_PROJECT_ID"]}
    base = f"{_posthog_api_host()}/api/projects/{settings.POSTHOG_PROJECT_ID}/persons"
    headers = {"Authorization": f"Bearer {settings.POSTHOG_PERSONAL_API_KEY}"}
    try:
        r = requests.get(f"{base}/", params={"distinct_id": user_id}, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        people = [p["id"] for p in r.json().get("results", []) if p.get("id")]
        deleted = 0
        for pid in people:
            d = requests.delete(f"{base}/{pid}/", params={"delete_events": "true"}, headers=headers, timeout=TIMEOUT)
            if d.status_code in (200, 202, 204, 404):
                deleted += 1
            else:
                logger.warning("[ACCOUNT_DELETE] PostHog delete %s answered %s", pid, d.status_code)
        return {"status": "ok" if deleted == len(people) else "partial", "deleted": deleted}
    except requests.RequestException as e:
        logger.warning("[ACCOUNT_DELETE] PostHog deletion failed for %s: %s", user_id, e)
        return {"status": "error"}


def delete_mixpanel_user(user_id: str) -> dict:
    if not (settings.MIXPANEL_PROJECT_TOKEN and settings.MIXPANEL_GDPR_TOKEN):
        logger.warning("[ACCOUNT_DELETE] Mixpanel data NOT deleted for %s: set MIXPANEL_PROJECT_TOKEN "
                       "and MIXPANEL_GDPR_TOKEN", user_id)
        return {"status": "skipped", "missing": ["MIXPANEL_PROJECT_TOKEN", "MIXPANEL_GDPR_TOKEN"]}
    try:
        r = requests.post(
            f"{settings.MIXPANEL_API_HOST.rstrip('/')}/api/app/data-deletions/v3.0/",
            params={"token": settings.MIXPANEL_PROJECT_TOKEN},
            headers={"Authorization": f"Bearer {settings.MIXPANEL_GDPR_TOKEN}"},
            json={"distinct_ids": [user_id], "compliance_type": "GDPR"},
            timeout=TIMEOUT,
        )
        if r.ok:
            task = (r.json().get("results") or {}).get("task_id")
            return {"status": "requested", "task_id": task}
        logger.warning("[ACCOUNT_DELETE] Mixpanel deletion answered %s: %s", r.status_code, r.text[:200])
        return {"status": "error", "http": r.status_code}
    except requests.RequestException as e:
        logger.warning("[ACCOUNT_DELETE] Mixpanel deletion failed for %s: %s", user_id, e)
        return {"status": "error"}


def delete_external(user_id: str, refs: Iterable[tuple[str, str]] = ()) -> dict:
    out = {}
    for key, fn in (("blobs", lambda: delete_user_blobs(user_id, refs)),
                    ("posthog", lambda: delete_posthog_person(user_id)),
                    ("mixpanel", lambda: delete_mixpanel_user(user_id))):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 - never undo a committed deletion
            logger.exception("[ACCOUNT_DELETE] %s step failed for %s", key, user_id)
            out[key] = {"status": "error", "error": str(e)[:200]}
    return out
