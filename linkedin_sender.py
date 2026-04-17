#!/usr/bin/env python3
"""
LinkedIn Message Sender — runs locally, uses Chrome TLS impersonation
so LinkedIn can't fingerprint it as a bot.

  .venv/bin/python linkedin_sender.py
  (or double-click "Start LinkedIn Sender.command")

Then open http://localhost:5050 in your browser.
"""
import json, re, os, time, random
from flask import Flask, request, jsonify, render_template_string
from curl_cffi import requests as cffi

app = Flask(__name__)
CREDS_FILE = os.path.join(os.path.dirname(__file__), ".li_creds.json")
VOYAGER = "https://www.linkedin.com/voyager/api"

# ── Voyager helpers ───────────────────────────────────────────────────────────

def _headers(li_at, jsessionid):
    jsessionid = jsessionid.strip().strip('"')   # strip any accidental quotes
    return {
        "Cookie": f'li_at={li_at}; JSESSIONID="{jsessionid}"',
        "csrf-token": jsessionid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-track": json.dumps({
            "clientVersion": "1.13.1862", "mpVersion": "1.13.1862",
            "osName": "web", "timezoneOffset": 0, "timezone": "America/New_York",
            "deviceFormFactor": "DESKTOP", "mpName": "voyager-web",
            "displayDensity": 1, "displayWidth": 1920, "displayHeight": 1080,
        }),
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/feed/",
        "Origin": "https://www.linkedin.com",
    }

def _slug(url):
    url = (url or "").strip().rstrip("/")
    if "/in/" in url:
        return url.split("/in/")[-1].split("/")[0].split("?")[0]
    return url.split("/")[-1].split("?")[0]

def _session(li_at, jsessionid):
    """curl_cffi session that looks exactly like Chrome 124."""
    s = cffi.Session(impersonate="chrome124")
    s.headers.update(_headers(li_at, jsessionid))
    return s

def resolve_urns(s, slug):
    """Return (member_id, fsd_id). Tries HTML parse then dash API."""
    # Strategy 1: profile HTML
    try:
        r = s.get(f"https://www.linkedin.com/in/{slug}/",
                  headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                  timeout=20)
        if r.status_code == 200:
            html = r.text
            m = re.search(r'"objectUrn"\s*:\s*"urn:li:member:(\d+)"', html) \
                or re.search(r'urn:li:member:(\d+)', html)
            member_id = m.group(1) if m else None
            f = re.search(r'urn:li:fsd_profile:([A-Za-z0-9_-]{10,})', html)
            fsd_id = f.group(1) if f else None
            if member_id or fsd_id:
                return member_id, fsd_id
        else:
            return None, None, f"Profile page returned {r.status_code} — check your li_at cookie"
    except Exception as e:
        return None, None, f"Network error reaching LinkedIn: {e}"

    # Strategy 2: dash profiles API
    try:
        r = s.get(f"{VOYAGER}/identity/dash/profiles",
                  params={"q": "memberIdentity", "memberIdentity": slug},
                  timeout=20)
        if r.status_code == 200:
            data = r.json()
            entities = data.get("included", []) + (
                data.get("data", {})
                    .get("identityDashProfilesByMemberIdentity", {})
                    .get("elements", [])
            )
            member_id = fsd_id = None
            for e in entities:
                combined = (e.get("objectUrn", "") + " " + e.get("entityUrn", ""))
                mm = re.search(r'member:(\d+)', combined)
                if mm and not member_id: member_id = mm.group(1)
                ff = re.search(r'fsd_profile:([A-Za-z0-9_-]{10,})', combined)
                if ff and not fsd_id: fsd_id = ff.group(1)
            if member_id or fsd_id:
                return member_id, fsd_id
    except Exception:
        pass

    return None, None, f"Could not resolve '{slug}' — double-check the profile URL"

