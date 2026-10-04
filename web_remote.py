"""ZINCUM/SHUNGITE local web remote - control downloads from your phone.

Runs on http://<laptop-ip>:8765 while the desktop app is open.
"""
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>SHUNGITE Remote</title>
<link rel=manifest href=/manifest.json>
<meta name=theme-color content="#0a0a0b">\n<link rel=icon href=/icon.svg type=image/svg+xml>
<link rel=apple-touch-icon href=/icon.svg>
<meta name=apple-mobile-web-app-capable content=yes>
<style>
 body{background:#0a0a0b;color:#f5f5f4;font-family:system-ui;margin:0;padding:16px}
 h1{font-size:1.2rem;letter-spacing:3px;color:#fafafa}
 .card{background:#121214;border-radius:12px;padding:12px;margin-bottom:10px}
 input{width:100%;padding:10px;background:#17181a;color:#fff;border:1px solid #26262a;
       border-radius:8px;box-sizing:border-box;font-size:1rem}
 button{background:#f5f5f4;color:#0a0a0b;border:none;border-radius:8px;
        padding:10px 18px;font-weight:700;width:100%;margin-top:8px;font-size:1rem}
 .muted{color:#9ca3af;font-size:.85rem}
</style></head><body>
<h1>&#9670; SHUNGITE REMOTE</h1>
<div class=card>
 <form id=f><input id=u placeholder="YouTube / Spotify URL"><button>Add & Download</button></form>
 <p class=muted>Submits to the running SHUNGITE app on your laptop.</p>
</div>
<div class=card><h3 style=margin:0>Queue</h3><pre id=q class=muted style=white-space:pre-wrap>loading…</pre></div>
<div class=card>
 <button id=clear style="background:#26262a;color:#f5f5f4">Clear finished</button>
 <button id=dupe style="background:#ef4444;color:#fff;margin-top:6px">Delete duplicate audio (keep best)</button>
 <p class=muted style="margin:4px 0 0">Fingerprint-scans the library; in each identical-sounding group keeps only the largest (best quality) file.</p>
</div>
<script>
async function refresh(){
 try{const r=await fetch('/api/queue');const j=await r.json();
  const q=document.getElementById('q');q.textContent='';
  for(const it of j){const d=document.createElement('div');
   d.style.padding='3px 0';d.dataset.id=it.id;
   d.innerHTML='<span class=muted>'+it.status+'</span> '+it.label;
   if(it.status==='queued'){d.style.cursor='pointer';
    d.onclick=async()=>{await fetch('/api/remove?id='+encodeURIComponent(it.id),{method:'POST'});refresh();};}
   q.appendChild(d);}
 }catch(e){}}
setInterval(refresh,2000);refresh();
document.getElementById('clear').onclick=async()=>{await fetch('/api/clear',{method:'POST'});refresh()};
let dupeArmed=false;
async function pollDedupe(){
 const pr=await fetch('/api/dedupe',{method:'POST'});
 const pj=await pr.json();
 if(pj.scanning){
  document.getElementById('q').textContent=
   '⏳ scanning '+pj.done+'/'+pj.total+' files…';
  setTimeout(pollDedupe,2000);
  return; }
 if(pj.error){document.getElementById('q').textContent='⚠ '+pj.error;return;}
 document.getElementById('q').textContent=
  'found '+pj.groups+' duplicate groups — '+pj.would_delete+' files would be deleted (kept best per group)';
}
document.getElementById('dupe').onclick=async()=>{
 const b=document.getElementById('dupe');
 if(!dupeArmed){b.textContent='Tap again to confirm';
  dupeArmed=true;setTimeout(()=>{dupeArmed=false;b.textContent='Delete duplicate audio (keep best)';},4000);
  pollDedupe();
  return;}
 dupeArmed=false;b.textContent='Delete duplicate audio (keep best)';
 await fetch('/api/dedupe?confirm=1',{method:'POST'});
 document.getElementById('q').textContent='⏳ deleting duplicates…';};
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
 const u=document.getElementById('u').value.trim();if(!u)return;
 await fetch('/api/add',{method:'POST',body:u});
 document.getElementById('u').value='';setTimeout(refresh,800);};
</script></body></html>"""


class _State:
    app = None


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _manifest_json():
    return json.dumps(MANIFEST)


MANIFEST = {
    "name": "SHUNGITE Remote",
    "short_name": "SHUNGITE",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0a0a0b",
    "theme_color": "#0a0a0b",
    "icons": [{
        "src": "/icon.svg",
        "sizes": "any",
        "type": "image/svg+xml",
        "purpose": "any"
    }]
}


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="20" fill="#0a0a0b"/><text x="50" y="66" font-size="44" text-anchor="middle" fill="#f5f5f4" font-family="system-ui" font-weight="700">&#9670;</text></svg>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/manifest.json":
            self._send(200, _manifest_json(),
                       "application/manifest+json")
        elif p == "/icon.svg":
            self._send(200, ICON_SVG, "image/svg+xml")
        elif p == "/":
            self._send(200, _HTML, "text/html; charset=utf-8")
        elif p == "/api/queue":
            app = _State.app
            try:
                snap = app.dq.snapshot()
                self._send(200, json.dumps(
                    [{"id": k, "status": v["status"], "label": v["label"],
                      "error": v.get("error")}
                     for k, v in snap.items()]),
                    "application/json")
            except Exception as e:
                self._send(500, str(e))
        else:
            self._send(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/dedupe":
            app = _State.app
            qs_d = parse_qs(urlparse(self.path).query)
            confirm = (qs_d.get("confirm") or ["0"])[0] == "1"
            # if a scan already finished, serve it instantly
            if getattr(app, "_dedupe_result", None) is not None and not confirm:
                r = app._dedupe_result
                app._dedupe_result = None
                return self._send(200, json.dumps(r), "application/json")
            if getattr(app, "_dedupe_progress", None) and \
                    getattr(app, "_dedupe_result", None) is None:
                p = app._dedupe_progress
                return self._send(200, json.dumps(
                    {"scanning": True, "done": p["done"],
                     "total": p["total"]}), "application/json")
            try:
                import glob as _g
                lib = app.cfg.get(
                    "library_root",
                    os.path.expanduser("~/Desktop/Downloaded_Media2.0"))
                music = os.path.join(lib, "Music")
                files = []
                for ext in ("*.opus", "*.mp3", "*.m4a", "*.flac"):
                    files += _g.glob(os.path.join(music, "**", ext),
                                     recursive=True)
                import acoustic_dedup as _ad

                def _scan_bg():
                    try:
                        app._dedupe_progress = {"done": 0, "total": len(files)}

                        def _prog(n, total):
                            app._dedupe_progress = {"done": n, "total": total}

                        grps = _ad.find_duplicates(files, progress=_prog)
                        preview = []
                        for grp in grps[:10]:
                            keep2 = max(grp, key=os.path.getsize)
                            preview.append({
                                "keep": os.path.basename(keep2),
                                "delete": [os.path.basename(p) for p in grp
                                           if p != keep2]})
                        app._dedupe_groups = grps
                        app._dedupe_result = {
                            "groups": len(grps),
                            "would_delete": sum(len(g) - 1 for g in grps),
                            "preview": preview}
                    except Exception as e:
                        app._dedupe_result = {"error": str(e)}

                t_scan = threading.Thread(target=_scan_bg, daemon=True)
                t_scan.start()
                groups = []          # filled by the bg scan when done

                if not confirm:
                    preview = []
                    for grp in groups[:10]:
                        keep = max(grp, key=os.path.getsize)
                        preview.append({
                            "keep": os.path.basename(keep),
                            "delete": [os.path.basename(p) for p in grp
                                       if p != keep]})
                    return self._send(200, json.dumps({
                        "groups": len(groups),
                        "would_delete": sum(len(g) - 1 for g in groups),
                        "preview": preview}), "application/json")

                def run():
                    removed = 0
                    for g in groups:
                        keep = max(g, key=os.path.getsize)
                        for p in g:
                            if p != keep:
                                try:
                                    base = os.path.splitext(p)[0]
                                    for suffix in ("", ".jpg", ".lrc",
                                                   ".segments.json",
                                                   ".en.vtt"):
                                        try:
                                            os.remove(base + suffix)
                                        except OSError:
                                            pass
                                    removed += 1
                                except OSError:
                                    pass

                threading.Thread(target=run, daemon=True).start()
                return self._send(200,
                                  f"dedupe started on {len(groups)} groups")
            except Exception as e:
                return self._send(500, str(e))
        if path == "/api/remove":
            qs = parse_qs(urlparse(self.path).query)
            jid = (qs.get("id") or [""])[0]
            app = _State.app
            try:
                with app.dq.lock:
                    st = app.dq.active.get(jid)
                    if st and st["status"] == "queued":
                        app.dq.active.pop(jid, None)
                        return self._send(200, "removed")
                    return self._send(409, "not queued")
            except Exception as e:
                return self._send(500, str(e))
        if path == "/api/retry":
            qs = parse_qs(urlparse(self.path).query)
            jid = (qs.get("id") or [""])[0]
            app = _State.app
            try:
                with app.dq.lock:
                    st = app.dq.active.get(jid)
                    if st and st["status"] not in ("queued", "running"):
                        st["status"] = "queued"
                        return self._send(200, "requeued")
                    return self._send(409, "still active")
            except Exception as e:
                return self._send(500, str(e))
        if path == "/api/clear":
            app = _State.app
            try:
                with app.dq.lock:
                    done_ids = [k for k, v in app.dq.active.items()
                                if v["status"] not in ("queued", "running")]
                    for k in done_ids:
                        app.dq.active.pop(k, None)
                return self._send(200, "cleared")
            except Exception as e:
                return self._send(500, str(e))
        if path != "/api/add":
            return self._send(404, "nf")
        length = int(self.headers.get("Content-Length", 0))
        url = self.rfile.read(length).decode().strip()
        app = _State.app
        if not url or app is None:
            return self._send(400, "bad request")

        # route through the app's worker pool so it shows in the queue UI
        def job():
            import PEAK_GITHUB_true as _pg
            title = url[:60]
            state, msg = _pg.download_one(
                title, url,
                os.path.join(app.cfg.get(
                    "library_root",
                    os.path.expanduser("~/Desktop/Downloaded_Media2.0")),
                    "Music"),
                app._spfmt.get() if hasattr(app, "_spfmt") else "audio",
                (app.qkey(app._spq.get()) if hasattr(app, "_spq")
                 else "Opus 256k (guaranteed)"),
                0, browser=app.browser(),
                cookiefile=app._yt_cookiefile())
            try:
                import history_db as _h
                _h.record(title, "", "audio", "remote", state)
            except Exception:
                pass

        app.dq.submit(job, label=url[:50])
        self._send(200, "queued")

    def log_message(self, *a):
        pass


def start_server(port=8765):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    return server, f"http://{_lan_ip()}:{port}"


if __name__ == "__main__":
    srv, addr = start_server()
    print(f"remote at {addr}")
