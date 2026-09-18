// Listen for requests from the Studojo page
window.addEventListener('STUDOJO_REQUEST_LI_COOKIES', () => {
  chrome.runtime.sendMessage({ type: 'GET_LI_COOKIES' }, (response) => {
    if (chrome.runtime.lastError) {
      window.dispatchEvent(new CustomEvent('STUDOJO_LI_COOKIES', {
        detail: { error: 'Extension error: ' + chrome.runtime.lastError.message },
      }));
      return;
    }
    window.dispatchEvent(new CustomEvent('STUDOJO_LI_COOKIES', { detail: response }));
  });
});

// Signal to the page that the extension is installed
window.dispatchEvent(new CustomEvent('STUDOJO_EXT_READY'));
