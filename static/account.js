// Shared by every page: loads who is signed in (/me) and fills the top bar's account area.
// Pages that need the same details wait on window.accountReady.

window.accountReady = fetch('/me')
  .then(res => (res.ok ? res.json() : null))
  .catch(() => null);

function keptFor(hours) {
  if (hours >= 48) return `${Math.round(hours / 24)} days`;
  return hours === 1 ? '1 hour' : `${hours} hours`;
}

function navItem(tag, props, text) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.textContent = text;
  return node;
}

async function deleteAccount() {
  const ok = confirm('Delete your account? This removes every file and result saved to it, and cannot be undone.');
  if (!ok) return;
  const res = await fetch('/account', { method: 'DELETE' });
  if (res.ok) window.location.href = '/app';
  else alert('Your account could not be deleted. Please try again.');
}

window.accountReady.then(me => {
  const slot = document.getElementById('account-nav');
  if (!slot || !me) return;

  const items = [navItem('a', { href: '/files', className: 'nav-link', id: 'nav-files' }, 'My files')];

  if (me.accounts && !me.signed_in) {
    const next = encodeURIComponent(location.pathname + location.search);
    items.push(navItem('a', { href: `/sign-in?next=${next}`, className: 'nav-link primary', id: 'nav-sign-in' }, 'Sign in'));
  }

  if (me.accounts && me.signed_in) {
    const menu = document.createElement('details');
    menu.className = 'account-menu';
    menu.id = 'account-menu';
    // The email comes from the account, so it only ever goes in as text.
    menu.append(navItem('summary', { className: 'nav-link' }, me.email));

    const panel = document.createElement('div');
    panel.className = 'account-menu-panel';
    const plan = me.plan === 'paid' ? 'Paid plan' : 'Free plan';
    const usage = me.run_limit != null ? ` · ${me.runs_this_month} of ${me.run_limit} runs used this month` : '';
    panel.append(navItem('div', { className: 'menu-note' }, plan + usage));

    const signOut = document.createElement('form');
    signOut.method = 'post';
    signOut.action = '/auth/sign-out';
    signOut.append(navItem('button', { type: 'submit' }, 'Sign out'));
    panel.append(signOut);
    panel.append(navItem('button', { type: 'button', className: 'danger', onclick: deleteAccount }, 'Delete my account'));

    menu.append(panel);
    items.push(menu);
    document.addEventListener('click', e => { if (!menu.contains(e.target)) menu.open = false; });
  }

  slot.replaceChildren(...items);
});
