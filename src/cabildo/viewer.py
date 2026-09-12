"""Local web viewer for the cabildo bus — the open counterpart of the hosted
dashboard.

cabildo needs no network: the bus is a single SQLite file. This serves a
read-only view of THAT file (agents, locks, messages) on localhost, so you can
watch your agents in a browser without the hosted backend. Stdlib only
(http.server), like the rest of the package.

    cabildo viewer                 # http://127.0.0.1:8787
    cabildo viewer --port 9000
    cabildo viewer --open          # also open a browser tab

It binds to 127.0.0.1 by default — the bus is private to this machine and the
view is unauthenticated, so it should not be exposed on a network. Pass
--host 0.0.0.0 only if you understand that anyone who can reach the port can
read your bus.
"""

from __future__ import annotations

import argparse
import json
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import store


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _state() -> dict[str, Any]:
    """Snapshot of the local bus: active agents, held locks, recent messages."""
    conn = store.connect()
    try:
        now = time.time()

        agents = []
        for r in store.active_sessions(conn):
            k = r.keys()
            agents.append(
                {
                    "name": store.display_name(r),
                    "project": (r["project_name"] if "project_name" in k else None) or "",
                    "cwd": (r["cwd"] if "cwd" in k else "") or "",
                    "last_seen": _iso(r["last_seen_at"]),
                    "ago": int(now - r["last_seen_at"]),
                }
            )

        claims = [
            {
                "resource": c["resource"],
                "holder": c["holder"],
                "note": c.get("note") or "",
                "expires_in": c.get("expires_in"),
            }
            for c in store.list_claims(conn)
        ]

        messages = []
        for m in store.history(conn, limit=200):
            if m.get("topic"):
                to = "#" + m["topic"]
            elif m.get("to_session"):
                to = store.display_name(store.get_session(conn, m["to_session"]), m["to_session"])
            else:
                to = ""
            messages.append(
                {
                    "from": m.get("from_display") or (m.get("from_session") or "")[:8],
                    "to": to,
                    "body": m.get("body") or "",
                    "urgent": bool(m.get("urgent")),
                    "created": _iso(m.get("created_at")),
                }
            )

        # history() caps at `limit`; report the true bus size so the header does
        # not sit pinned at the fetch limit and read as "stuck".
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        return {
            "host": store.hostname(),
            "db": str(store.db_path()),
            "generated": _iso(now),
            "agents": agents,
            "claims": claims,
            "messages": messages,
            "messages_total": total,
        }
    finally:
        conn.close()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (http.server API)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                body = json.dumps(_state()).encode("utf-8")
            except Exception as e:  # a broken DB should not crash the server
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(500, body, "application/json")
                return
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, format: str, *args: Any) -> None:  # silence per-request stderr noise
        pass


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False) -> int:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print(f"cabildo viewer · {url} · bus {store.db_path()}")
    print("read-only view of the local bus. Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cabildo viewer", description=(__doc__ or "").split("\n")[0])
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    p.add_argument("--open", action="store_true", help="open a browser tab")
    args = p.parse_args(argv)
    return serve(args.host, args.port, args.open)


