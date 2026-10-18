// Turns Taggy's Markdown replies into tidy, safe page elements.
// Elements are built with createElement + textContent only, never innerHTML, so a reply can't inject markup.
// Also produces plain text for the scratchpad and for reading aloud.
(function (root) {
  'use strict';

  const RE = {
    fence: /^\s{0,3}(```|~~~)\s*([\w+-]*)\s*$/,
    heading: /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/,
    hr: /^\s{0,3}([-*_])(\s*\1){2,}\s*$/,
    bullet: /^(\s*)[-*+•]\s+(.*)$/,
    ordered: /^(\s*)(\d{1,3})[.)]\s+(.*)$/,
    quote: /^\s*>\s?(.*)$/,
    tableRow: /^\s*\|.*\|\s*$/,
    tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
  };

  const cells = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  const isTableStart = (lines, i) => RE.tableRow.test(lines[i]) && i + 1 < lines.length && RE.tableSep.test(lines[i + 1]);
  const startsBlock = (lines, i) => {
    const l = lines[i];
    return RE.fence.test(l) || RE.heading.test(l) || RE.hr.test(l) || RE.bullet.test(l) ||
      RE.ordered.test(l) || RE.quote.test(l) || isTableStart(lines, i);
  };

  /** Split Markdown into blocks: heading, p, ul, ol, quote, code, table, hr. */
  function parse(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = line.match(RE.fence))) {
        const marker = m[1], code = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(marker)) code.push(lines[i++]);
        i++; // closing fence (or end of reply)
        blocks.push({ type: 'code', text: code.join('\n') });
        continue;
      }
      if ((m = line.match(RE.heading))) { blocks.push({ type: 'heading', level: m[1].length, text: m[2] }); i++; continue; }
      if (RE.hr.test(line)) { blocks.push({ type: 'hr' }); i++; continue; }
      if (isTableStart(lines, i)) {
        const head = cells(line), rows = [];
        i += 2;
        while (i < lines.length && RE.tableRow.test(lines[i])) rows.push(cells(lines[i++]));
        blocks.push({ type: 'table', head, rows });
        continue;
      }
      if (RE.quote.test(line)) {
        const quoted = [];
        while (i < lines.length && RE.quote.test(lines[i])) quoted.push(lines[i++].match(RE.quote)[1]);
        blocks.push({ type: 'quote', text: quoted.join(' ') });
        continue;
      }
      if (RE.bullet.test(line) || RE.ordered.test(line)) {
        const ordered = !RE.bullet.test(line);
        const start = ordered ? Number(line.match(RE.ordered)[2]) : 1;
        const items = [];
        while (i < lines.length) {
          const l = lines[i], b = l.match(RE.bullet), o = l.match(RE.ordered);
          if (ordered ? o : b) { items.push(ordered ? o[3] : b[2]); i++; continue; }
          if ((b || o) && /^\s{2,}/.test(l)) { items.push(b ? b[2] : o[3]); i++; continue; } // nested item, kept flat
          if (l.trim() && /^\s{2,}/.test(l) && items.length) { items[items.length - 1] += ' ' + l.trim(); i++; continue; }
          break;
        }
        blocks.push({ type: ordered ? 'ol' : 'ul', start, items });
        continue;
      }
      const para = [line.trim()];
      i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) para.push(lines[i++].trim());
      blocks.push({ type: 'p', lines: para });
    }
    return blocks;
  }

  const INLINE = /(`+)([^`]+?)\1|\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*(?!\s)([^*\n]+?)\*|(?<![\w])_(?!\s)([^_\n]+?)_(?![\w])/;

  /** Inline tokens: text, code, strong, em, del, link. */
  function inline(text) {
    const tokens = [];
    let rest = String(text || '');
    while (rest) {
      const m = rest.match(INLINE);
      if (!m) { tokens.push({ t: 'text', v: rest }); break; }
      if (m.index) tokens.push({ t: 'text', v: rest.slice(0, m.index) });
      if (m[2] !== undefined) tokens.push({ t: 'code', v: m[2] });
      else if (m[3] !== undefined || m[4] !== undefined) tokens.push({ t: 'strong', c: inline(m[3] ?? m[4]) });
      else if (m[5] !== undefined) tokens.push({ t: 'del', c: inline(m[5]) });
      else if (m[6] !== undefined) tokens.push({ t: 'link', c: inline(m[6]), href: m[7] });
      else tokens.push({ t: 'em', c: inline(m[8] ?? m[9]) });
      rest = rest.slice(m.index + m[0].length);
    }
    return tokens;
  }

  const inlinePlain = text => inline(text).map(function flat(tok) {
    return tok.c ? tok.c.map(flat).join('') : tok.v;
  }).join('');

  /** Readable plain text. forSpeech drops list markers and symbols a voice would read out. */
  function plain(src, { forSpeech = false } = {}) {
    return parse(src).map(b => {
      switch (b.type) {
        case 'heading': return inlinePlain(b.text) + (forSpeech ? '.' : '');
        case 'p': return b.lines.map(inlinePlain).join(' ');
        case 'ul': return b.items.map(it => (forSpeech ? '' : '• ') + inlinePlain(it)).join(forSpeech ? '. ' : '\n');
        case 'ol': return b.items.map((it, n) => (forSpeech ? '' : `${b.start + n}. `) + inlinePlain(it)).join(forSpeech ? '. ' : '\n');
        case 'quote': return inlinePlain(b.text);
        case 'code': return forSpeech ? '' : b.text;
        case 'table': return [b.head, ...b.rows].map(r => r.map(inlinePlain).join(forSpeech ? ', ' : ' | ')).join(forSpeech ? '. ' : '\n');
        default: return '';
      }
    }).filter(Boolean).join(forSpeech ? ' ' : '\n\n').replace(/[ \t]+/g, ' ').trim();
  }

  function safeHref(href) {
    try { const u = new URL(href); return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null; }
    catch { return null; }
  }

  function renderInline(tokens, doc, parent) {
    for (const tok of tokens) {
      if (tok.t === 'text') { parent.append(doc.createTextNode(tok.v)); continue; }
      if (tok.t === 'code') { const c = doc.createElement('code'); c.textContent = tok.v; parent.append(c); continue; }
      if (tok.t === 'link') {
        const href = safeHref(tok.href);
        if (!href) { renderInline(tok.c, doc, parent); continue; }
        const a = doc.createElement('a');
        a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer';
        renderInline(tok.c, doc, a); parent.append(a); continue;
      }
      const el = doc.createElement(tok.t === 'strong' ? 'strong' : tok.t === 'del' ? 'del' : 'em');
      renderInline(tok.c, doc, el); parent.append(el);
    }
  }

  /** Build a DocumentFragment for a reply. */
  function render(src, doc = root.document) {
    const frag = doc.createDocumentFragment();
    const make = (tag, cls) => { const e = doc.createElement(tag); if (cls) e.className = cls; return e; };
    for (const b of parse(src)) {
      let el;
      if (b.type === 'heading') { el = make(b.level <= 3 ? 'h4' : 'h5'); renderInline(inline(b.text), doc, el); }
      else if (b.type === 'p') {
        el = make('p');
        b.lines.forEach((line, n) => { if (n) el.append(make('br')); renderInline(inline(line), doc, el); });
      }
      else if (b.type === 'ul' || b.type === 'ol') {
        el = make(b.type);
        if (b.type === 'ol' && b.start !== 1) el.start = b.start;
        for (const item of b.items) { const li = make('li'); renderInline(inline(item), doc, li); el.append(li); }
      }
      else if (b.type === 'quote') { el = make('blockquote'); renderInline(inline(b.text), doc, el); }
      else if (b.type === 'code') { el = make('pre'); const c = make('code'); c.textContent = b.text; el.append(c); }
      else if (b.type === 'hr') { el = make('hr'); }
      else if (b.type === 'table') {
        el = make('div', 'md-table');
        const table = make('table'), thead = make('thead'), tbody = make('tbody'), headRow = make('tr');
        b.head.forEach(h => { const th = make('th'); renderInline(inline(h), doc, th); headRow.append(th); });
        thead.append(headRow);
        b.rows.forEach(r => {
          const tr = make('tr');
          b.head.forEach((_, n) => { const td = make('td'); renderInline(inline(r[n] || ''), doc, td); tr.append(td); });
          tbody.append(tr);
        });
        table.append(thead, tbody); el.append(table);
      }
      if (el) frag.append(el);
    }
    return frag;
  }

  const api = { parse, inline, plain, render };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TaggyFormat = api;
})(typeof window !== 'undefined' ? window : globalThis);
