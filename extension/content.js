/**
 * Studojo LinkedIn Connect — content script.
 * Runs on studojo.pro/lkot and studojo.com pages.
 * Bridges page ↔ background service worker for LinkedIn cookie access.
 */

// Tell the page the extension is installed
window.dispatchEvent(new CustomEvent('STUDOJO_EXT_READY'));

// Also respond to on-demand checks (e.g. user switches to Extension tab)
window.addEventListener('STUDOJO_CHECK_EXT', () => {
  window.dispatchEvent(new CustomEvent('STUDOJO_EXT_READY'));
});

// When the page asks for cookies, fetch them via the background worker
window.addEventListener('STUDOJO_REQUEST_LI_COOKIES', () => {
  chrome.runtime.sendMessage({ type: 'GET_LI_COOKIES' }, (response) => {
    const detail =
      chrome.runtime.lastError
        ? { error: 'Extension error: ' + chrome.runtime.lastError.message }
        : response || { error: 'No response from extension — try reloading the page.' };

    window.dispatchEvent(new CustomEvent('STUDOJO_LI_COOKIES', { detail }));
  });
});
