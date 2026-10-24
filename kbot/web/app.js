// kbot control center. No framework and no build step: hash routes, one page module per screen, polling.
// Every value that comes from the server goes through esc() (the html`` tag does it), so market data and
// log text can never become markup.
'use strict';
(() => {
  const MINUS = '−';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
  const raw = (s) => new Raw(s);
  const show = (v) => (v instanceof Raw ? v.s : Array.isArray(v) ? v.map(show).join('') : esc(v));
  const html = (str, ...vals) => raw(str.reduce((out, s, i) => out + s + (i < vals.length ? show(vals[i]) : ''), ''));
  const DASH = raw('<span class="faint">–</span>');   // missing or not applicable

  // --- numbers: tabular mono, a sign on every change, a true minus ---------------------------------
  const nf = (d) => new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const sgn = (v) => (v > 0 ? '+' : v < 0 ? MINUS : '');
  const usd = (v, signed = true, d = 2) => (v == null ? '–' : (signed ? sgn(v) : v < 0 ? MINUS : '') + '$' + nf(d).format(Math.abs(v)));
  const money = (v, signed = true) => (v == null ? DASH : html`<span class="num">${usd(v, signed)}</span>`);
  // polarity colour only where the column's job is "what moved": P&L
  const pnl = (v) => (v == null ? DASH : html`<span class="num ${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${usd(v)}</span>`);
  const cents = (v) => (v == null ? DASH : html`<span class="num">${sgn(v) + nf(2).format(Math.abs(v * 100))}¢</span>`);
  // contract prices in cents, as Kalshi shows them; one decimal covers its 0.1¢ ticks
  const price = (v, d = 1) => (v == null ? DASH : html`<span class="num">${nf(d).format(v * 100)}¢</span>`);
  const level = (v) => (v == null ? '–' : nf(Math.abs(v) < 10 ? 4 : 2).format(v));   // strikes and spot
  const pct = (v, d = 1) => (v == null ? DASH : html`<span class="num">${nf(d).format(v * 100)}%</span>`);
  const int = (v) => (v == null ? DASH : html`<span class="num">${nf(0).format(v)}</span>`);
  const ci = (c, f = cents) => (c && c[0] != null ? html`<span class="sub">95% CI ${f(c[0])} to ${f(c[1])}</span>` : '');
  const pad2 = (n) => String(n).padStart(2, '0');
  const clock = (ts) => { const d = new Date(ts * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; };
  const when = (ts) => {
    if (!ts) return '–';
    const d = new Date(ts * 1000);
    return d.toDateString() === new Date().toDateString() ? clock(ts)
      : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + clock(ts).slice(0, 5);
  };
  const day = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + clock(ts).slice(0, 5) : '–');
  const ago = (s) => (s == null ? '–' : s < 90 ? `${Math.max(0, Math.round(s))} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`);
  const left = (s) => (s <= 0 ? 'closed' : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)}:${pad2(Math.floor(s % 60))}`);
  const short = (series) => String(series || '').replace(/^KX/, '').replace(/15M$/, '');

  // --- server --------------------------------------------------------------------------------------
  const S = { csrf: null, auth: false, state: null, lastOk: null, page: null, timer: null, strat: null, stratAt: 0 };
  async function api(path, body) {
    const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': S.csrf || '' }, body: JSON.stringify(body) };
    let r;
    try { r = await fetch(path, { ...opt, credentials: 'same-origin', cache: 'no-store' }); } catch {
      throw Object.assign(new Error('The control server is not answering.'), { offline: true });
    }
    const data = await r.json().catch(() => ({}));
    if (r.status === 401 && path !== '/api/login') { S.csrf = null; showLogin(); throw Object.assign(new Error('log in first'), { auth: true }); }
    if (!r.ok) throw Object.assign(new Error(data.error || `HTTP ${r.status}`), { status: r.status, data });
    return data;
  }

  // --- shell ----------------------------------------------------------------------------------------
  const view = $('#view');
  view.innerHTML = '<div id="conn" class="notice neg" role="status" hidden></div><div id="page"></div><div id="toast" role="status" hidden></div>';
  const page = $('#page');
  const STATES = { running: ['pos', 'Running'], paused: ['warn', 'Paused'], disconnected: ['neg', 'Disconnected'], stopped: ['', 'Stopped'], starting: ['', 'Starting'] };
  const statePill = (b) => {
    const [cls, label] = b.estop ? ['neg', 'Emergency stop'] : STATES[b.state] || ['', b.state];
    return html`<span class="pill ${cls}"><span class="dot"></span>${label}</span>`;
  };
  function paintShell() {
    const st = S.state;
    const pill = statePill(st.bot);
    $('#side-state').outerHTML = show(pill).replace('class="pill', 'id="side-state" class="pill');
    $('#topbar-state').outerHTML = show(pill).replace('class="pill', 'id="topbar-state" class="pill');
    const armed = st.production_trading !== 'disabled';
    $('#side-prod').className = 'pill' + (armed ? ' neg' : '');
    $('#side-prod').textContent = armed ? 'ARMED' : 'Off';
    $('#scope').textContent = `${st.mode === 'paper' ? 'Paper trading' : st.mode} · ${st.config}`;
  }
  function setConn(ok, msg) {
    const el = $('#conn');
    el.hidden = ok;
    if (!ok) el.innerHTML = show(html`<b>Disconnected.</b> ${msg} Showing data from ${S.lastOk ? clock(S.lastOk / 1000) : 'never'}. Retrying every few seconds.`);
  }
  let toastTimer;
  function toast(msg, bad = false) {
    const el = $('#toast');
    el.hidden = false;
    el.className = 'notice' + (bad ? ' neg' : '');
    el.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:30;max-width:420px;margin:0;box-shadow:0 12px 28px rgba(0,0,0,.4)';
    el.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 9000 : 5000);
  }
  function ask({ title, text, ok, danger = false, input = null, required = false }) {
    const d = $('#confirm');
    $('#confirm-title').textContent = title;
    $('#confirm-text').innerHTML = show(text);
    $('#confirm-field').hidden = !input;
    $('#confirm-label').textContent = input || '';
    const box = $('#confirm-input');
    box.value = '';
    box.required = !!(input && required);
    const b = $('#confirm-ok');
    b.textContent = ok;
    b.className = 'btn ' + (danger ? 'danger solid' : 'primary');
    d.returnValue = '';
    d.showModal();
    (input ? box : b).focus();
    return new Promise((res) => d.addEventListener('close', () => res({ ok: d.returnValue === 'ok', value: box.value.trim() }), { once: true }));
  }
  async function control(action, extra = {}) {
    try {
      const r = await api('/api/control', { action, ...extra });
      toast(r.message);
    } catch (e) { if (!e.auth) toast(e.message, true); }
    tick();
  }
  async function resumeFlow(estopped) {
    const r = await ask({
      title: estopped ? 'Clear the emergency stop and resume?' : 'Resume new entries?',
      text: estopped ? 'Entries start again on the next tick. Say what you checked; it goes in the log.' : 'Say what you checked; it goes in the log. Limits are not changed.',
      ok: 'Resume', input: 'What you checked', required: true,
    });
    if (r.ok) control('resume', { reviewed: r.value, clear_estop: estopped });
  }
  async function stopFlow() {
    const r = await ask({ title: 'Stop the paper bot?', text: 'The process shuts down cleanly: resting paper bids are cancelled and open positions stay on the books and settle when their markets close. You can start it again from here.', ok: 'Stop bot', danger: true });
    if (r.ok) control('stop', { confirm: true });
  }
  async function estopFlow() {
    const r = await ask({ title: 'Engage the emergency stop?', text: 'No new orders of any kind. Open entry orders are cancelled on the next tick. Positions are not closed; they settle normally. It stays on, even after a restart, until you clear it here or with the CLI.', ok: 'Emergency stop', danger: true, input: 'Reason (optional)' });
    if (r.ok) control('estop', { confirm: true, reason: r.value || 'emergency stop from the web page' });
  }
  // the screen's one primary action, chosen from the bot's state
  function primaryAction(b) {
    if (b.estop) return html`<a class="btn danger" href="#/controls">Review emergency stop</a>`;
    if (b.state === 'stopped' && !b.service) return html`<button class="btn primary" data-act="start">Start paper bot</button>`;
    if (b.state === 'paused') return html`<button class="btn primary" data-act="resume">Resume</button>`;
    if (b.state === 'running') return html`<button class="btn" data-act="pause">Pause entries</button>`;
    if (b.state === 'disconnected') return html`<a class="btn" href="#/controls">Check health</a>`;
    return html`<button class="btn" disabled>Starting…</button>`;
  }
  document.addEventListener('click', (ev) => {
    const a = ev.target.closest('[data-act]');
    if (!a || a.disabled) return;
    const act = a.dataset.act;
    if (act === 'start') control('start');
    else if (act === 'pause') control('pause', { reason: 'paused from the web page' });
    else if (act === 'resume') resumeFlow(!!S.state?.bot.estop);
    else if (act === 'stop') stopFlow();
    else if (act === 'estop') estopFlow();
    else if (act === 'decision') openDecision(a.dataset.id);
  });

  // --- shared pieces --------------------------------------------------------------------------------
  function windowTrack(m, now, cutoff) {
    const span = (m.close_ts - m.open_ts) || 900, done = Math.min(1, Math.max(0, (now - m.open_ts) / span));
    return html`<div class="window" title="${m.ticker}: opens ${clock(m.open_ts)}, closes ${clock(m.close_ts)}; no new entries in the last ${cutoff} s">
      <div class="track"><div class="done" style="width:${(done * 100).toFixed(1)}%"></div><div class="lock" style="width:${(cutoff / span * 100).toFixed(1)}%"></div><div class="now" style="left:${(done * 100).toFixed(1)}%"></div></div>
      <span class="left">${left(m.close_ts - now)}</span></div>`;
  }
  function verdictPill(v) {
    if (!v) return DASH;
    const cls = { loses: 'neg', 'no-edge': 'warn', unproven: 'warn', thin: '', none: '' }[v.key] || '';
    return html`<span class="pill ${cls}" title="${v.text}">${v.short || v.text}</span>`;
  }
  const EV = {
    settlement: (d, t) => `Settled ${t}: ${String(d.side).toUpperCase()} ${d.result === d.side ? 'won' : 'lost'}`,
    startup: (d) => `Bot started (${d.strategy}, $${d.bankroll} bankroll)`,
    shutdown: (d) => `Bot stopped. ${d.open_positions} open position${d.open_positions === 1 ? '' : 's'} left to settle`,
    pause: (d) => `Paused: ${d.reason}`, estop: (d) => `Emergency stop: ${d.reason}`,
    risk_trip: (d) => `Risk limit hit: ${d.reason}`, risk_clear: (d) => `Resumed after review: ${d.by}`,
    error: (d) => `Error: ${d.error}`, notify: (d) => d.text, web_start: () => 'Start requested from this page',
    stop_requested: () => 'Stop requested', unexpected_termination: () => 'The previous run did not shut down cleanly',
    settings_changed: (d) => `Settings changed: ${Object.keys(d.changes || {}).join(', ')}`,
    cancel_open_entries: (d) => `Cancelled ${d.count} open order${d.count === 1 ? '' : 's'}: ${d.reason}`,
    entry_abandoned: (d, t) => `Entry abandoned on ${t}: ${d.reason}`,
    validation_viewed: (d) => `Backtest read the validation split (${d.strategy})`,
  };
  const evText = (e) => { let d = {}; try { d = JSON.parse(e.data || '{}'); } catch { /* keep {} */ } return [(EV[e.kind] || (() => e.kind.replaceAll('_', ' ') + (e.ticker ? ` ${e.ticker}` : '')))(d, e.ticker), d]; };
  function drawLine(el, curve, start, key) {
    if (el.dataset.key === key) return;            // redraw only when the data changed
    el.dataset.key = key;
    const pts = [[null, start], ...(curve.length ? curve : [[Date.now() / 1000, start]])];
    const labels = pts.map((p, i) => (i === 0 ? 'Start' : curve.length ? when(p[0]) : 'Now'));
    const values = pts.map((p) => p[1]);
    let lo = Math.min(...values), hi = Math.max(...values);
    if (hi - lo < 1) { const mid = (hi + lo) / 2; lo = mid - 0.5; hi = mid + 0.5; }
    Charts.line(el, { labels, series: [{ name: 'Equity', values }], format: (v) => usd(v, false), height: 180, min: lo, max: hi,
      reference: { value: start, label: `Start ${usd(start, false, 0)}` } });
  }
  const emptyRow = (cols, text) => html`<tr><td colspan="${cols}"><div class="empty">${text}</div></td></tr>`;

  // --- pages ----------------------------------------------------------------------------------------
  const PAGES = {};

  PAGES.overview = {
    title: 'Overview',
    mount() {
      page.innerHTML = show(html`
      <div class="title-row"><div><h1>Overview</h1><p class="sub" id="ov-finding"><span class="skeleton">Loading the bot's state</span></p></div>
        <div class="actions"><span class="stamp" id="ov-stamp"></span><span id="ov-action"></span></div></div>
      <div class="grid stretch">
        <section class="panel s8"><header><h2>Paper account</h2><span class="meta" id="ov-label"></span></header>
          <div class="lead" id="ov-net"><span class="skeleton">$0.00</span></div><div class="lead-sub" id="ov-net-sub">&nbsp;</div>
          <div class="kpis four" id="ov-kpis"></div>
          <div class="chart" id="ov-chart"></div></section>
        <section class="panel s4" style="display:flex;flex-direction:column"><header><h2>Bot</h2><a class="card-link" href="#/controls">Controls</a></header>
          <div id="ov-bot"></div><div class="btn-row" id="ov-bot-foot" style="margin-top:auto;padding-top:16px"></div></section>
      </div>
      <div class="grid">
        <section class="panel s7"><header><h2>Open 15-minute windows</h2><a class="card-link" href="#/markets">Live markets</a></header><div class="windows" id="ov-windows"></div></section>
        <section class="panel s5"><header><h2>Recent activity</h2></header><ul class="rows" id="ov-activity"></ul></section>
      </div>
      <div class="grid">
        <section class="panel s7"><header><h2>Recent decisions</h2><a class="card-link" href="#/trades">All decisions</a></header><ul class="rows" id="ov-decisions"></ul></section>
        <section class="panel s5"><header><h2>Recent simulated fills</h2><span class="meta">Paper</span></header><ul class="rows" id="ov-fills"></ul></section>
      </div>`);
    },
    async refresh() {
      const st = S.state, p = st.pnl, b = st.bot;
      if (Date.now() - S.stratAt > 30000) { S.stratAt = Date.now(); api('/api/strategies').then((r) => { S.strat = r; }).catch(() => {}); }
      const row = S.strat?.strategies.find((r) => r.configured);
      const ev = row && (row.paper_per_contract || row.paper || row.historical);
      const evidence = row && ev && ev.verdict ? ` ${row.name}${row.paper_per_contract ? ' research log' : ''}: ${ev.verdict.text.toLowerCase()}${row.paper_per_contract?.net != null ? `, ${sgn(ev.net)}${nf(2).format(Math.abs(ev.net * 100))}¢ per contract over ${ev.n} fills` : ''}.` : '';
      $('#ov-finding').textContent = (p.trades ? `Paper net ${usd(p.net)} after ${usd(p.fees, false)} in fees on ${p.trades} settled trade${p.trades === 1 ? '' : 's'}.` : 'No settled paper trades yet.') + evidence;
      $('#ov-stamp').textContent = `Updated ${clock(st.now)}`;
      $('#ov-action').innerHTML = show(primaryAction(b));
      $('#ov-label').textContent = p.label;
      $('#ov-net').innerHTML = show(pnl(p.net));
      $('#ov-net-sub').textContent = `Net after fees, ${p.trades} settled trade${p.trades === 1 ? '' : 's'} · $${st.bankroll} bankroll`;
      $('#ov-kpis').innerHTML = show([['Settled trades', int(p.trades)], ['Average per trade', p.trades ? cents(p.net / p.trades) : DASH], ['Gross', money(p.gross)], ['Fees', money(p.fees, false)], ['Today (UTC)', pnl(p.today)], ['Unrealized', money(p.unrealized)], ['Win rate', pct(p.win_rate)], ['Max drawdown', money(p.max_drawdown, false)]]
        .map(([k, v]) => html`<div class="kpi"><div class="kpi-label">${k}</div><div class="kpi-value">${v}</div></div>`));
      drawLine($('#ov-chart'), p.equity_curve, st.bankroll, JSON.stringify([p.equity_curve.length, p.equity_curve.at(-1), st.bankroll]));
      const h = b.health || {};
      $('#ov-bot').innerHTML = show(html`
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">${statePill(b)}</div>
        <p class="muted small" style="margin-bottom:10px">${b.detail}</p>
        <dl class="kv">
          <div><dt>Strategy</dt><dd class="mono">${st.strategy}</dd></div>
          <div><dt>Markets</dt><dd class="mono">${st.series.length ? st.series.map(short).join(' · ') : 'every 15M series'}</dd></div>
          <div><dt>Open positions</dt><dd>${st.positions.length ? html`${int(st.positions.length)} · ${money(st.exposure, false)} at risk` : 'None'}</dd></div>
          <div><dt>Heartbeat</dt><dd>${b.state === 'stopped' ? '–' : ago(b.heartbeat_age_s)}</dd></div>
          <div><dt>Last market data</dt><dd>${st.last_data_update ? ago(st.now - st.last_data_update) : '–'}</dd></div>
          <div><dt>Data feed</dt><dd>${b.state === 'stopped' ? '–' : h.ws ? 'WebSocket' : 'REST polling'}</dd></div>
          <div><dt>Running since</dt><dd>${b.started && b.state !== 'stopped' ? when(b.started) : '–'}</dd></div>
          <div><dt>Real-money trading</dt><dd>${st.production_trading === 'disabled' ? 'Disabled' : 'ARMED'}</dd></div>
        </dl>`);
      $('#ov-bot-foot').innerHTML = show(b.estop ? '' : html`<button class="btn danger" data-act="estop">Emergency stop</button>`);
      const ms = st.active_markets.slice(0, 6);
      $('#ov-windows').innerHTML = show(ms.length ? ms.map((m) => html`<div class="row"><div><b class="mono">${short(m.series)}</b> <span class="faint small mono">${level(m.floor_strike)}</span></div>${windowTrack(m, st.now, st.entry_cutoff_s)}<span class="num small" style="text-align:right">${clock(m.close_ts).slice(0, 5)}</span></div>`)
        : html`<div class="empty">${b.state === 'stopped' ? 'The bot is stopped, so no market windows are being read.' : 'No open windows in the database yet.'}</div>`);
      const acts = st.activity.slice(0, 6);
      $('#ov-activity').innerHTML = show(acts.length ? acts.map((e) => { const [t, d] = evText(e); return html`<li><span class="num faint small">${when(e.ts)}</span><span class="trunc" style="max-width:100%">${t}</span>${e.kind === 'settlement' ? pnl(d.pnl) : ''}</li>`; })
        : html`<li style="display:block"><div class="empty">Nothing has happened yet. Starts, stops, settlements and errors appear here.</div></li>`);
      const none = (text) => html`<li style="display:block"><div class="empty">${text}</div></li>`;
      $('#ov-decisions').innerHTML = show(st.recent_decisions.length ? st.recent_decisions.map((d) => html`<li class="click" data-act="decision" data-id="${d.id}"><span class="num faint small">${when(d.ts)}</span><span class="trunc" style="max-width:100%"><span class="mono">${short(d.ticker)}</span> ${d.outcome_side ? d.outcome_side.toUpperCase() : ''} · <span class="muted">${d.reason}</span></span><span class="pill ${d.accepted ? 'pos' : ''}">${d.accepted ? 'Taken' : 'Skipped'}</span></li>`)
        : none('No signals yet. Each market the strategy looks at shows here as taken or skipped, with the reason.'));
      $('#ov-fills').innerHTML = show(st.recent_fills.length ? st.recent_fills.map((f) => html`<li><span class="num faint small">${when(f.ts)}</span><span class="trunc" style="max-width:100%"><span class="mono">${short(f.ticker)}</span> ${f.outcome_side.toUpperCase()} ${int(f.count)} at ${price(f.price)}</span><span class="small faint">fee ${money(f.fee, false)}</span></li>`)
        : none('No simulated fills yet.'));
    },
  };

  PAGES.markets = {
    title: 'Live markets',
    mount() {
      page.innerHTML = show(html`
      <div class="title-row"><div><h1>Live markets</h1><p class="sub" id="mk-sub">Kalshi 15-minute markets the bot is reading.</p></div>
        <div class="actions"><span class="pill">Prices: real Kalshi data</span><span class="pill">Bot column: simulated paper trading</span></div></div>
      <div id="mk-notice"></div>
      <section class="panel"><div class="table-wrap"><table>
        <thead><tr><th>Market</th><th>Window</th><th class="n">Strike</th><th class="n">Spot (proxy)</th><th class="n">Yes bid</th><th class="n">Yes ask</th><th class="n">Spread</th><th class="n">Mid</th><th>Bot decision (simulated)</th></tr></thead>
        <tbody id="mk-rows">${emptyRow(9, 'Loading markets')}</tbody></table></div></section>`);
    },
    async refresh() {
      const r = await api('/api/markets'), st = S.state;
      const age = r.updated ? r.now - r.updated : null;
      $('#mk-sub').textContent = `${r.markets.length} open market${r.markets.length === 1 ? '' : 's'}. Last price update ${r.updated ? clock(r.updated) + ' (' + ago(age) + ')' : 'never'}. Spot is a Coinbase proxy, not the CF Benchmarks index that settles these markets.`;
      $('#mk-notice').innerHTML = show(age != null && age > r.stale_after_s
        ? html`<div class="notice"><b>Stale.</b> The newest price is ${ago(age)}. ${st.bot.state === 'stopped' ? 'The bot is stopped; start it to read live prices.' : 'The bot is not receiving market data; see Bot controls.'}</div>` : '');
      $('#mk-rows').innerHTML = show(r.markets.length ? r.markets.map((m) => {
        const b = m.book, stale = !b || r.now - b.ts > r.stale_after_s, mid = b && b.yes_bid != null && b.yes_ask != null ? (b.yes_bid + b.yes_ask) / 2 : null;
        const g = m.signal, q = m.quotes;
        const bot = q.length ? html`<span class="pill">Paper bids</span> <span class="mono small">${q.map((x) => `${x.side.toUpperCase()} ${nf(1).format(x.price * 100)}¢`).join(' · ')}</span>`
          : g ? html`<span class="pill ${g.accepted ? 'pos' : ''}">${g.accepted ? 'Taken' : 'Skipped'}</span> <span class="small muted trunc" title="${g.reason}">${g.strategy}: ${g.reason}</span>` : DASH;
        return html`<tr class="${g ? 'click' : ''}" ${raw(g ? `data-act="decision" data-id="${g.id}"` : '')}>
          <td><b class="mono">${m.ticker}</b><span class="sub">${m.series}</span></td>
          <td>${windowTrack(m, r.now, st.entry_cutoff_s)}</td>
          <td class="n num">${level(m.floor_strike)}</td><td class="n num">${level(b?.spot)}</td>
          <td class="n ${stale ? 'faint' : ''}">${price(b?.yes_bid)}</td><td class="n ${stale ? 'faint' : ''}">${price(b?.yes_ask)}</td>
          <td class="n">${b && b.yes_bid != null && b.yes_ask != null ? price(b.yes_ask - b.yes_bid) : DASH}</td><td class="n">${price(mid, 2)}${stale && b ? html`<span class="sub">${ago(r.now - b.ts)}</span>` : ''}</td>
          <td>${bot}</td></tr>`;
      }) : emptyRow(9, st.bot.state === 'stopped' ? 'No open markets. The bot records markets while it runs, and it is stopped.' : 'No open markets recorded yet. The bot finds them within a minute of starting.'));
    },
  };

  PAGES.trades = {
    title: 'Positions and trades', tab: 'decisions', view: 'all', q: '',
    mount() {
      const T = PAGES.trades;
      page.innerHTML = show(html`
      <div class="title-row"><div><h1>Positions and trades</h1><p class="sub">Simulated paper trading: fills come from the public trade tape and no order reaches Kalshi. Click a row to see why it was taken or skipped.</p></div></div>
      <div class="kpis boxed" id="tr-kpis"></div>
      <section class="panel" style="margin-top:24px"><header><h2>Open positions</h2><span class="meta">Marked at the bid on our side, from the bot's latest snapshot</span></header>
        <div class="table-wrap"><table><thead><tr><th>Market</th><th>Side</th><th class="n">Contracts</th><th class="n">Cost</th><th class="n">Fees</th><th class="n">Exposure</th><th class="n">Mark</th><th class="n">Unrealized</th><th class="n">Closes</th></tr></thead><tbody id="tr-pos"></tbody></table></div></section>
      <section class="panel"><div class="tabs" role="tablist">
          <button role="tab" data-tab="decisions">Decisions</button><button role="tab" data-tab="fills">Simulated fills</button><button role="tab" data-tab="settled">Settled</button></div>
        <div class="toolbar" style="margin-top:12px"><input class="input" id="tr-q" type="search" placeholder="Search market, strategy or reason" aria-label="Search trades" value="${T.q}">
          <div class="seg" id="tr-view"><label><input type="radio" name="v" value="all"><span>All</span></label><label><input type="radio" name="v" value="taken"><span>Taken</span></label><label><input type="radio" name="v" value="skipped"><span>Skipped</span></label></div></div>
        <div class="table-wrap"><table id="tr-table"></table></div><p class="faint small" id="tr-foot" style="margin-top:10px"></p></section>`);
      const sync = () => {
        $$('.tabs button', page).forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === T.tab)));
        $('#tr-view').hidden = T.tab !== 'decisions';
        $$('#tr-view input').forEach((i) => { i.checked = i.value === T.view; });
      };
      $('.tabs', page).addEventListener('click', (e) => { const b = e.target.closest('[data-tab]'); if (b) { T.tab = b.dataset.tab; sync(); tick(); } });
      $('#tr-view').addEventListener('change', (e) => { T.view = e.target.value; tick(); });
      let deb;
      $('#tr-q').addEventListener('input', (e) => { clearTimeout(deb); deb = setTimeout(() => { T.q = e.target.value; tick(); }, 250); });
      sync();
    },
    async refresh() {
      const T = PAGES.trades;
      const r = await api(`/api/trades?q=${encodeURIComponent(T.q)}&view=${T.view}&limit=100`);
      $('#tr-kpis').innerHTML = show([['Open positions', int(r.positions.length)], ['Exposure (worst case)', money(r.exposure, false)], ['Unrealized', r.unmarked ? html`${money(r.unrealized)}<small>${r.unmarked} unmarked</small>` : money(r.unrealized)], ['Mode', html`<span style="font-family:var(--font);font-size:14px">${r.mode === 'paper' ? 'Paper (simulated)' : r.mode}</span>`]]
        .map(([k, v]) => html`<div class="kpi"><div class="kpi-label">${k}</div><div class="kpi-value">${v}</div></div>`));
      $('#tr-pos').innerHTML = show(r.positions.length ? r.positions.map((p) => html`<tr><td class="mono">${p.ticker}</td><td>${p.outcome_side.toUpperCase()}</td><td class="n">${int(p.count)}</td><td class="n">${money(p.cost, false)}</td><td class="n">${money(p.fees, false)}</td><td class="n">${money(p.cost + p.fees, false)}</td><td class="n">${price(p.mark)}</td><td class="n">${pnl(p.unrealized)}</td><td class="n num">${clock(p.close_ts)}</td></tr>`)
        : emptyRow(9, 'No open positions.'));
      const q = T.q ? ` matching "${T.q}"` : '';
      let head, rows;
      if (T.tab === 'decisions') {
        head = html`<tr><th>Time</th><th>Strategy</th><th>Market</th><th>Side</th><th class="n">Price</th><th class="n">Model p</th><th class="n">Market p</th><th class="n">Net edge</th><th>Decision</th><th>Why</th></tr>`;
        rows = r.decisions.length ? r.decisions.map((d) => html`<tr class="click" data-act="decision" data-id="${d.id}"><td class="num">${when(d.ts)}</td><td class="mono">${d.strategy}</td><td class="mono">${d.ticker}</td><td>${d.outcome_side ? d.outcome_side.toUpperCase() : '–'}</td><td class="n">${price(d.entry_price)}</td><td class="n">${pct(d.q_win)}</td><td class="n">${pct(d.market_q)}</td><td class="n">${d.net_edge == null ? DASH : cents(d.net_edge)}</td><td><span class="pill ${d.accepted ? 'pos' : ''}">${d.accepted ? 'Taken' : 'Skipped'}</span></td><td class="why">${d.reason}</td></tr>`)
          : emptyRow(10, `No decisions${q}.`);
      } else if (T.tab === 'fills') {
        head = html`<tr><th>Time</th><th>Market</th><th>Side</th><th class="n">Qty</th><th class="n">Price</th><th class="n">Fee</th><th>Strategy</th><th>Outcome</th><th>Why it was placed</th></tr>`;
        rows = r.fills.length ? r.fills.map((f) => html`<tr class="${f.signal_id ? 'click' : ''}" ${raw(f.signal_id ? `data-act="decision" data-id="${f.signal_id}"` : '')}><td class="num">${when(f.ts)}</td><td class="mono">${f.ticker}</td><td>${f.outcome_side.toUpperCase()}</td><td class="n">${int(f.count)}</td><td class="n">${price(f.price)}</td><td class="n">${money(f.fee, false)}</td><td class="mono">${f.strategy || '–'}</td><td>${f.position_status === 'settled' ? html`${f.result?.toUpperCase()} ${pnl(f.pnl)}` : html`<span class="pill">Open</span>`}</td><td class="why">${f.why || '–'}</td></tr>`)
          : emptyRow(9, `No simulated fills${q}.`);
      } else {
        head = html`<tr><th>Market</th><th>Side</th><th class="n">Qty</th><th class="n">Cost</th><th class="n">Fees</th><th>Result</th><th class="n">P&amp;L</th><th class="n">Settled</th></tr>`;
        rows = r.settled.length ? r.settled.map((p) => html`<tr><td class="mono">${p.ticker}</td><td>${p.outcome_side.toUpperCase()}</td><td class="n">${int(p.count)}</td><td class="n">${money(p.cost, false)}</td><td class="n">${money(p.fees, false)}</td><td>${p.result?.toUpperCase()} ${p.result === p.outcome_side ? '(won)' : '(lost)'}</td><td class="n">${pnl(p.pnl)}</td><td class="n num">${when(p.settled_at)}</td></tr>`)
          : emptyRow(8, `No settled positions${q}.`);
      }
      $('#tr-table').innerHTML = show(html`<thead>${head}</thead><tbody>${rows}</tbody>`);
      const n = { decisions: r.decisions, fills: r.fills, settled: r.settled }[T.tab].length;
      $('#tr-foot').textContent = n >= r.limit ? `Showing the latest ${r.limit}. Search to narrow it down.` : `${n} shown.`;
    },
  };

  async function openDecision(id) {
    const d = $('#detail'), body = $('#detail-body');
    body.innerHTML = '<p class="muted">Loading…</p>';
    if (!d.open) d.showModal();
    let r;
    try { r = await api(`/api/decision?id=${encodeURIComponent(id)}`); } catch (e) { body.textContent = e.message; return; }
    const s = r.signal, data = s.data || {}, qt = r.quote, pos = r.position, m = r.market;
    const kv = (rows) => html`<dl class="kv">${rows.filter(Boolean).map(([k, v]) => html`<div><dt>${k}</dt><dd>${v}</dd></div>`)}</dl>`;
    body.innerHTML = show(html`
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div><h2 class="mono">${s.ticker}</h2><p class="muted small">${s.strategy} · ${when(s.ts)} · ${s.mode === 'paper' ? 'simulated paper trading' : s.mode}</p></div>
        <form method="dialog"><button class="btn">Close</button></form></div>
      <div><span class="pill ${s.accepted ? 'pos' : ''}">${s.accepted ? 'Taken' : 'Skipped'}</span></div>
      <div><div class="kpi-label">Why</div><p style="margin-top:4px;font-size:15px;line-height:22px">${s.reason || '–'}</p></div>
      ${s.q_win != null || s.net_edge != null ? html`<section><h3 class="kpi-label" style="margin-bottom:4px">The numbers behind it</h3>${kv([
        s.outcome_side && ['Side', s.outcome_side.toUpperCase()], ['Entry price', price(s.entry_price)],
        s.q_win != null && ['Model win probability', pct(s.q_win)], s.market_q != null && ['Market-implied probability', pct(s.market_q)],
        s.net_edge != null && ['Net edge after fees and buffer', cents(s.net_edge)], s.net_edge != null && ['Edge required (Settings)', cents(r.min_net_edge)],
        data.fee != null && ['Fee per contract', money(data.fee, false)], data.spot != null && ['Spot (proxy)', html`<span class="num">${level(data.spot)}</span>`],
        data.sigma != null && ['Volatility used', html`<span class="num">${data.sigma}</span>`], data.seconds_left != null && ['Seconds left', int(data.seconds_left)]])}</section>` : ''}
      ${qt ? html`<section><h3 class="kpi-label" style="margin-bottom:4px">Paper bid (maker rule)</h3>${kv([['Bid', html`${qt.side.toUpperCase()} at ${price(qt.price)}`], ['Book when posted', html`${price(qt.yes_bid)} / ${price(qt.yes_ask)} yes bid / ask`], ['Contracts ahead in the queue', int(qt.queue)], ['Seconds left', int(qt.seconds_left)], ['Risk decision', qt.risk || '–'], ['Status', qt.status], qt.fill_ts && ['Filled by a trade at', html`${price(qt.fill_trade_price)} at ${when(qt.fill_ts)}`]])}</section>` : ''}
      ${r.orders.length ? html`<section><h3 class="kpi-label" style="margin-bottom:4px">Simulated order${r.orders.length > 1 ? 's' : ''}</h3>${r.orders.map((o) => kv([['State', o.state], ['Limit', price(o.limit_price)], ['Filled', html`${int(o.filled)} of ${int(o.count)}`], ['Average price', price(o.avg_price)], ['Fees', money(o.fees, false)]]))}</section>` : ''}
      ${r.fills.length ? html`<section><h3 class="kpi-label" style="margin-bottom:4px">Fills</h3>${kv(r.fills.map((f) => [when(f.ts), html`${int(f.count)} at ${price(f.price)}, fee ${money(f.fee, false)}`]))}</section>` : ''}
      ${pos ? html`<section><h3 class="kpi-label" style="margin-bottom:4px">Position</h3>${kv([['Status', pos.status], ['Cost and fees', money(pos.cost + pos.fees, false)], pos.result && ['Result', pos.result.toUpperCase()], pos.pnl != null && ['Realized P&L', pnl(pos.pnl)]])}</section>` : ''}
      ${m ? html`<section><h3 class="kpi-label" style="margin-bottom:4px">Market</h3>${kv([['Series', m.series], ['Strike', html`<span class="num">${level(m.floor_strike)}</span>`], ['Window', `${when(m.open_ts)} to ${clock(m.close_ts)}`], ['Result', m.result ? m.result.toUpperCase() : 'not settled yet']])}</section>` : ''}
      ${s.data ? html`<details><summary class="muted small" style="cursor:pointer">Everything the strategy recorded</summary><pre style="margin-top:8px">${JSON.stringify(s.data, null, 2)}</pre></details>` : ''}`);
  }

  PAGES.strategies = {
    title: 'Strategies', every: 30000,
    mount() {
      page.innerHTML = show(html`
      <div class="title-row"><div><h1>Strategies</h1><p class="sub" id="sg-sub">Loading results</p></div></div>
      <div id="sg-notice"></div>
      <section class="panel"><header><h2>Historical backtest</h2><span class="meta" id="sg-hsrc"></span></header>
        <div class="table-wrap"><table id="sg-hist"></table></div></section>
      <section class="panel"><header><h2>Paper trading</h2><span class="meta">Live Kalshi data, simulated fills, this database only</span></header>
        <div class="table-wrap"><table id="sg-paper"></table></div></section>`);
    },
    async refresh() {
      const r = await api('/api/strategies');
      S.strat = r; S.stratAt = Date.now();
      $('#sg-sub').textContent = r.research_verdict ? `Latest research verdict: ${r.research_verdict}` : 'No research report yet, so no strategy has a historical verdict.';
      $('#sg-notice').innerHTML = show(r.have_reports ? '' : html`<div class="notice"><b>No reports found in ${r.reports_dir}/.</b> Historical results come from <span class="mono">kbot research</span> and <span class="mono">kbot maker-history</span> report files. Run them, or copy the saved reports into that folder.</div>`);
      $('#sg-hsrc').textContent = [...new Set(r.strategies.map((x) => x.historical?.source).filter(Boolean))].join(' · ');
      const head = html`<thead><tr><th>Strategy</th><th class="n">Trades</th><th class="n">Gross</th><th class="n">Fees</th><th class="n">Net</th><th class="n">Net per trade</th><th class="n">Max drawdown</th><th class="n">Against no-trade</th><th>Verdict</th></tr></thead>`;
      const name = (x, extra = '') => html`<td><b class="mono">${x.name}</b> ${x.configured ? html`<span class="pill">Configured</span>` : ''}${extra}${x.note ? html`<span class="sub" style="white-space:normal;max-width:340px">${x.note}</span>` : ''}</td>`;
      const vs = (net, perContract) => (net == null ? DASH : net === 0 ? html`<span class="muted">same as not trading</span>` : html`<span class="${net < 0 ? 'neg' : ''}">${perContract ? cents(net) : usd(net)} ${net < 0 ? 'worse' : 'better in this sample'}</span>`);
      const base = html`<tr class="ref"><td><b class="mono">no-trade baseline</b><span class="sub">Doing nothing. Every strategy must beat this after fees.</span></td><td class="n">${int(0)}</td><td class="n">${money(0)}</td><td class="n">${money(0, false)}</td><td class="n">${money(0)}</td><td class="n">${DASH}</td><td class="n">${money(0, false)}</td><td class="n">${DASH}</td><td>${DASH}</td></tr>`;
      $('#sg-hist').innerHTML = show(html`${head}<tbody>${base}${r.strategies.filter((x) => x.name !== 'no_trade').map((x) => {
        const h = x.historical;
        if (!h) return html`<tr>${name(x)}<td class="n" colspan="7"><span class="faint">Not in the latest report</span></td><td>${x.backtestable ? html`<a class="card-link" href="#/backtest">Backtest it</a>` : DASH}</td></tr>`;
        const pc = h.unit !== 'per trade';
        return html`<tr>${name(x)}<td class="n">${int(h.trades)}${pc ? html`<span class="sub">fills</span>` : ''}</td><td class="n">${pc ? cents(h.gross_per_trade) : money(h.gross)}${pc && h.gross_per_trade != null ? html`<span class="sub">per contract</span>` : ''}</td><td class="n">${pc ? DASH : money(h.fees, false)}</td><td class="n">${pc ? DASH : pnl(h.net)}</td>
          <td class="n">${cents(h.per_trade)}${ci(h.ci)}</td><td class="n">${h.max_drawdown == null ? html`<span class="faint">not measured</span>` : money(h.max_drawdown, false)}</td><td class="n">${vs(pc ? h.per_trade : h.net, pc)}</td><td>${verdictPill(h.verdict)}</td></tr>`;
      })}</tbody>`);
      const paperRows = [];
      r.strategies.forEach((x) => {
        if (x.paper) paperRows.push(html`<tr>${name(x, html`<span class="sub">Paper account, risk-limited</span>`)}<td class="n">${int(x.paper.trades)}</td><td class="n">${money(x.paper.gross)}</td><td class="n">${money(x.paper.fees, false)}</td><td class="n">${pnl(x.paper.net)}</td><td class="n">${cents(x.paper.per_trade)}</td><td class="n">${money(x.paper.max_drawdown, false)}</td><td class="n">${vs(x.paper.trades ? x.paper.net : null)}</td><td>${verdictPill(x.paper.trades < 30 ? { key: 'thin', text: x.paper.trades ? `Too few trades to judge (${x.paper.trades})` : 'No trades yet' } : null)}</td></tr>`);
        const c = x.paper_per_contract;
        if (c) paperRows.push(html`<tr><td><b class="mono">${x.name}</b><span class="sub" style="white-space:normal;max-width:340px">${c.what}${c.since_utc ? `, since ${c.since_utc} UTC` : ''}</span></td><td class="n">${int(c.n)}<span class="sub">fills</span></td><td class="n">${cents(c.gross)}${c.gross != null ? html`<span class="sub">per contract</span>` : ''}</td><td class="n">${DASH}</td><td class="n">${cents(c.net)}${c.net != null ? html`<span class="sub">per contract</span>` : ''}</td><td class="n">${cents(c.net)}${ci(c.ci)}</td><td class="n"><span class="faint">not measured</span></td><td class="n">${vs(c.net, true)}</td><td>${verdictPill(c.verdict)}</td></tr>`);
      });
      $('#sg-paper').innerHTML = show(html`${head}<tbody>${base}${paperRows.length ? paperRows : emptyRow(9, 'No paper results in this database yet. Start the paper bot from Bot controls.')}</tbody>`);
    },
  };

  PAGES.backtest = {
    title: 'Backtesting', every: 10000, opts: null,
    async mount() {
      const T = PAGES.backtest;
      page.innerHTML = show(html`<div class="title-row"><div><h1>Backtesting</h1><p class="sub">Replays settled Kalshi markets through the same strategy, risk and execution code as the bot. Results are a HISTORICAL BACKTEST, not a forecast.</p></div></div><div id="bt-body"><p class="muted">Loading datasets…</p></div>`);
      T.opts = await api('/api/backtest');
      const o = T.opts;
      if (!o.datasets.length) {
        $('#bt-body').innerHTML = show(html`<div class="panel"><div class="empty" style="min-height:140px">No backtest dataset in the data folder. A dataset is a SQLite file with settled markets and minute candles.<br>Make one with <span class="mono">python -m kbot collect --days 60</span> and <span class="mono">collect-spot --days 60</span>, or copy kbot-90d.sqlite into data/.</div></div>`);
        return;
      }
      const sc = o.strategy_config;
      $('#bt-body').innerHTML = show(html`<div class="grid">
        <form class="panel s5" id="bt-form" novalidate><header><h2>Configure</h2></header><div style="display:grid;gap:18px">
          <div class="field"><label for="bt-ds">Dataset</label><select class="input" id="bt-ds" name="dataset">${o.datasets.map((d) => html`<option value="${d.path}">${d.name} · ${nf(0).format(d.settled_markets)} settled markets</option>`)}</select><span class="hint" id="bt-ds-info"></span></div>
          <div class="field"><label for="bt-st">Strategy</label><select class="input" id="bt-st" name="strategy">${o.strategies.map((x) => html`<option ${x === (o.strategies.includes(sc.name) ? sc.name : o.strategies.find((y) => y !== 'no_trade')) ? 'selected' : ''}>${x}</option>`)}</select><span class="hint">The maker strategy needs public trades, so it runs with <span class="mono">kbot maker-history</span> instead.</span></div>
          <div class="field"><span class="label">Data split</span><div class="seg" id="bt-split">${Object.entries(o.splits).map(([k, v], i) => html`<label><input type="radio" name="split" value="${k}" ${i === 0 ? 'checked' : ''}><span>${v}</span></label>`)}<label title="The last 20% is held back for one final check. It cannot be run from this page."><input type="radio" disabled><span>Holdout (locked)</span></label></div>
            <span class="hint">Design and tune on train. Validation and holdout are evaluation data: looking at them and then changing settings turns them into training data.</span></div>
          <div class="field"><span class="label">Series</span><div class="chips" id="bt-series"></div><span class="hint">None ticked means every series in the dataset.</span></div>
          <div class="fields"><div class="field"><label for="bt-slip">Slippage ($ per contract)</label><input class="input" id="bt-slip" name="slippage" type="number" step="0.005" min="0" max="0.1" value="${o.defaults.slippage}"></div>
            <div class="field"><label for="bt-fp">Fill probability</label><input class="input" id="bt-fp" name="fill_prob" type="number" step="0.05" min="0.05" max="1" value="${o.defaults.fill_prob}"></div>
            <div class="field"><label for="bt-bank">Bankroll ($)</label><input class="input" id="bt-bank" name="bankroll" type="number" step="1" min="1" max="100000" value="${o.defaults.bankroll}"></div></div>
          <p class="hint small muted">Strategy parameters and risk limits come from <a class="card-link" href="#/settings">Settings</a>: min edge ${nf(2).format(sc.min_net_edge)}, entries ${sc.min_seconds_left} to ${sc.max_seconds_left} s before close, max spread ${sc.max_spread}, ${nf(1).format(o.risk.max_position_risk_pct * 100)}% per position.</p>
          <label class="check" id="bt-confirm-row" hidden><input type="checkbox" id="bt-confirm"><span>I will not change strategy settings because of this result. <span class="muted" id="bt-looks"></span></span></label>
          <p class="neg small" id="bt-err" role="alert"></p>
          <div class="btn-row"><button class="btn primary" id="bt-run">Run backtest</button></div></div></form>
        <section class="panel s7"><header><h2>Result</h2><span class="meta" id="bt-meta"></span></header><div id="bt-limits"></div><div id="bt-result"><div class="empty">Run a backtest to see trades, gross and net P&amp;L, fees, drawdown and the equity curve here.</div></div></section></div>
        <section class="panel" style="margin-top:24px"><header><h2>Past runs</h2><span class="meta">Saved in reports/ as web-backtest-*.json</span></header><div class="table-wrap"><table id="bt-hist"></table></div></section>`);
      const f = $('#bt-form');
      const ds = () => o.datasets.find((d) => d.path === $('#bt-ds').value);
      const syncDs = () => {
        const d = ds();
        $('#bt-ds-info').textContent = `${day(d.first_close)} to ${day(d.last_close)} · ${d.series.length} series · ${d.spot_points ? nf(0).format(d.spot_points) + ' spot points (Coinbase proxy)' : 'no spot prices, so fair_value cannot trade'} · holdout opened ${d.holdout_opened} time${d.holdout_opened === 1 ? '' : 's'}`;
        $('#bt-series').innerHTML = show(d.series.map((x) => html`<label><input type="checkbox" value="${x}">${short(x)}</label>`));
        $('#bt-looks').textContent = `Validation has been read ${d.validation_looks} time${d.validation_looks === 1 ? '' : 's'} from this page on this dataset.`;
        T.renderLimits();
      };
      const syncSplit = () => { $('#bt-confirm-row').hidden = f.split.value === 'train'; $('#bt-confirm').checked = false; };
      $('#bt-ds').addEventListener('change', syncDs);
      $('#bt-split').addEventListener('change', syncSplit);
      f.addEventListener('submit', async (e) => {
        e.preventDefault();
        $('#bt-err').textContent = '';
        try {
          await api('/api/backtest', { dataset: f.dataset.value, strategy: f.strategy.value, split: f.split.value, series: $$('#bt-series input:checked').map((i) => i.value), slippage: f.slippage.value, fill_prob: f.fill_prob.value, bankroll: f.bankroll.value, confirm_evaluation: $('#bt-confirm').checked });
          T.every = 1000; tick();
        } catch (err) { $('#bt-err').textContent = err.message; }
      });
      syncDs(); syncSplit();
    },
    renderLimits() {
      const d = PAGES.backtest.opts.datasets.find((x) => x.path === $('#bt-ds').value);
      $('#bt-limits').innerHTML = show(html`<div class="notice" style="display:block"><b>Data limits.</b> Fills are simulated at each minute's top-of-book bid and ask from Kalshi candles; the depth behind them and your place in the queue are unknown, so real fills can be worse. Slippage and fill probability are assumptions you set. ${d.spot_points ? 'Spot prices are a Coinbase proxy for the CF Benchmarks index that settles the crypto markets.' : 'This dataset has no spot prices.'} The split is chronological: train is the first 60% of settled markets, validation the next 20%, and the last 20% (holdout) is excluded here. A positive result on a few hundred trades can be luck: read the 95% CI.${Date.now() / 1000 - d.last_close > 7 * 86400 ? ` The newest market in this dataset settled ${day(d.last_close)}; it does not include anything since.` : ''}</div>`);
    },
    async refresh() {
      const T = PAGES.backtest;
      if (!T.opts || !T.opts.datasets.length || !$('#bt-result')) return;
      const r = await api('/api/backtest');
      const j = r.job, running = j && j.state === 'running';
      T.every = running ? 1000 : 10000;
      $('#bt-run').disabled = !!running;
      $('#bt-run').textContent = running ? 'Running…' : 'Run backtest';
      $('#bt-hist').innerHTML = show(html`<thead><tr><th>Run</th><th>Strategy</th><th>Split</th><th class="n">Trades</th><th class="n">Net</th><th class="n">Fees</th><th class="n">Max drawdown</th><th class="n">Net per trade, 95% CI</th></tr></thead><tbody>${r.history.length ? r.history.map((h) => html`<tr><td class="num">${h.generated_utc.replace('T', ' ').slice(0, 16)}</td><td class="mono">${h.params.strategy}</td><td>${r.splits[h.params.split]}</td><td class="n">${int(h.summary.trades)}</td><td class="n">${pnl(h.summary.total_net_pnl ?? 0)}</td><td class="n">${money(h.summary.total_fees ?? 0, false)}</td><td class="n">${money(h.summary.max_drawdown ?? 0, false)}</td><td class="n">${h.summary.ci95_avg_net_per_trade ? html`${cents(h.summary.ci95_avg_net_per_trade[0])} to ${cents(h.summary.ci95_avg_net_per_trade[1])}` : DASH}</td></tr>`) : emptyRow(8, 'No runs yet.')}</tbody>`);
      if (!j) return;
      const p = j.params;
      $('#bt-meta').textContent = `${p.strategy} · ${r.splits[p.split]} · ${p.dataset.split('/').at(-1)}`;
      if (running) {
        $('#bt-result').innerHTML = show(html`<p class="muted" style="margin-bottom:8px">${j.phase[0].toUpperCase() + j.phase.slice(1)} · ${Math.round(Date.now() / 1000 - j.started)} s</p><div class="progress"><div style="width:${(j.progress * 100).toFixed(0)}%"></div></div><p class="faint small" style="margin-top:6px">${j.phase === 'loading data' ? 'Reading markets and candles from the dataset' : `${Math.round(j.progress * 100)}% of market events replayed`}</p>`);
        return;
      }
      if (j.state === 'error') { $('#bt-result').innerHTML = show(html`<div class="notice neg" style="display:block"><b>The backtest failed.</b> ${j.error}</div>`); return; }
      const x = j.result, m = x.metrics, n = m.trades;
      const v = n ? (!m.ci95_avg_net_per_trade ? { key: 'thin', text: `Too few trades to judge (${n})` } : m.ci95_avg_net_per_trade[1] < 0 ? { key: 'loses', text: 'Loses money after fees (95% CI below zero)' } : m.ci95_avg_net_per_trade[0] > 0 ? { key: 'unproven', text: 'Above zero in this sample only. Not proven; needs fresh data' } : { key: 'no-edge', text: 'No edge shown (95% CI includes zero)' }) : { key: 'none', text: 'No trades' };
      const key = j.id;
      if ($('#bt-result').dataset.job === key) return;
      $('#bt-result').dataset.job = key;
      const rej = Object.entries(m.rejections || {}).sort((a, b) => b[1] - a[1]).slice(0, 5);
      $('#bt-result').innerHTML = show(html`
        <div class="kpis">${[['Trades', int(n)], ['Gross', money(x.gross)], ['Fees', money(m.total_fees ?? 0, false)], ['Net', pnl(m.total_net_pnl ?? 0)], ['Max drawdown', money(m.max_drawdown ?? 0, false)], ['Win rate', pct(m.win_rate)]].map(([k, val]) => html`<div class="kpi"><div class="kpi-label">${k}</div><div class="kpi-value">${val}</div></div>`)}</div>
        <p style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">${verdictPill(v)} <span class="muted">${n ? html`Net per trade ${cents(m.avg_net_per_trade)}${m.ci95_avg_net_per_trade ? html` (95% CI ${cents(m.ci95_avg_net_per_trade[0])} to ${cents(m.ci95_avg_net_per_trade[1])})` : ''}. No-trade baseline: $0.00, so this run is ${usd(Math.abs(m.total_net_pnl), false)} ${m.total_net_pnl < 0 ? 'worse' : 'better in this sample'}.` : 'The strategy found no trade it was allowed to take. Same as the no-trade baseline.'}</span></p>
        <div class="chart" id="bt-chart" style="margin-top:12px"></div>
        <dl class="kv" style="margin-top:12px">
          <div><dt>Data status</dt><dd class="small" style="text-align:right">${p.split === 'train' ? 'Previously examined: train is development data, and the existing strategies were built on it.' : `Previously examined: validation has been read ${x.validation_looks} time${x.validation_looks === 1 ? '' : 's'} from this page, and earlier research used it too.`} Nothing in this dataset is untouched; that takes markets that settle after a strategy is frozen.</dd></div>
          <div><dt>Series</dt><dd class="mono">${p.series.length ? p.series.map(short).join(' · ') : 'All in the dataset'}</dd></div>
          <div><dt>Parameters</dt><dd class="small">min edge ${price(x.strategy_config.min_net_edge)} · entries ${x.strategy_config.min_seconds_left} to ${x.strategy_config.max_seconds_left} s before close · max spread ${price(x.strategy_config.max_spread)} · slippage ${price(p.slippage)} · fill probability ${pct(p.fill_prob, 0)} · ${usd(p.bankroll, false, 0)} bankroll</dd></div>
          <div><dt>Markets replayed</dt><dd class="num">${nf(0).format(x.split.markets_in_run)} of ${nf(0).format(x.split.markets_loaded)}</dd></div>
          <div><dt>Train</dt><dd class="num">${x.split.train ? `${day(x.split.train[0])} to ${day(x.split.train[1])}` : '–'}</dd></div>
          <div><dt>Validation</dt><dd class="num">${x.split.validation ? `${day(x.split.validation[0])} to ${day(x.split.validation[1])}` : '–'}</dd></div>
          <div><dt>Holdout (excluded)</dt><dd class="num">${x.split.holdout ? `${day(x.split.holdout[0])} to ${day(x.split.holdout[1])}` : '–'}</dd></div>
          <div><dt>Most common skip reasons (first word)</dt><dd class="small">${rej.length ? rej.map(([k, c]) => `${k} ${nf(0).format(c)}`).join(' · ') : '–'}</dd></div>
        </dl>`);
      drawLine($('#bt-chart'), x.equity_curve, p.bankroll, key);
    },
  };

  PAGES.controls = {
    title: 'Bot controls',
    mount() {
      page.innerHTML = show(html`
      <div class="title-row"><div><h1>Bot controls</h1><p class="sub" id="ct-sub"></p></div><div class="actions"><span class="stamp" id="ct-stamp"></span></div></div>
      <div class="notice" style="background:var(--surface);border-color:var(--border);color:var(--muted)"><b style="color:var(--text)">Real-money trading is disabled.</b> This page starts the bot in paper mode only and has no way to arm production. Arming stays a manual step in docs/GO_LIVE.md.</div>
      <div id="ct-warn"></div>
      <div class="grid stretch">
        <section class="panel s6" style="display:flex;flex-direction:column"><header><h2>Paper-trading process</h2></header><div id="ct-proc" style="display:flex;flex-direction:column;gap:14px;flex:1"></div></section>
        <section class="panel s6" style="display:flex;flex-direction:column"><header><h2>Emergency stop</h2></header><div id="ct-estop" style="display:flex;flex-direction:column;gap:14px;flex:1"></div></section>
      </div>
      <div class="grid stretch">
        <section class="panel s6"><header><h2>Health</h2><span class="meta">Is it receiving data?</span></header><div id="ct-health"></div></section>
        <section class="panel s6"><header><h2>Risk limits</h2><a class="card-link" href="#/settings">Change</a></header><div id="ct-risk"></div></section>
      </div>`);
    },
    async refresh() {
      const st = S.state, b = st.bot, h = b.health || {}, L = st.limits, U = st.risk_utilization;
      $('#ct-sub').innerHTML = show(html`${statePill(b)} <span style="margin-left:8px">${b.detail}</span>`);
      $('#ct-stamp').textContent = `Updated ${clock(st.now)}`;
      const warns = [];
      if (st.settings_changed_since_start && b.state !== 'stopped') warns.push(html`<div class="notice"><b>Settings changed since the bot started.</b> It keeps using the old ones until you stop and start it.</div>`);
      if (b.halted && !b.estop && !b.pause) warns.push(html`<div class="notice neg"><b>Entries halted by a risk limit.</b> ${b.halted}. Review, then resume.</div>`);
      if (st.reconcile_problems && st.reconcile_problems.length) warns.push(html`<div class="notice neg"><b>Reconciliation problems:</b> ${st.reconcile_problems.join('; ')}</div>`);
      if (st.launch && st.launch.exit_code) warns.push(html`<div class="notice neg" style="display:block"><b>The bot process exited with code ${st.launch.exit_code}.</b> Last lines of logs/web-bot.log:<pre style="margin-top:8px">${(st.launch.log || []).join('\n')}</pre></div>`);
      $('#ct-warn').innerHTML = show(warns);
      const stopped = b.state === 'stopped', starting = b.state === 'starting';
      $('#ct-proc').innerHTML = show(html`
        <dl class="kv"><div><dt>State</dt><dd>${statePill(b)}</dd></div><div><dt>Process</dt><dd class="num">${b.pid ? `pid ${b.pid}` : '–'}</dd></div><div><dt>Started</dt><dd>${b.started && !stopped ? when(b.started) : '–'}</dd></div><div><dt>Strategy</dt><dd class="mono">${st.strategy}</dd></div><div><dt>Entries</dt><dd>${b.estop ? 'blocked by the emergency stop' : b.pause ? `paused: ${b.pause.reason}` : b.halted ? 'halted by a risk limit' : stopped ? '–' : 'allowed'}</dd></div></dl>
        <div class="btn-row" style="margin-top:auto">
          ${b.service ? '' : stopped ? html`<button class="btn primary" data-act="start">Start paper bot</button>` : html`<button class="btn danger" data-act="stop" ${starting ? 'disabled' : ''}>Stop</button>`}
          ${b.pause || (b.halted && !b.estop) ? html`<button class="btn" data-act="resume">Resume</button>` : html`<button class="btn" data-act="pause">Pause entries</button>`}
        </div>
        <p class="faint small">${b.service ? html`The bot runs as a background service (<span class="mono">${b.service}</span>), which starts it and restarts it if it exits, so Start and Stop are not offered here. Start or stop the service as docs/HOSTING.md shows. ` : ''}Pause blocks new entries and keeps tracking open positions. ${b.service ? '' : 'Stop ends the process; positions settle on Kalshi\'s schedule either way. '}Pause works while stopped too and holds on the next start.</p>`);
      $('#ct-estop').innerHTML = show(b.estop ? html`
        <div class="notice neg" style="margin:0;display:block"><b>Engaged ${when(b.estop.ts)}.</b> ${b.estop.reason}</div>
        <p class="muted">No new orders of any kind until it is cleared. Open positions are not closed; they settle normally.</p>
        <div class="btn-row" style="margin-top:auto"><button class="btn" data-act="resume">Clear and resume…</button></div>` : html`
        <p class="muted">Blocks every new order at once and cancels open entry orders on the next tick. It is saved in the database, so it holds through a restart until you clear it. Positions are not closed.</p>
        <div class="btn-row" style="margin-top:auto"><button class="btn danger solid" data-act="estop">Emergency stop</button></div>`);
      const bad = (x) => html`<span class="neg">${x}</span>`;
      $('#ct-health').innerHTML = show(html`<dl class="kv">
        <div><dt>Heartbeat</dt><dd>${stopped ? '–' : b.heartbeat_age_s > 30 ? bad(ago(b.heartbeat_age_s)) : ago(b.heartbeat_age_s)}</dd></div>
        <div><dt>Health check</dt><dd>${stopped ? '–' : h.ok ? html`<span class="pos">ok</span>` : bad(h.reason || 'unknown')}</dd></div>
        <div><dt>Latest market data</dt><dd>${st.last_data_update ? (st.now - st.last_data_update > 30 ? bad(ago(st.now - st.last_data_update)) : ago(st.now - st.last_data_update)) : '–'}</dd></div>
        <div><dt>Data feed</dt><dd>${stopped ? '–' : h.ws ? 'WebSocket' : 'REST polling (no data key)'}</dd></div>
        <div><dt>Last successful API call</dt><dd>${h.last_api_ok && !stopped ? ago(st.now - h.last_api_ok) : '–'}</dd></div>
        <div><dt>Clock skew</dt><dd class="num">${h.clock_skew_s == null || stopped ? '–' : `${sgn(h.clock_skew_s)}${nf(2).format(Math.abs(h.clock_skew_s))} s`}</dd></div>
        <div><dt>Open markets read</dt><dd class="num">${st.active_markets.length}</dd></div>
        <div><dt>Reconciliation</dt><dd>${st.mode === 'paper' ? 'not needed in paper mode' : st.last_reconcile ? ago(st.now - st.last_reconcile) : '–'}</dd></div></dl>`);
      const meter = (label, [used, cap], fmtv) => { const r = cap ? used / cap : 0; return html`<div class="meter ${r >= 1 ? 'over' : r >= 0.75 ? 'hot' : ''}"><div class="top"><span class="muted">${label}</span><span class="num">${fmtv(used)} of ${fmtv(cap)}</span></div><div class="track"><div class="fill" style="width:${Math.min(100, r * 100).toFixed(1)}%"></div></div></div>`; };
      const dollars = (v) => usd(v, false), count = (v) => nf(0).format(v);
      $('#ct-risk').innerHTML = show(html`
        ${meter('Open risk (worst case)', U.open_risk, dollars)}${meter('Realized loss today (UTC)', U.daily_loss, dollars)}${meter('Markets with positions or orders', U.open_positions, count)}${meter('Losses in a row', U.consecutive_losses, count)}
        <dl class="kv" style="margin-top:8px"><div><dt>Bankroll</dt><dd class="num">${usd(L.bankroll, false)}</dd></div><div><dt>Worst-case loss per position</dt><dd class="num">${nf(1).format(L.max_position_risk_pct * 100)}% = ${usd(st.max_price_one_contract, false)}</dd></div><div><dt>Contracts per market side</dt><dd class="num">${L.max_market_inventory}</dd></div></dl>
        <p class="faint small" style="margin-top:8px">At these limits one contract can cost at most about ${usd(st.max_price_one_contract, false)} including fees, so pricier contracts are skipped. A loss streak or the daily loss limit halts entries until you resume.</p>`);
    },
  };

  PAGES.settings = {
    title: 'Settings', every: 0,
    async mount() {
      page.innerHTML = show(html`<div class="title-row"><div><h1>Settings</h1><p class="sub" id="se-sub">Loading</p></div></div><div id="se-body"></div>`);
      PAGES.settings.render(await api('/api/settings'));
    },
    render(c) {
      const F = Object.fromEntries(c.fields.map((f) => [(f.section ? f.section + '.' : '') + f.key, f]));
      const isPct = (k) => k.endsWith('_pct');
      const LABEL = { bankroll: 'Bankroll ($)', max_position_risk_pct: 'Worst-case loss per position (%)', max_open_risk_pct: 'Worst-case loss across everything open (%)', max_daily_loss_pct: 'Realized loss per UTC day (%)', max_consecutive_losses: 'Losses in a row before a halt', max_open_positions: 'Markets with positions or orders', max_market_inventory: 'Contracts per market side (maker)', min_net_edge: 'Minimum net edge ($ per contract)', min_seconds_left: 'No entries in the last (s)', max_seconds_left: 'No entries before (s left)', max_spread: 'Maximum spread ($)', min_top_size: 'Minimum size at the best price', vol_lookback_s: 'Volatility lookback (s)', model_uncertainty: 'Model uncertainty buffer ($)', max_data_age_s: 'Oldest usable book (s)', max_clock_skew_s: 'Allowed clock skew (s)', loop_interval_s: 'Loop interval (s)' };
      const input = (k) => { const f = F[k], v = isPct(k) ? +(f.value * 100).toFixed(4) : f.value; return html`<div class="field"><label for="f-${k}">${LABEL[f.key] || f.key}</label><input class="input" id="f-${k}" name="${k}" type="number" step="any" value="${v}" data-was="${v}">${f.range ? html`<span class="hint">${f.range[0]} to ${f.range[1]}</span>` : isPct(k) ? html`<span class="hint">above 0, at most 25</span>` : ''}</div>`; };
      const known = [...new Set([...c.known_series, ...F.series.value])].sort();
      const strat = F['strategy.name'].value;
      $('#se-sub').textContent = `Saved to ${c.config}. The bot reads these when it starts, so stop and start it to apply a change.`;
      $('#se-body').innerHTML = show(html`<form id="se-form" novalidate>
        <div class="set-row"><div><h2>Markets</h2><p>Which 15-minute series the bot reads and trades. None ticked means every series ending in 15M that it finds.</p></div>
          <div class="panel"><div class="chips" id="se-series">${known.map((x) => html`<label><input type="checkbox" value="${x}" ${F.series.value.includes(x) ? 'checked' : ''}>${x}</label>`)}</div>
          <div class="field" style="margin-top:14px;max-width:320px"><label for="se-add">Add a series</label><input class="input" id="se-add" placeholder="KXDOGE15M" autocomplete="off"><span class="hint">Ticker of a 15-minute series, ending in 15M.</span></div></div></div>
        <div class="set-row"><div><h2>Strategy</h2><p>Changing the strategy changes what the paper bot does on its next start. No strategy has a proven edge; see Strategies.</p></div>
          <div class="panel"><div class="field" style="max-width:320px"><label for="f-strategy.name">Strategy</label><select class="input" id="f-strategy.name" name="strategy.name" data-was="${strat}">${c.strategies.map((x) => html`<option ${x === strat ? 'selected' : ''}>${x}</option>`)}</select><span class="hint">maker runs in paper mode only and uses E2's frozen quoting rule, not the parameters below.</span></div>
          <div class="fields" style="margin-top:16px">${['min_net_edge', 'model_uncertainty', 'min_seconds_left', 'max_seconds_left', 'max_spread', 'min_top_size', 'vol_lookback_s'].map((k) => input('strategy.' + k))}</div></div></div>
        <div class="set-row"><div><h2>Risk limits</h2><p>Hard limits every order passes through. Raising one asks you to confirm. The values below are fractions of the bankroll shown as percentages.</p></div>
          <div class="panel"><div class="fields">${['bankroll', 'max_position_risk_pct', 'max_open_risk_pct', 'max_daily_loss_pct', 'max_consecutive_losses', 'max_open_positions', 'max_market_inventory'].map((k) => input('risk.' + k))}</div><p class="warn small" id="se-raise" style="margin-top:12px"></p></div></div>
        <div class="set-row"><div><h2>Data and loop</h2><p>When the bot treats data as too old to trade on, and how often it runs.</p></div>
          <div class="panel"><div class="fields">${['max_data_age_s', 'max_clock_skew_s', 'loop_interval_s'].map(input)}</div></div></div>
        <div class="set-row"><div><h2>Fixed here</h2><p>Changed only in the config file or environment, never from this page.</p></div>
          <div class="panel"><dl class="kv"><div><dt>Mode</dt><dd>${c.mode}</dd></div><div><dt>Market data from</dt><dd>${c.data_env}</dd></div><div><dt>Database</dt><dd class="mono">${c.db_path}</dd></div><div><dt>Real-money trading</dt><dd>${c.production_trading === 'disabled' ? 'Disabled' : 'ARMED'}</dd></div><div><dt>API keys</dt><dd>Read by the bot from your .env file. Never sent to this page.</dd></div></dl></div></div>
        <div class="form-foot"><span class="small" id="se-msg" role="status"></span><button class="btn primary" id="se-save">Save settings</button></div></form>`);
      const form = $('#se-form');
      const raised = () => $$('input[name^="risk."]', form).filter((i) => i.value !== '' && +i.value > +i.dataset.was).map((i) => LABEL[i.name.slice(5)]);
      form.addEventListener('input', () => { const r = raised(); $('#se-raise').textContent = r.length ? `Raising: ${r.join(', ')}. You will be asked to confirm.` : ''; });
      $('#se-add').addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const v = e.target.value.trim().toUpperCase();
        if (!/^KX[A-Z0-9]+15M$/.test(v)) { e.target.setAttribute('aria-invalid', 'true'); return; }
        e.target.removeAttribute('aria-invalid'); e.target.value = '';
        if (!$$('#se-series input').some((i) => i.value === v)) $('#se-series').insertAdjacentHTML('beforeend', show(html`<label><input type="checkbox" value="${v}" checked>${v}</label>`));
      });
      const save = async (confirmRaise) => {
        const values = { series: $$('#se-series input:checked').map((i) => i.value), 'strategy.name': form['strategy.name'].value };
        $$('input[type="number"]', form).forEach((i) => { values[i.name] = isPct(i.name) ? +i.value / 100 : i.value; });
        $$('[aria-invalid]', form).forEach((i) => i.removeAttribute('aria-invalid'));
        try {
          const r = await api('/api/settings', { values, confirm_raise: confirmRaise });
          const n = Object.keys(r.saved).length;
          PAGES.settings.render(r.settings);
          $('#se-msg').innerHTML = show(n ? html`<span class="pos">Saved ${n} change${n === 1 ? '' : 's'}: ${Object.keys(r.saved).join(', ')}.</span> ${S.state?.bot.state !== 'stopped' ? 'Restart the bot to apply.' : ''}` : html`<span class="muted">Nothing changed.</span>`);
        } catch (e) {
          if (e.data?.needs_confirm) {
            const a = await ask({ title: 'Raise risk limits?', text: html`${e.message}. Larger limits let the paper bot risk more of its bankroll, and the same file is what a demo or real-money run would read.`, ok: 'Raise limits', danger: true });
            if (a.ok) return save(true);
            return;
          }
          $('#se-msg').innerHTML = show(html`<span class="neg">Not saved. ${e.message}</span>`);
          e.message.split('; ').forEach((m) => { const el = form[m.split(':')[0]]; if (el && el.setAttribute) el.setAttribute('aria-invalid', 'true'); });
        }
      };
      form.addEventListener('submit', (e) => { e.preventDefault(); save(false); });
    },
  };

  // --- login, routing, polling ----------------------------------------------------------------------
  function showLogin() {
    clearTimeout(S.timer);
    S.page = null;
    page.innerHTML = show(html`<form class="panel login" id="login"><header><h2>Log in to kbot</h2></header>
      <div class="field"><label for="tok">Access token</label><input class="input" id="tok" type="password" autocomplete="current-password" required><span class="hint">The KBOT_DASHBOARD_TOKEN value set where the server runs.</span></div>
      <p class="neg small" id="login-err" style="margin-top:10px" role="alert"></p><div class="btn-row" style="margin-top:12px"><button class="btn primary">Log in</button></div></form>`);
    $('#tok').focus();
    $('#login').addEventListener('submit', async (e) => {
      e.preventDefault();
      try { await api('/api/login', { token: $('#tok').value }); await boot(); } catch (err) { $('#login-err').textContent = err.message; }
    });
  }
  async function tick() {
    clearTimeout(S.timer);
    const p = PAGES[S.page];
    if (!p) return;
    try {
      S.state = await api('/api/state');
      S.lastOk = Date.now();
      setConn(true);
      paintShell();
      if (p.refresh) await p.refresh();
    } catch (e) {
      if (e.auth) return;
      if (e.offline) setConn(false, e.message);
      else toast(e.message, true);
    }
    const every = p.every === 0 ? 15000 : p.every || 3000;    // settings: only the shell refreshes
    S.timer = setTimeout(tick, document.hidden ? Math.max(every, 15000) : every);
  }
  async function route() {
    const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
    const key = PAGES[name] ? name : 'overview';
    S.page = key;
    $$('.nav-item').forEach((a) => (a.getAttribute('href') === '#/' + key ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
    $('#topbar-title').textContent = PAGES[key].title;
    document.title = `${PAGES[key].title} · kbot`;
    $('#sidebar').classList.remove('open');
    $('#menu').setAttribute('aria-expanded', 'false');
    $('#detail').open && $('#detail').close();
    clearTimeout(S.timer);
    try {
      if (!S.state) { S.state = await api('/api/state'); S.lastOk = Date.now(); paintShell(); }
      await PAGES[key].mount();
    } catch (e) {
      if (e.auth) return;
      page.innerHTML = show(html`<div class="notice neg"><b>Could not load this page.</b> ${e.message}</div>`);
    }
    view.focus({ preventScroll: true });
    tick();
  }
  async function boot() {
    let s;
    try { s = await api('/api/session'); } catch (e) { page.innerHTML = show(html`<div class="notice neg"><b>Cannot reach the control server.</b> ${e.message} Start it with <span class="mono">scripts/web.sh</span>, then reload.</div>`); setTimeout(boot, 5000); return; }
    S.auth = s.auth_required;
    $('#logout').hidden = !s.auth_required;
    if (!s.authed) return showLogin();
    S.csrf = s.csrf;
    route();
  }
  $('#menu').addEventListener('click', () => { const o = $('#sidebar').classList.toggle('open'); $('#menu').setAttribute('aria-expanded', String(o)); });
  $('#logout').addEventListener('click', async () => { try { await api('/api/logout', {}); } catch { /* already out */ } S.state = null; showLogin(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.page) tick(); });
  window.addEventListener('hashchange', route);
  boot();
})();