# --------------------------------------------------------------------------- #
# The page. Read-only: agents, locks, messages. Same visual language as the
# hosted dashboard, minus everything that only the cloud can do (spawn, tokens,
# per-project uplink, cost).
# --------------------------------------------------------------------------- #
_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cabildo · local bus</title>
<style>
  :root{
    --bg:#f6f8fa; --panel:#ffffff; --panel-2:#eef1f4; --line:#d0d7de;
    --ink:#1f2328; --dim:#57606a; --faint:#6e7781;
    --accent:#b45309; --green:#1a7f37; --red:#cf222e; --blue:#0969da; --violet:#8250df;
    --pulse:26,127,55;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{display:flex;align-items:center;gap:16px;padding:14px 22px;
    border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
  header h1{margin:0;font:600 18px var(--mono);letter-spacing:.5px}
  header h1 .dot{color:var(--accent)}
  .host{font:12px var(--mono);color:var(--dim)} .host b{color:var(--accent)}
  .live{display:flex;align-items:center;gap:7px;color:var(--dim);font:12px var(--mono)}
  .pulse{width:8px;height:8px;border-radius:50%;background:var(--green);
    box-shadow:0 0 0 0 rgba(var(--pulse),.6);animation:p 2s infinite}
  @keyframes p{0%{box-shadow:0 0 0 0 rgba(var(--pulse),.5)}70%{box-shadow:0 0 0 7px rgba(var(--pulse),0)}100%{box-shadow:0 0 0 0 rgba(var(--pulse),0)}}
  .stat{margin-left:auto;display:flex;gap:22px}
  .stat b{display:block;font:600 20px var(--mono);color:var(--ink)}
  .stat span{font:11px var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:.6px}
  main{max-width:1100px;margin:0 auto;padding:22px;display:grid;gap:18px;grid-template-columns:1fr 1fr}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .card > h2{margin:0;padding:12px 16px;font:600 12px var(--mono);letter-spacing:.8px;
    text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--line);
    display:flex;justify-content:space-between;align-items:center}
  .card > h2 .n{color:var(--accent)}
  .body{max-height:360px;overflow-y:auto}
  .wide{grid-column:1 / -1}
  .row{padding:10px 16px;border-bottom:1px solid var(--panel-2);display:flex;gap:10px;align-items:baseline}
  .row:last-child{border-bottom:0}
  .id{font:12px var(--mono);color:var(--violet)}
  .proj{font:11px var(--mono);color:var(--faint);background:var(--panel-2);padding:1px 7px;border-radius:20px}
  .cwd{font:11px var(--mono);color:var(--faint);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ago{color:var(--faint);font:11px var(--mono);white-space:nowrap}
  .res{font:12px var(--mono);color:var(--accent);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .note{color:var(--dim);font-size:12px;flex:2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .msg-from{font:12px var(--mono);color:var(--blue);min-width:120px}
  .msg-from.urgent{color:var(--red);font-weight:600}
  .msg-to{font:11px var(--mono);color:var(--faint);min-width:120px}
  .msg-body{color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;cursor:pointer}
  .msg-body.open{white-space:pre-wrap;overflow:visible;word-break:break-word}
  .msg-time{color:var(--faint);font:11px var(--mono);white-space:nowrap;margin-left:8px;min-width:34px;text-align:right}
  .empty{padding:22px 16px;color:var(--faint);font:12px var(--mono);text-align:center}
  @media(max-width:760px){main{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1><span class="dot">&#9642;</span> cabildo</h1>
  <span class="host">local bus <b id="host">&hellip;</b></span>
  <span class="live"><span class="pulse"></span><span id="clock">connecting&hellip;</span></span>
  <div class="stat">
    <div><b id="c-agents">&ndash;</b><span>agents</span></div>
    <div><b id="c-claims">&ndash;</b><span>locks</span></div>
    <div><b id="c-msgs">&ndash;</b><span>messages</span></div>
  </div>
</header>
<main>
  <section class="card">
    <h2>agents <span class="n" id="h-agents"></span></h2>
    <div class="body" id="agents"></div>
  </section>
  <section class="card">
    <h2>locks <span class="n" id="h-claims"></span></h2>
    <div class="body" id="claims"></div>
  </section>
  <section class="card wide">
    <h2>messages <span class="n" id="h-msgs"></span></h2>
    <div class="body" id="messages"></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
const el = (t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.textContent=h;return e;};
function ago(iso){ if(!iso) return ""; const s=Math.max(0,(Date.now()-new Date(iso))/1000);
  if(s<60)return Math.floor(s)+"s"; if(s<3600)return Math.floor(s/60)+"m"; return Math.floor(s/3600)+"h"; }
const OPEN = new Set();

async function tick(){
  let d; try{ d = await (await fetch("/api/state")).json(); }catch(e){ $("#clock").textContent="offline"; return; }
  if(d.error){ $("#clock").textContent="error: "+d.error; return; }
  $("#host").textContent = d.host || "?";
  $("#clock").textContent = "live · " + new Date().toLocaleTimeString();
  $("#c-agents").textContent = d.agents.length;
  $("#c-claims").textContent = d.claims.length;
  const total = (typeof d.messages_total==="number") ? d.messages_total : d.messages.length;
  $("#c-msgs").textContent = total > d.messages.length ? (total+" · last "+d.messages.length) : String(total);
  $("#h-agents").textContent = d.agents.length;
  $("#h-claims").textContent = d.claims.length;
  $("#h-msgs").textContent = total;

  const A=$("#agents"); A.innerHTML="";
  if(!d.agents.length) A.append(el("div","empty","no agents on the bus"));
  d.agents.forEach(a=>{
    const r=el("div","row");
    r.append(el("span","id",a.name));
    if(a.project) r.append(el("span","proj",a.project));
    r.append(el("span","cwd",a.cwd||""));
    r.append(el("span","ago",a.ago+"s ago"));
    A.append(r);
  });

  const C=$("#claims"); C.innerHTML="";
  if(!d.claims.length) C.append(el("div","empty","no active locks"));
  d.claims.forEach(c=>{
    const r=el("div","row");
    r.append(el("span","res",c.resource));
    r.append(el("span","id",c.holder));
    r.append(el("span","note",c.note||""));
    C.append(r);
  });

  const M=$("#messages");
  const atBottom = M.scrollHeight - M.scrollTop - M.clientHeight < 40;
  M.innerHTML="";
  if(!d.messages.length) M.append(el("div","empty","no messages yet"));
  d.messages.forEach((m,i)=>{
    const r=el("div","row");
    r.append(el("span","msg-from"+(m.urgent?" urgent":""),m.from));
    r.append(el("span","msg-to",m.to));
    const key=(m.created||"")+"|"+i;
    const body=el("span","msg-body"+(OPEN.has(key)?" open":""),m.body);
    body.title="click to expand";
    body.onclick=()=>{ OPEN.has(key)?OPEN.delete(key):OPEN.add(key); body.classList.toggle("open"); };
    r.append(body);
    const ts=el("span","msg-time",ago(m.created));
    if(m.created) ts.title=new Date(m.created).toLocaleString();
    r.append(ts);
    M.append(r);
  });
  if(atBottom) M.scrollTop = M.scrollHeight;
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import sys

    sys.exit(main())
