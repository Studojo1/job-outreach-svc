chrome.runtime.sendMessage({ type: 'GET_LI_COOKIES' }, (res) => {
  const el = document.getElementById('status');
  if (res && res.li_at) {
    el.textContent = '✓ LinkedIn session found. Go to Studojo to connect.';
    el.style.background = '#f0fdf4';
    el.style.color = '#166534';
    el.style.borderColor = '#bbf7d0';
  } else {
    el.textContent = res?.error || 'Not logged in to LinkedIn. Open linkedin.com and sign in.';
    el.style.background = '#fef9f0';
    el.style.color = '#92400e';
    el.style.borderColor = '#fed7aa';
  }
});
