/**
 * Studojo LinkedIn Connect — background service worker.
 * Reads LinkedIn session cookies using the cookies API (not accessible from content scripts).
 */
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== 'GET_LI_COOKIES') return;

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

  return true; // keep the message channel open for the async response
});
