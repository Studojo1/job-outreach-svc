"""Guards for k8s/ops-alerts, which the deploy workflows now kubectl-apply.

Audit IN-N07 / IN-N08 (2026-09-30):
- the Mesa daily-scrape CronJob must send the internal auth header, or the
  run-due endpoint 401s and no saved search is scraped;
- no file may declare a Secret, because applying it would overwrite the
  hand-made live secret (ops-alert-webhook) with empty values;
- no container may run a moving ':latest' (or untagged) image.
"""

from pathlib import Path

import yaml

OPS_DIR = Path(__file__).resolve().parent.parent / "k8s" / "ops-alerts"


def _docs():
    for path in sorted(OPS_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                yield path.name, doc


def _containers(doc):
    spec = doc.get("spec", {})
    if doc.get("kind") == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    pod = spec.get("template", {}).get("spec", {})
    return pod.get("containers", []) + pod.get("initContainers", [])


def test_ops_alerts_dir_is_not_empty():
    assert list(_docs()), "k8s/ops-alerts has no manifests"


def test_no_secret_objects_are_applied():
    offenders = [name for name, doc in _docs() if doc.get("kind") == "Secret"]
    assert not offenders, f"Secret declared in {offenders}; it would wipe the live one on deploy"


def test_every_image_is_pinned():
    for name, doc in _docs():
        for c in _containers(doc):
            image = c["image"]
            pinned = "@sha256:" in image or (
                ":" in image.rsplit("/", 1)[-1] and not image.endswith(":latest")
            )
            assert pinned, f"{name}: container {c['name']} runs unpinned image {image}"


def test_mesa_daily_scrape_sends_internal_auth_header():
    docs = [doc for name, doc in _docs() if doc.get("metadata", {}).get("name") == "mesa-daily-scrape"]
    assert len(docs) == 1
    (container,) = _containers(docs[0])
    command = " ".join(container.get("args", []) + container.get("command", []))
    assert '-H "x-studojo-internal: $INTERNAL_API_SECRET"' in command
    env = {e["name"]: e for e in container.get("env", [])}
    ref = env["INTERNAL_API_SECRET"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "app-secrets", "key": "INTERNAL_API_SECRET"}
