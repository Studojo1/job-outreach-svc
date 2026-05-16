/**
 * Studojo LinkedIn Connect — background service worker.
 *
 * Two jobs:
 *   1. Cookie reader: when the connect page asks (GET_LI_COOKIES), return the
 *      user's LinkedIn li_at + JSESSIONID so the page can hand them to the server.
 *   2. Task runner: poll the Studojo server for pending connection requests, then
 *      run each one inside this browser (user's IP, user's session cookies) by
 *      opening the profile in a background tab and clicking Connect → Send.
 */

const API_BASES = [
  'https://studojo.pro/api/v1/outreach/linkedin/automation',
  'https://studojo.com/api/v1/outreach/linkedin/automation',
];
const POLL_PERIOD_MIN = 0.5;        // 30s
const TASK_TIMEOUT_MS = 60_000;     // hard cap per send

// ── State ────────────────────────────────────────────────────────────────────
let inflight = false;
const seenTabs = new Set();

// ── Bootstrap: restore JWT + API base from storage ───────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create('studojo-poll', { periodInMinutes: POLL_PERIOD_MIN });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create('studojo-poll', { periodInMinutes: POLL_PERIOD_MIN });
});
chrome.alarms.create('studojo-poll', { periodInMinutes: POLL_PERIOD_MIN });

// ── Messaging ────────────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'GET_LI_COOKIES') {
    Promise.all([
      chrome.cookies.get({ url: 'https://www.linkedin.com', name: 'li_at' }),
      chrome.cookies.get({ url: 'https://www.linkedin.com', name: 'JSESSIONID' }),
    ])
      .then(([liAt, jsessionid]) => {
        if (!liAt?.value) {
          sendResponse({
            error: 'Not logged in to LinkedIn. Please visit linkedin.com and log in first, then try again.',
          });
          return;
        }
        sendResponse({ li_at: liAt.value, jsessionid: jsessionid?.value || '' });
      })
      .catch((err) => sendResponse({ error: 'Could not read LinkedIn cookies: ' + err.message }));
    return true;
  }

  if (message.type === 'SET_STUDOJO_AUTH') {
    chrome.storage.local.set({
      studojo_jwt: message.jwt || '',
      studojo_origin: message.origin || '',
    });
    sendResponse({ ok: true });
    return;
  }

  if (message.type === 'PING') {
    sendResponse({ ok: true, version: '2.0.0' });
    return;
  }
});

// ── Polling loop ─────────────────────────────────────────────────────────────
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== 'studojo-poll') return;
  if (inflight) return;
  inflight = true;
  try {
    await pollOnce();
  } catch (e) {
    console.warn('Studojo poll error:', e);
  } finally {
    inflight = false;
  }
});

async function pollOnce() {
  const { studojo_jwt, studojo_origin } = await chrome.storage.local.get([
    'studojo_jwt', 'studojo_origin',
  ]);
  if (!studojo_jwt) return;  // user hasn't opened the connect page since extension install

  const base = studojo_origin || API_BASES[0];
  const url = `${base.replace(/\/$/, '')}/extension/next-task`;
  let res;
  try {
    res = await fetch(url, {
      headers: { 'Authorization': `Bearer ${studojo_jwt}` },
    });
  } catch (e) {
    return;  // network error — try again next tick
  }
  if (res.status === 401 || res.status === 403) {
    // JWT expired — clear it. User needs to refresh studojo.pro tab.
    await chrome.storage.local.remove('studojo_jwt');
    return;
  }
  if (!res.ok) return;

  let data;
  try { data = await res.json(); } catch (_) { return; }
  if (!data?.task) return;

  await runTask(data.task, base, studojo_jwt);
}

