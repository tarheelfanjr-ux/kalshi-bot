"""Read-only local dashboard. Standard library only.

Binds to 127.0.0.1 by default. Binding anywhere else requires KBOT_DASHBOARD_TOKEN, sent as
?token=... or an X-Token header. It never shows key ids, key paths, or other secrets, and it
has no endpoints that place or cancel orders.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .db import DB
from .reports import LABELS, calibration, performance
from .risk import RiskManager


def state(db: DB, s: Settings) -> dict:
    running = db.get("running") or {}
    mode = running.get("mode") or s.mode
    risk = RiskManager(db, s.risk, mode)
    L = s.risk
    now = time.time()
    perf = performance(db, mode, L.bankroll)
    eq = db.one("SELECT * FROM equity WHERE mode=? ORDER BY ts DESC LIMIT 1", (mode,))
    return {
        "now": now,
        "mode": mode,
        "mode_label": LABELS.get(mode, mode),
        "running": bool(running),
        "heartbeat_age_s": round(now - (db.get("heartbeat") or 0), 1),
        "health": db.get("health"),
        "halted": risk.halted(),
        "production_trading": "ARMED" if mode == "production" else "disabled",
        "account_balance": db.get(f"balance:{mode}") if mode in ("demo", "production") else None,
        "bankroll": L.bankroll,
        "risk_utilization": {
            "open_risk": [round(risk.open_risk(), 2), round(L.max_open_risk_pct * L.bankroll, 2)],
            "daily_loss": [round(risk.daily_loss(), 2), round(L.max_daily_loss_pct * L.bankroll, 2)],
            "open_positions": [len(risk.busy_tickers()), L.max_open_positions],
            "consecutive_losses": [risk.consecutive_losses(), L.max_consecutive_losses],
        },
        "performance": perf,
        "calibration": calibration(db, mode),
        "unrealized": round(eq["open_value"], 4) if eq else None,
        "positions": [dict(r) for r in db.q(
            "SELECT ticker,outcome_side,count,cost,fees,close_ts FROM positions WHERE mode=? AND status='open'", (mode,))],
        "orders": [dict(r) for r in db.q(
            "SELECT client_order_id,ticker,outcome_side,limit_price,count,filled,state,created_at FROM orders "
            "WHERE mode=? AND state NOT IN ('filled','canceled','rejected')", (mode,))],
        "opportunities": [dict(r) for r in db.q(
            "SELECT ts,ticker,outcome_side,entry_price,q_win,market_q,net_edge,accepted,reason FROM signals "
            "WHERE mode=? AND net_edge IS NOT NULL AND ts>? ORDER BY ts DESC LIMIT 20", (mode, now - 600))],
        "recent_rejections": [dict(r) for r in db.q(
            "SELECT ts,ticker,reason FROM signals WHERE mode=? AND accepted=0 ORDER BY ts DESC LIMIT 20", (mode,))],
        "last_reconcile": db.get(f"last_reconcile:{mode}"),
        "reconcile_problems": db.get(f"reconcile_problems:{mode}"),
        "last_data_update": (db.one("SELECT MAX(ts) t FROM snapshots") or {"t": None})["t"],
        "events": [dict(r) for r in db.q(
            "SELECT ts,kind,ticker,data FROM events WHERE kind IN "
            "('error','notify','risk_trip','risk_clear','unexpected_termination','startup','shutdown') "
            "ORDER BY ts DESC LIMIT 30")],
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>kbot dashboard</title><style>
:root{--bg:#fafafa;--fg:#111;--card:#fff;--line:#ddd;--bad:#b00020;--ok:#0a7a32;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--card:#1b1b1b;--line:#333;--bad:#ff6b81;--ok:#4cd07d;--warn:#f0b35a}}
body{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 4px} h2{font-size:14px;margin:0 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
.banner{padding:8px 12px;border-radius:8px;margin:8px 0;font-weight:600;border:2px solid var(--warn)}
.bad{color:var(--bad)} .ok{color:var(--ok)} table{border-collapse:collapse;width:100%;font-size:12px}
td,th{border-bottom:1px solid var(--line);padding:3px 4px;text-align:left;white-space:nowrap}
svg{width:100%;height:140px}
</style></head><body>
<h1>Kalshi 15-minute bot</h1><div id="banner" class="banner"></div><div class="grid" id="g"></div>
<script>
const T=new URLSearchParams(location.search).get('token')||'';
const f=(x,d=2)=>x==null?'n/a':(typeof x==='number'?x.toFixed(d):x);
const t=s=>s?new Date(s*1000).toISOString().slice(11,19)+'Z':'n/a';
function table(rows,cols){if(!rows||!rows.length)return'<i>none</i>';
 return'<table><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c];
 if(c==='ts'||c.endsWith('_at')||c==='close_ts')v=t(v);else if(typeof v==='number')v=f(v,3);
 return'<td>'+String(v??'').replace(/</g,'&lt;')+'</td>'}).join('')+'</tr>').join('')+'</table>'}
function curve(pts){if(!pts||pts.length<2)return'<i>no settled trades yet</i>';
 const ys=pts.map(p=>p[1]),lo=Math.min(...ys),hi=Math.max(...ys)||1,n=pts.length;
 const d=pts.map((p,i)=>(i?'L':'M')+(i/(n-1)*300).toFixed(1)+','+(130-(p[1]-lo)/((hi-lo)||1)*120).toFixed(1)).join('');
 return'<svg viewBox="0 0 300 140"><path d="'+d+'" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'+
 '<div>min '+f(lo)+' max '+f(hi)+'</div>'}
async function load(){const r=await fetch('/api/state'+(T?'?token='+encodeURIComponent(T):''));const s=await r.json();
 const h=s.health||{};const P=s.performance;const U=s.risk_utilization;
 document.getElementById('banner').innerHTML=s.mode_label+' | production trading: '+s.production_trading+
  (s.halted?' | <span class="bad">HALTED: '+s.halted+'</span>':'');
 const card=(title,body)=>'<div class="card"><h2>'+title+'</h2>'+body+'</div>';
 document.getElementById('g').innerHTML=[
 card('Status','Mode: <b>'+s.mode+'</b><br>Process: '+(s.running?'<span class=ok>running</span>':'<span class=bad>not running</span>')+
  ' (heartbeat '+s.heartbeat_age_s+'s ago)<br>Health: '+(h.ok?'<span class=ok>ok</span>':'<span class=bad>'+(h.reason||'unknown')+'</span>')+
  '<br>WebSocket: '+(h.ws?'connected':'off')+'<br>Clock skew: '+f(h.clock_skew_s)+'s<br>Last API OK: '+t(h.last_api_ok)+
  '<br>Last data update: '+t(s.last_data_update)+'<br>Last reconcile: '+t(s.last_reconcile)+
  (s.reconcile_problems&&s.reconcile_problems.length?'<br><span class=bad>'+s.reconcile_problems.join('; ')+'</span>':'')),
 card('Money ('+s.mode+')','Account balance: '+(s.account_balance==null?'n/a (not authorized / paper)':'$'+s.account_balance)+
  '<br>Designated bankroll: $'+f(s.bankroll)+'<br>Daily realized P&L: $'+f(P.daily_realized)+'<br>Unrealized P&L: $'+f(s.unrealized)+
  '<br>Net P&L: $'+f(P.net_pnl)+'<br>Fees: $'+f(P.fees)+'<br>Settled trades: '+P.settled_trades+'<br>Win rate: '+f(P.win_rate,3)+
  '<br>Max drawdown: $'+f(P.max_drawdown)+'<br>Calibration: '+JSON.stringify(s.calibration)),
 card('Risk limit use',Object.entries(U).map(([k,v])=>k+': '+f(v[0])+' / '+f(v[1])).join('<br>')),
 card('Equity curve ('+s.mode+')',curve(P.equity_curve)),
 card('Open positions',table(s.positions,['ticker','outcome_side','count','cost','fees','close_ts'])),
 card('Outstanding orders',table(s.orders,['ticker','outcome_side','limit_price','count','filled','state'])),
 card('Opportunities (10 min)',table(s.opportunities,['ts','ticker','outcome_side','entry_price','q_win','market_q','net_edge','accepted','reason'])),
 card('Recent rejections',table(s.recent_rejections,['ts','ticker','reason'])),
 card('Errors and warnings',table(s.events,['ts','kind','ticker','data'])),
 ].join('')}
load();setInterval(load,5000);
</script></body></html>"""


def serve(db_path: str, s: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    token = os.environ.get("KBOT_DASHBOARD_TOKEN", "")
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("refusing to bind a dashboard to a non-local address without KBOT_DASHBOARD_TOKEN")

    class H(BaseHTTPRequestHandler):
        def _authed(self) -> bool:
            if not token:
                return True
            q = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            return hmac.compare_digest(q or self.headers.get("X-Token", ""), token)

        def do_GET(self):
            if not self._authed():
                self.send_error(401)
                return
            path = urlsplit(self.path).path
            if path == "/":
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif path == "/api/state":
                db = DB(db_path)
                try:
                    body, ctype = json.dumps(state(db, s), default=str).encode(), "application/json"
                finally:
                    db.close()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"dashboard on http://{host}:{port}/")
    ThreadingHTTPServer((host, port), H).serve_forever()