def send_message(li_at, jsessionid, profile_url, content):
    time.sleep(random.uniform(1.0, 2.0))
    slug = _slug(profile_url)
    s = _session(li_at, jsessionid)

    result = resolve_urns(s, slug)
    if len(result) == 3:
        return False, result[2]
    member_id, fsd_id = result

    # Messaging only reliably accepts urn:li:member:{numericId}
    recipients = []
    if member_id:
        recipients.append(f"urn:li:member:{member_id}")
    if fsd_id and not member_id:
        recipients.append(f"urn:li:fsd_profile:{fsd_id}")
    if not recipients:
        return False, f"No URN found for '{slug}' — check the profile URL"

    last_err = ""
    for recipient in recipients:
        payload = {
            "keyVersion": "LEGACY_INBOX",
            "conversationCreate": {
                "eventCreate": {"value": {
                    "com.linkedin.voyager.messaging.create.MessageCreate": {
                        "attributedBody": {"text": content, "attributes": []},
                        "attachments": [],
                    }
                }},
                "recipients": [recipient],
                "subtype": "MEMBER_TO_MEMBER",
            },
        }
        r = s.post(f"{VOYAGER}/messaging/conversations",
                   json=payload,
                   headers={"Content-Type": "application/json"},
                   timeout=20)
        if r.status_code in (200, 201):
            return True, "Sent!"
        last_err = f"LinkedIn {r.status_code}: {r.text[:300]}"

    return False, last_err or "All attempts failed"

