/**
 * Studojo LinkedIn Connect — content script.
 * Runs on studojo.pro / studojo.com pages.
 *
 * Three jobs:
 *   1. Tell the page the extension is installed (STUDOJO_EXT_READY)
 *   2. Forward cookie requests from page → background worker
 *   3. Forward the user's JWT (from page localStorage) to the background worker
 *      so the background can authenticate against the Studojo server for polling.
 */

// Announce extension presence
window.dispatchEvent(new CustomEvent('STUDOJO_EXT_READY'));

// On-demand re-announce
window.addEventListener('STUDOJO_CHECK_EXT', () => {
  window.dispatchEvent(new CustomEvent('STUDOJO_EXT_READY'));
});

// Cookie bridge (existing behavior)
window.addEventListener('STUDOJO_REQUEST_LI_COOKIES', () => {
  chrome.runtime.sendMessage({ type: 'GET_LI_COOKIES' }, (response) => {
    const detail =
      chrome.runtime.lastError
        ? { error: 'Extension error: ' + chrome.runtime.lastError.message }
        : response || { error: 'No response from extension — try reloading the page.' };
    window.dispatchEvent(new CustomEvent('STUDOJO_LI_COOKIES', { detail }));
  });
});

// Hand the user's JWT to the background worker so it can poll on its behalf.
// We re-send on every page visit so token refreshes propagate.
function pushAuthToBackground() {
  try {
    const jwt = window.localStorage.getItem('token');
    if (!jwt) return;
    chrome.runtime.sendMessage({
      type: 'SET_STUDOJO_AUTH',
      jwt,
      // Use same-origin API base. Strip path to get origin + /job-outreach prefix.
      origin: window.location.origin + '/api/v1/outreach/linkedin/automation',
    });
  } catch (_) { /* ignore */ }
}

pushAuthToBackground();

// Re-push if the page sets a new token after login refresh
let lastJwt = '';
setInterval(() => {
  try {
    const jwt = window.localStorage.getItem('token') || '';
    if (jwt && jwt !== lastJwt) {
      lastJwt = jwt;
      pushAuthToBackground();
    }
  } catch (_) { /* ignore */ }
}, 5000);
