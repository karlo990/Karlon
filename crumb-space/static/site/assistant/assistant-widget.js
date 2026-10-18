// Website assistant chat widget. Add to any page with:
//   <link rel="stylesheet" href="/assistant/assistant-widget.css">
//   <script src="/assistant/taggy-format.js" defer></script>
//   <script src="/assistant/assistant-widget.js" defer></script>
// It reads its name, greeting and suggestions from GET /api/assistant and stays hidden if that fails.
// When the settings include a Turnstile site key (online), it gets a chat session first; see ensureSession.
// Built with createElement/textContent only; replies are rendered by TaggyFormat (no innerHTML).
// The conversation lives in memory only: nothing is stored in the browser.
(() => {
  'use strict';
  const MAX_SENT = 12, MAX_CHARS = 1000;
  const el = (tag, props = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key in node && typeof value !== 'string') node[key] = value;
      else node.setAttribute(key, value);
    }
    node.append(...children);
    return node;
  };
  const safeHttps = href => { try { const u = new URL(href); return u.protocol === 'https:' ? u.href : null; } catch { return null; } };

  async function start() {
    let settings;
    try {
      const response = await fetch('/api/assistant');
      if (!response.ok) return;
      settings = await response.json();
    } catch { return; }

    const history = [];
    let busy = false;
    const launch = el('button', { class: 'sa-launch', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'sa-panel' },
      el('span', { class: 'sa-launch-dot', 'aria-hidden': 'true' }), `Ask ${settings.business}`);
    const close = el('button', { class: 'sa-close', type: 'button', 'aria-label': 'Close chat', text: '×' });
    const log = el('div', { class: 'sa-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
    const status = el('p', { class: 'sa-status', role: 'status' });
    const input = el('textarea', { id: 'sa-input', rows: '2', maxlength: String(MAX_CHARS), placeholder: 'Ask a question…' });
    const send = el('button', { class: 'sa-send', type: 'submit', text: 'Send' });
    const form = el('form', { class: 'sa-form' }, el('label', { class: 'sa-sr', for: 'sa-input', text: `Your question for ${settings.name}` }), input, send);
    const suggestions = el('div', { class: 'sa-suggestions', role: 'group', 'aria-label': 'Suggested questions' });
    for (const question of settings.suggestions || []) {
      const chip = el('button', { class: 'sa-chip', type: 'button', text: question });
      chip.addEventListener('click', () => ask(question));
      suggestions.append(chip);
    }
    const footer = el('div', { class: 'sa-footer' }, el('p', { class: 'sa-notice', text: settings.notice }));
    const handoff = settings.handoff && safeHttps(settings.handoff.href);
    if (handoff) footer.append(el('a', { class: 'sa-handoff', href: handoff, target: '_blank', rel: 'noopener noreferrer', text: settings.handoff.label }));
    const human = el('div', { class: 'sa-human' });
    const panel = el('section', { id: 'sa-panel', class: 'sa-panel', hidden: true, 'aria-labelledby': 'sa-title' },
      el('header', { class: 'sa-head' }, el('h2', { id: 'sa-title', text: `Ask ${settings.name}` }), close),
      log, suggestions, status, human, form, footer);
    const root = el('div', { class: 'sa-root' }, panel, launch);
    for (const name of ['accent', 'ink', 'paper', 'soft']) {
      const colour = settings.theme?.[name];
      if (/^#[0-9a-f]{6}$/i.test(colour || '')) root.style.setProperty(`--sa-${name}`, colour);
    }
    document.body.append(root);

    function bubble(role, text) {
      const body = el('div', { class: 'sa-text' });
      if (role === 'assistant' && window.TaggyFormat) body.append(window.TaggyFormat.render(text));
      else body.textContent = text;
      const item = el('div', { class: `sa-msg sa-${role}` }, el('span', { class: 'sa-sr', text: role === 'user' ? 'You said:' : `${settings.name} said:` }), body);
      log.append(item);
      log.scrollTop = log.scrollHeight;
      return item;
    }
    bubble('assistant', settings.greeting);
    const setBusy = value => { busy = value; send.disabled = value || !settings.ready; input.disabled = !settings.ready; };
    setBusy(false);
    if (!settings.ready) status.textContent = 'The chat isn’t switched on yet. Please try again later.';

    // Online, the server wants a short session first: a Turnstile check (usually invisible) swapped for a
    // cookie by POST /api/session. Tokens are single-use, so the widget is reset before each new check.
    // The Turnstile script only loads once the visitor opens the chat. Locally there is no site key.
    let session = null, turnstileId = null, waiting = null;
    const loadTurnstile = () => window.turnstile ? Promise.resolve() : new Promise((resolve, reject) => {
      const script = el('script', { src: 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit', async: true });
      script.onload = resolve; script.onerror = reject;
      document.head.append(script);
    });
    const turnstileToken = () => new Promise((resolve, reject) => {
      waiting = { resolve, reject };
      if (turnstileId === null) {
        turnstileId = window.turnstile.render(human, {
          sitekey: settings.turnstileSiteKey, action: 'assistant', appearance: 'interaction-only',
          callback: token => { waiting?.resolve(token); waiting = null; },
          'error-callback': () => { waiting?.reject(Error()); waiting = null; return true; },
        });
      } else window.turnstile.reset(turnstileId);
      const mine = waiting;
      setTimeout(() => { if (waiting === mine) { waiting.reject(Error()); waiting = null; } }, 60000);
    });
    function ensureSession() {
      if (!settings.turnstileSiteKey) return Promise.resolve();
      session ||= (async () => {
        await loadTurnstile();
        const token = await turnstileToken();
        const response = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) });
        if (!response.ok) throw Error((await response.json().catch(() => ({}))).error);
      })().catch(error => { session = null; throw Error(error.message || 'We couldn’t check that you’re a person. Please try again.'); });
      return session;
    }

    async function ask(question) {
      question = question.trim().slice(0, MAX_CHARS);
      if (!question || busy || !settings.ready) return;
      suggestions.hidden = true;
      history.push({ role: 'user', content: question });
      bubble('user', question);
      input.value = '';
      setBusy(true);
      status.textContent = `${settings.name} is thinking…`;
      try {
        const post = async () => {
          await ensureSession();
          return fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages: history.slice(-MAX_SENT) }) });
        };
        let response = await post();
        let data = await response.json().catch(() => ({}));
        if (response.status === 401 && data.code === 'session' && settings.turnstileSiteKey) {
          session = null;
          response = await post();
          data = await response.json().catch(() => ({}));
        }
        if (!response.ok || !data.text) throw Error(data.error || 'The chat couldn’t answer just now. Please try again.');
        history.push({ role: 'assistant', content: data.text });
        bubble('assistant', data.text);
        status.textContent = '';
      } catch (error) {
        history.pop();
        status.textContent = error.message;
      } finally {
        setBusy(false);
        input.focus();
      }
    }

    const toggle = open => {
      panel.hidden = !open;
      launch.setAttribute('aria-expanded', String(open));
      root.classList.toggle('sa-open', open);
      (open ? input : launch).focus();
      if (open && settings.ready) ensureSession().catch(error => { status.textContent = error.message; });
    };
    launch.addEventListener('click', () => toggle(panel.hidden));
    close.addEventListener('click', () => toggle(false));
    panel.addEventListener('keydown', event => { if (event.key === 'Escape') toggle(false); });
    form.addEventListener('submit', event => { event.preventDefault(); ask(input.value); });
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask(input.value); }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
