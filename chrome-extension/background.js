chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== 'GET_LI_COOKIES') return;

  Promise.all([
    chrome.cookies.get({ url: 'https://www.linkedin.com', name: 'li_at' }),
    chrome.cookies.get({ url: 'https://www.linkedin.com', name: 'JSESSIONID' }),
  ]).then(([liAt, jsessionid]) => {
    if (!liAt) {
      sendResponse({ error: 'Not logged in to LinkedIn. Please open linkedin.com and sign in first.' });
      return;
    }
    sendResponse({
      li_at: liAt.value,
      jsessionid: jsessionid ? jsessionid.value.replace(/^"|"$/g, '') : '',
    });
  });

  return true; // keep channel open for async response
});