// ── Task runner ──────────────────────────────────────────────────────────────
async function runTask(task, base, jwt) {
  const url = task.profile_url;
  const taskId = task.task_id;
  if (!url || !taskId) return;

  let tabId = null;
  let success = false;
  let errorMsg = null;

  try {
    const tab = await chrome.tabs.create({ url, active: false });
    tabId = tab.id;
    seenTabs.add(tabId);

    await waitForTabLoad(tabId, 30_000);
    // Let LinkedIn React settle
    await sleep(3500);

    const [result] = await Promise.race([
      chrome.scripting.executeScript({
        target: { tabId },
        func: sendInviteOnPage,
      }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('script timeout')), TASK_TIMEOUT_MS)),
    ]);

    const r = result?.result || {};
    success = !!r.ok;
    if (!success) errorMsg = r.error || 'unknown';
  } catch (e) {
    errorMsg = (e && e.message) ? e.message : String(e);
  } finally {
    if (tabId != null) {
      try { await chrome.tabs.remove(tabId); } catch (_) { /* ignore */ }
      seenTabs.delete(tabId);
    }
  }

  try {
    await fetch(`${base.replace(/\/$/, '')}/extension/task-result`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${jwt}`,
      },
      body: JSON.stringify({ task_id: taskId, success, error: errorMsg }),
    });
  } catch (_) { /* best effort */ }
}

function waitForTabLoad(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(handler);
      reject(new Error('tab load timeout'));
    }, timeoutMs);
    const handler = (id, info) => {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(handler);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(handler);
  });
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ── Page-side script (injected into LinkedIn profile tab) ────────────────────
// Returns { ok: boolean, error?: string }
async function sendInviteOnPage() {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (b) => b && b.offsetParent !== null;

  const findConnectButton = () => {
    // 1) Direct Connect button in the profile action bar
    const direct = [...document.querySelectorAll('button')].find((b) =>
      visible(b) &&
      ((b.getAttribute('aria-label') || '').trim() === 'Connect' ||
       (b.getAttribute('aria-label') || '').toLowerCase().includes('invite') ||
       (b.innerText || '').trim().toLowerCase() === 'connect')
    );
    if (direct) return { type: 'direct', btn: direct };

    // 2) More dropdown — find the visible More button on the profile card
    const more = [...document.querySelectorAll('button')].find((b) =>
      visible(b) &&
      ((b.getAttribute('aria-label') || '').trim() === 'More actions' ||
       (b.getAttribute('aria-label') || '').toLowerCase().startsWith('more'))
    );
    if (more) return { type: 'more', btn: more };
    return null;
  };

  // Detect "already pending" — Connect button replaced with Pending
  const alreadyPending = [...document.querySelectorAll('button')].find((b) =>
    visible(b) &&
    ((b.getAttribute('aria-label') || '').toLowerCase().includes('pending') ||
     (b.innerText || '').trim().toLowerCase() === 'pending')
  );
  if (alreadyPending) return { ok: true, alreadyPending: true };

  // Detect "Message" only (1st-degree already) — treat as success no-op
  const msgOnly = [...document.querySelectorAll('button')].find((b) =>
    visible(b) && (b.getAttribute('aria-label') || '').toLowerCase().startsWith('message ')
  );

  let attempt = findConnectButton();
  if (!attempt && msgOnly) return { ok: true, alreadyConnected: true };
  if (!attempt) {
    await wait(2500);
    attempt = findConnectButton();
  }
  if (!attempt) return { ok: false, error: 'Connect button not found' };

  if (attempt.type === 'more') {
    attempt.btn.click();
    await wait(900);
    // Find "Connect" menuitem inside the dropdown
    const items = [...document.querySelectorAll('[role="menuitem"], li a, li button, div[role="button"]')];
    const connectItem = items.find((el) =>
      visible(el) && ((el.innerText || '').trim().toLowerCase() === 'connect' ||
                      (el.getAttribute('aria-label') || '').toLowerCase() === 'connect')
    );
    if (!connectItem) return { ok: false, error: 'Connect menuitem not found' };
    connectItem.click();
  } else {
    attempt.btn.click();
  }

  // Wait for the invite modal to open
  let modalReady = false;
  for (let i = 0; i < 20; i++) {
    await wait(400);
    const sendBtn = [...document.querySelectorAll('button')].find((b) =>
      visible(b) &&
      ((b.getAttribute('aria-label') || '').trim() === 'Send without a note' ||
       (b.innerText || '').trim() === 'Send without a note' ||
       (b.getAttribute('aria-label') || '').trim() === 'Send now')
    );
    if (sendBtn) { modalReady = true; break; }
  }
  if (!modalReady) return { ok: false, error: 'invite modal did not open' };

  // Click "Send without a note" — never the generic "Send" (messaging overlay).
  const inviteLabels = ['Send without a note', 'Send now'];
  let clicked = null;
  for (const lbl of inviteLabels) {
    const b = [...document.querySelectorAll('button')].find((x) =>
      visible(x) && ((x.getAttribute('aria-label') || '').trim() === lbl ||
                     (x.innerText || '').trim() === lbl)
    );
    if (b) { b.click(); clicked = lbl; break; }
  }
  if (!clicked) return { ok: false, error: 'send button not found in modal' };

  // Give LinkedIn a moment to fire the API call
  await wait(2500);
  return { ok: true, clicked };
}
