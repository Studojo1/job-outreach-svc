"""PP-P43: support tickets unanswered for 24h must alert the founders.

Loads the real alert script out of k8s/ops-alerts/ticket-unanswered-alert.yaml
(the ConfigMap the CronJob mounts) and runs its logic; psycopg2 / ACS are only
imported inside main(), so nothing here touches a database or sends mail.
"""
import importlib.util
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "ops-alerts" / "ticket-unanswered-alert.yaml"
NOW = datetime(2026, 9, 29, 10, 20, tzinfo=timezone.utc)


def _script_source() -> str:
    lines = MANIFEST.read_text().splitlines()
    start = lines.index("  alert.py: |") + 1
    body = []
    for line in lines[start:]:
        if line.startswith("---"):
            break
        body.append(line[4:])
    return "\n".join(body)


def _load():
    """Import the ConfigMap's alert.py as a module (not as __main__)."""
    path = pathlib.Path(tempfile.mkdtemp()) / "ticket_alert.py"
    path.write_text(_script_source())
    spec = importlib.util.spec_from_file_location("ticket_alert_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return vars(mod)


def _ticket(tid, since, **kw):
    return {"id": tid, "since": since, "user_email": f"s{tid}@x.com", "category": "bug",
            "priority": "normal", "status": "open", "source": "support_chat", "preview": "help", **kw}


def test_manifest_is_a_cronjob_wired_like_the_other_alerts():
    text = MANIFEST.read_text()
    assert "kind: CronJob" in text and "name: ticket-unanswered-alert-script" in text
    assert "key: database-url" in text and "key: acs-email-connection-string" in text
    assert 'schedule: "0 * * * *"' in text


def test_waiting_since_follows_who_spoke_last():
    ws = _load()["waiting_since"]
    t0 = NOW - timedelta(days=5)
    # Never answered: waiting since the ticket was raised (ticket 38/39).
    assert ws(t0, [("user", t0)]) == t0
    assert ws(t0, []) == t0
    # Admin replied last: nobody is waiting.
    assert ws(t0, [("user", t0), ("admin", t0 + timedelta(hours=3))]) is None
    # Student wrote again after the reply: the clock restarts there (ticket 30).
    again = t0 + timedelta(days=2)
    assert ws(t0, [("user", t0), ("admin", t0 + timedelta(hours=3)), ("user", again),
                   ("user", again + timedelta(hours=1))]) == again
    # System notes neither start nor stop the clock.
    assert ws(t0, [("user", t0), ("system", t0 + timedelta(hours=1))]) == t0


def test_pick_flags_24h_waits_and_marks_the_ones_that_just_crossed():
    pick = _load()["pick"]
    hour = NOW.replace(minute=0)
    old = _ticket(1, hour - timedelta(days=5))
    just = _ticket(2, hour - timedelta(hours=24, minutes=30))
    young = _ticket(3, hour - timedelta(hours=23))
    answered = _ticket(4, None)
    overdue, fresh = pick([young, just, answered, old], NOW, 24)
    assert [t["id"] for t in overdue] == [1, 2]
    assert [t["id"] for t in fresh] == [2]


def test_consecutive_hourly_runs_announce_each_wait_exactly_once():
    pick = _load()["pick"]
    t = _ticket(7, datetime(2026, 9, 28, 9, 59, 59, tzinfo=timezone.utc))
    hits = []
    for h in range(8, 13):
        now = datetime(2026, 9, 29, h, 0, 12, tzinfo=timezone.utc)
        hits += [now.hour for x in pick([t], now, 24)[1]]
    assert hits == [10]


def test_sends_on_new_crossings_and_once_a_day_as_a_digest():
    ns = _load()
    send = ns["should_send"]
    t = _ticket(1, NOW - timedelta(days=3))
    assert send([t], [t], NOW, 3)
    assert not send([t], [], NOW, 3)
    assert send([t], [], NOW.replace(hour=3), 3)
    assert not send([], [], NOW.replace(hour=3), 3)


def test_email_lists_every_waiting_ticket_with_a_reply_link():
    render = _load()["render"]
    a = _ticket(39, NOW - timedelta(hours=30), preview="<b>no leads</b>")
    b = _ticket(30, NOW - timedelta(hours=26))
    subject, text, html = render([a, b], [b], NOW)
    assert "2 ticket(s) unanswered for 24h+" in subject and "(1 new)" in subject
    assert "#39: waiting 30h" in text and "#30 NEW: waiting 26h" in text
    assert "https://admin.studojo.com/tickets/39" in text
    assert "&lt;b&gt;no leads&lt;/b&gt;" in html


def test_load_tickets_reads_open_and_in_progress_and_computes_the_wait():
    load = _load()["load_tickets"]
    t0 = NOW - timedelta(days=2)

    class Cur:
        def __init__(self):
            self.sql = []
            self.results = [
                [(True,)],
                [(39, "s@x.com", "bug", "high", "open", "support_chat", t0, "My   campaign\nstopped")],
                [(39, "user", t0), (39, "system", t0 + timedelta(minutes=1))],
            ]

        def execute(self, q, params=None):
            self.sql.append(q)

        def fetchone(self):
            return self.results.pop(0)[0]

        def fetchall(self):
            return self.results.pop(0)

    cur = Cur()
    [t] = load(cur)
    assert "status IN ('open', 'in_progress')" in cur.sql[1]
    assert t["since"] == t0 and t["preview"] == "My campaign stopped"