# ── Web UI ────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>LinkedIn Sender</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f3f4f6; min-height: 100vh; display: flex;
         align-items: center; justify-content: center; padding: 24px; }
  .card { background: white; border-radius: 12px; padding: 32px;
          width: 100%; max-width: 540px; box-shadow: 0 4px 24px rgba(0,0,0,.08); }
  h1 { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  .sub { font-size: 13px; color: #6b7280; margin-bottom: 24px; }
  label { display: block; font-size: 13px; font-weight: 600;
          color: #374151; margin-bottom: 4px; margin-top: 16px; }
  input, textarea { width: 100%; border: 1.5px solid #d1d5db; border-radius: 8px;
                    padding: 10px 12px; font-size: 14px; font-family: inherit;
                    transition: border-color .15s; }
  input:focus, textarea:focus { outline: none; border-color: #0a66c2; }
  textarea { resize: vertical; min-height: 120px; }
  .cookie-box { background: #f9fafb; border: 1.5px dashed #d1d5db;
                border-radius: 8px; padding: 14px; margin-top: 8px; }
  .cookie-box summary { font-size: 13px; font-weight: 600; cursor: pointer; color: #374151; }
  .cookie-box p { font-size: 12px; color: #6b7280; line-height: 1.6; margin-top: 6px; }
  .cookie-box ol { font-size: 12px; color: #6b7280; line-height: 1.8;
                   padding-left: 16px; margin-top: 6px; }
  .cookie-box code { background: #e5e7eb; padding: 1px 5px; border-radius: 3px;
                     font-family: monospace; font-size: 11px; }
  .save-row { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
  .save-row input[type=checkbox] { width: auto; }
  .save-row label { margin: 0; font-weight: 400; color: #6b7280; font-size: 12px; }
  button { margin-top: 20px; width: 100%; padding: 12px;
           background: #0a66c2; color: white; border: none;
           border-radius: 8px; font-size: 15px; font-weight: 600;
           cursor: pointer; transition: background .15s; }
  button:hover { background: #004182; }
  button:disabled { background: #9ca3af; cursor: not-allowed; }
  .result { margin-top: 16px; padding: 12px 14px; border-radius: 8px;
            font-size: 14px; display: none; }
  .result.ok  { background: #dcfce7; color: #166534; }
  .result.err { background: #fee2e2; color: #991b1b; word-break: break-word; }
  .saved { font-size: 11px; color: #059669; font-weight: 600; margin-left: 6px; }
</style>
</head>
<body>
<div class="card">
  <h1>LinkedIn Message Sender</h1>
  <p class="sub">Runs locally from your IP — no bans, no blocks.</p>

  <details class="cookie-box" {% if not saved %}open{% endif %}>
    <summary>🍪 LinkedIn cookies {% if saved %}<span class="saved">(saved ✓)</span>{% endif %}</summary>
    <p>Open <b>linkedin.com</b> → press <b>F12</b> → <b>Application</b> tab → <b>Cookies</b> → <code>www.linkedin.com</code></p>
    <ol>
      <li>Find <code>li_at</code> — copy its value and paste below</li>
      <li>Find <code>JSESSIONID</code> — copy its value <b>without</b> the surrounding quotes</li>
    </ol>

    <label>li_at</label>
    <input id="li_at" type="password" placeholder="AQE..." value="{{ li_at }}" autocomplete="off">

    <label>JSESSIONID <span style="font-weight:400;color:#9ca3af">(no quotes — just: ajax:12345…)</span></label>
    <input id="jsessionid" type="text" placeholder="ajax:123456789012345" value="{{ jsessionid }}" autocomplete="off">

    <div class="save-row">
      <input type="checkbox" id="save_creds" {% if saved %}checked{% endif %}>
      <label for="save_creds">Remember on this machine (stored locally, never sent anywhere)</label>
    </div>
  </details>

  <label>LinkedIn profile URL</label>
  <input id="profile_url" type="text" placeholder="https://www.linkedin.com/in/john-doe/">

  <label>Message</label>
  <textarea id="content" placeholder="Hi John…"></textarea>

  <button id="btn" onclick="send()">Send Message</button>
  <div id="result" class="result"></div>
</div>

<script>
async function send() {
  const btn = document.getElementById("btn");
  const res = document.getElementById("result");
  const li_at      = document.getElementById("li_at").value.trim();
  const jsessionid = document.getElementById("jsessionid").value.trim().replace(/^"|"$/g, "");
  const profile_url = document.getElementById("profile_url").value.trim();
  const content    = document.getElementById("content").value.trim();
  const save       = document.getElementById("save_creds").checked;

  if (!li_at || !jsessionid) return show("err", "Paste your li_at and JSESSIONID cookies first.");
  if (!profile_url)          return show("err", "Enter a LinkedIn profile URL.");
  if (!content)              return show("err", "Write a message first.");

  btn.disabled = true; btn.textContent = "Sending…"; res.style.display = "none";

  try {
    const r = await fetch("/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ li_at, jsessionid, profile_url, content, save }),
    });
    const data = await r.json();
    if (data.ok) show("ok", "✓ Message sent!");
    else         show("err", "Error: " + data.error);
  } catch (e) {
    show("err", "Request failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Send Message";
  }
}

function show(type, msg) {
  const el = document.getElementById("result");
  el.className = "result " + type;
  el.textContent = msg;
  el.style.display = "block";
}
</script>
</body>
</html>"""

@app.route("/")
def index():
    creds = {}
    saved = False
    if os.path.exists(CREDS_FILE):
        try:
            creds = json.load(open(CREDS_FILE))
            saved = True
        except Exception:
            pass
    return render_template_string(HTML,
        li_at=creds.get("li_at", ""),
        jsessionid=creds.get("jsessionid", ""),
        saved=saved)

@app.route("/send", methods=["POST"])
def send():
    data = request.get_json()
    li_at       = (data.get("li_at") or "").strip()
    jsessionid  = (data.get("jsessionid") or "").strip().strip('"')
    profile_url = (data.get("profile_url") or "").strip()
    content     = (data.get("content") or "").strip()

    if data.get("save") and li_at and jsessionid:
        try:
            json.dump({"li_at": li_at, "jsessionid": jsessionid}, open(CREDS_FILE, "w"))
        except Exception:
            pass

    ok, msg = send_message(li_at, jsessionid, profile_url, content)
    return jsonify({"ok": ok, "error": msg if not ok else None})

if __name__ == "__main__":
    print("\n LinkedIn Sender is running")
    print(" Open this in your browser → http://localhost:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=False)
