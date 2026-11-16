"""Kobra X control page: one HTML, two hosts.

- Plugin tab (`pages` capability): talks through OrcaSlicer's `window.orca` bridge.
- "Device (Web)" tab: OrcaSlicer loads the preset's `print_host_webui` URL there, so the
  plugin serves the same page on http://127.0.0.1:<port>/ and the page uses fetch().
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("kobrax_plugin")

WEBUI_PORT = 18930

PAGE_HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kobra X</title>
<style>
  :root{--bg:var(--orca-bg,#fff);--fg:var(--orca-fg,#1f2328);--muted:var(--orca-muted,#6b7280);
        --accent:var(--orca-accent,#009688);--accent-fg:var(--orca-accent-fg,#fff);--border:var(--orca-border,#d0d7de);
        --font:var(--orca-font,system-ui,-apple-system,"Segoe UI",sans-serif)}
  @media (prefers-color-scheme:dark){:root:not([data-orca]){--bg:#1e1f22;--fg:#e6e6e6;--muted:#9aa0a6;--border:#3a3b3f}}
  *{box-sizing:border-box}
  body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 var(--font)}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  h1{font-size:18px;margin:0}
  .pill{padding:2px 10px;border-radius:999px;border:1px solid var(--border);font-size:12px;color:var(--muted)}
  .pill.ok{border-color:var(--accent);color:var(--accent)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
  section{border:1px solid var(--border);border-radius:10px;padding:14px;min-width:0}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 10px}
  .row{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
  .row span:first-child{color:var(--muted)}
  .bar{height:8px;border-radius:4px;background:var(--border);overflow:hidden;margin:6px 0 10px}
  .bar>div{height:100%;background:var(--accent);width:0}
  .job{display:flex;gap:12px;align-items:flex-start}
  .job img{width:120px;max-width:40%;border-radius:6px;border:1px solid var(--border);background:#0001}
  .slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
  .slot{border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center;font-size:12px}
  .swatch{width:28px;height:28px;border-radius:50%;margin:0 auto 6px;border:1px solid var(--border)}
  .slot.off{opacity:.45}
  label{display:block;margin:8px 0 2px;color:var(--muted)}
  input[type=range]{width:100%}
  .ctl{display:flex;gap:8px;align-items:center}
  .ctl output{min-width:42px;text-align:right}
  button,select{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--fg);cursor:pointer}
  button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
  button:disabled{opacity:.5;cursor:default}
  pre{white-space:pre-wrap;word-break:break-word;font-size:12px;margin:8px 0 0;max-height:220px;overflow:auto;
      border:1px solid var(--border);border-radius:6px;padding:8px}
  .note{font-size:12px;color:var(--muted);margin-top:8px}
  #toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);padding:8px 14px;border-radius:8px;
         background:var(--fg);color:var(--bg);font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
  #toast.show{opacity:.92}
</style></head>
<body>
<header>
  <h1 id="name">Kobra X</h1>
  <span id="conn" class="pill">desconectada</span>
  <span id="meta" class="pill"></span>
</header>
<div class="grid">
  <section>
    <h2>Trabajo</h2>
    <div class="job"><img id="thumb" alt="" hidden><div style="flex:1;min-width:0">
      <div class="row"><span>Estado</span><b id="state">—</b></div>
      <div class="row"><span>Archivo</span><span id="file" style="overflow-wrap:anywhere">—</span></div>
      <div class="bar"><div id="prog"></div></div>
      <div class="row"><span>Progreso</span><span id="pct">—</span></div>
      <div class="row"><span>Capa</span><span id="layer">—</span></div>
      <div class="row"><span>Transcurrido</span><span id="elapsed">—</span></div>
      <div class="row"><span>Restante</span><span id="remain">—</span></div>
    </div></div>
  </section>
  <section>
    <h2>Temperaturas y ventiladores</h2>
    <div class="row"><span>Boquilla</span><span id="tn">—</span></div>
    <div class="row"><span>Cama</span><span id="tb">—</span></div>
    <label>Ventilador de pieza</label>
    <div class="ctl"><input type="range" min="0" max="100" step="5" data-fan="part"><output></output></div>
    <label>Ventilador auxiliar</label>
    <div class="ctl"><input type="range" min="0" max="100" step="5" data-fan="aux"><output></output></div>
    <label>Ventilador de caja</label>
    <div class="ctl"><input type="range" min="0" max="100" step="5" data-fan="box"><output></output></div>
    <p class="note">Auxiliar y caja: comando sin confirmar en la Kobra X.</p>
  </section>
  <section>
    <h2>Controles</h2>
    <label>Modo de velocidad</label>
    <div class="ctl"><select id="speed">
      <option value="1">Silencioso</option><option value="2">Estándar</option><option value="3">Sport</option>
    </select><button id="speedSet">Aplicar</button></div>
    <label>Luz</label>
    <div class="ctl"><button id="light">—</button></div>
    <p class="note">Offset de Z: pendiente hasta confirmar el formato con la captura del Slicer Next.</p>
  </section>
  <section>
    <h2>Filamento</h2>
    <div class="slots" id="slots"></div>
  </section>
  <section>
    <h2>Parámetros del trabajo</h2>
    <div id="params"><span class="note">Sin datos.</span></div>
    <div class="ctl" style="margin-top:8px"><button id="jobRefresh">Actualizar</button></div>
  </section>
  <section>
    <h2>Archivos en la impresora</h2>
    <div class="ctl"><button id="filesLocal">Memoria interna</button><button id="filesUdisk">Pendrive</button></div>
    <pre id="files" hidden></pre>
    <p class="note">Formato de la lista sin confirmar: se muestra la respuesta tal cual.</p>
  </section>
</div>
<p class="note" id="foot"></p>
<div id="toast"></div>
<script>
const bridge = window.orca && typeof window.orca.postMessage === "function";
if (bridge) document.documentElement.setAttribute("data-orca", "");
let snap = null, thumbKey = null, fanBusy = {};
const $ = id => document.getElementById(id);
const fmtMin = m => (m == null ? "—" : (m >= 60 ? Math.floor(m/60) + " h " + (m%60) + " min" : m + " min"));
const temp = (c, t) => c == null ? "—" : Math.round(c) + " °C" + (t ? " → " + Math.round(t) + " °C" : "");

function toast(text){ const t=$("toast"); t.textContent=text; t.classList.add("show"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),2600); }

function send(msg){
  if (bridge) { window.orca.postMessage(msg); return; }
  const isState = msg.type === "state";
  fetch(isState ? "api/state" : "api/action", isState ? {} :
        {method:"POST", headers:{"Content-Type":"application/json","X-Kobrax":"1"}, body:JSON.stringify(msg)})
    .then(r => r.json()).then(handle).catch(()=>{ $("conn").textContent="sin plugin"; $("conn").className="pill"; });
}
function act(action, extra){ send(Object.assign({type:"action", action}, extra || {})); }

function handle(msg){
  if (!msg) return;
  if (msg.type === "state") render(msg.data);
  else if (msg.type === "result") { if (msg.thumbnail) { $("thumb").src = "data:image/png;base64," + msg.thumbnail; $("thumb").hidden = false; }
                                    else toast(msg.ok ? (msg.message || "Enviado") : ("Error: " + msg.error)); }
}
if (bridge) window.orca.onMessage(handle);

function render(s){
  snap = s;
  $("name").textContent = s.name || "Kobra X";
  $("conn").textContent = s.connected ? "conectada" : "desconectada";
  $("conn").className = "pill" + (s.connected ? " ok" : "");
  $("meta").textContent = [s.host, s.version && ("fw " + s.version)].filter(Boolean).join(" · ");
  $("state").textContent = s.state_label;
  $("file").textContent = s.filename || "—";
  $("prog").style.width = (s.progress || 0) + "%";
  $("pct").textContent = s.progress == null ? "—" : s.progress + " %";
  $("layer").textContent = s.layer == null ? "—" : s.layer + " / " + (s.total_layers ?? "?");
  $("elapsed").textContent = fmtMin(s.elapsed_min);
  $("remain").textContent = fmtMin(s.remain_min);
  $("tn").textContent = temp(s.temps.nozzle, s.temps.nozzle_target);
  $("tb").textContent = temp(s.temps.bed, s.temps.bed_target);
  document.querySelectorAll("input[data-fan]").forEach(el => {
    const v = s.fans[el.dataset.fan];
    if (!fanBusy[el.dataset.fan] && v != null) el.value = v;
    el.nextElementSibling.textContent = (v == null ? "—" : v + " %");
  });
  if (document.activeElement !== $("speed") && s.speed) $("speed").value = s.speed;
  $("light").textContent = s.light == null ? "—" : (s.light ? "Apagar" : "Prender");
  $("slots").innerHTML = (s.slots || []).map(x =>
    `<div class="slot ${x.loaded ? "" : "off"}"><div class="swatch" style="background:#${x.color}"></div>
     ${x.index + 1}<br><b>${x.loaded ? (x.type || "?") : "vacío"}</b></div>`).join("") || '<span class="note">Sin datos.</span>';
  const p = s.job || {};
  const keys = Object.keys(p);
  $("params").innerHTML = keys.length ? keys.map(k => `<div class="row"><span>${k}</span><span>${p[k]}</span></div>`).join("")
                                      : '<span class="note">Sin datos (se cargan durante una impresión).</span>';
  if (s.thumb_key && s.thumb_key !== thumbKey) { thumbKey = s.thumb_key; act("thumbnail"); }
  if (!s.thumb_key) $("thumb").hidden = true;
  const f = s.files || {};
  const last = Object.keys(f).sort((a,b)=> (f[b].time||"").localeCompare(f[a].time||""))[0];
  if (last) { $("files").hidden = false; $("files").textContent = last + " · code " + f[last].code + " · " + (f[last].time || "") + "\n" + JSON.stringify(f[last].data, null, 2); }
  $("foot").textContent = "Plugin kobrax " + s.plugin_version + (s.webui_url ? " · Device (Web): " + s.webui_url : "");
  ["speedSet","light","jobRefresh","filesLocal","filesUdisk"].forEach(id => $(id).disabled = !s.connected);
}

document.querySelectorAll("input[data-fan]").forEach(el => {
  el.addEventListener("input", () => { fanBusy[el.dataset.fan] = true; el.nextElementSibling.textContent = el.value + " %"; });
  el.addEventListener("change", () => { act("fan", {fan: el.dataset.fan, pct: +el.value}); setTimeout(()=>fanBusy[el.dataset.fan]=false, 6000); });
});
$("speedSet").onclick = () => act("speed", {level: +$("speed").value});
$("light").onclick = () => act("light", {on: !(snap && snap.light)});
$("jobRefresh").onclick = () => act("job");
$("filesLocal").onclick = () => act("files", {root: "local"});
$("filesUdisk").onclick = () => act("files", {root: "udisk"});

send({type:"state"});
setInterval(() => send({type:"state"}), 2000);
</script>
</body></html>
"""


Handler = Callable[[dict[str, Any]], dict[str, Any]]


class LocalWebUI:
    """Serves PAGE_HTML and a tiny JSON API on 127.0.0.1 for the "Device (Web)" tab.

    POSTs must carry the non-simple header `X-Kobrax: 1`: a web page in a browser cannot send
    it cross-origin without a CORS preflight, which this server never approves, so other sites
    cannot drive the printer through it. The Host header is checked against DNS rebinding.
    """

    def __init__(self, get_state: Callable[[], dict[str, Any]], on_action: Handler, port: int = WEBUI_PORT):
        self._get_state = get_state
        self._on_action = on_action
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/" if self._server else ""

    def start(self) -> bool:
        if self._server is not None:
            return True
        ui = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # keep OrcaSlicer's log clean
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj: Any, code: int = 200) -> None:
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _host_ok(self) -> bool:
                return self.headers.get("Host", "") in (f"127.0.0.1:{ui.port}", f"localhost:{ui.port}")

            def do_GET(self) -> None:  # noqa: N802
                if not self._host_ok():
                    return self._json({"error": "host"}, 403)
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    return self._send(200, PAGE_HTML.encode(), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json({"type": "state", "data": ui._get_state()})
                return self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                if not self._host_ok() or self.headers.get("X-Kobrax") != "1":
                    return self._json({"error": "forbidden"}, 403)
                if self.path.split("?", 1)[0] != "/api/action":
                    return self._json({"error": "not found"}, 404)
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                    msg = json.loads(self.rfile.read(length) or b"{}")
                    return self._json(ui._on_action(msg if isinstance(msg, dict) else {}))
                except ValueError:
                    return self._json({"type": "result", "ok": False, "error": "JSON inválido"}, 400)

        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as err:
            log.warning("local web UI not started on port %d: %s", self.port, err)
            return False
        server.daemon_threads = True
        self.port = server.server_address[1]  # resolves port 0 (tests)
        self._server = server
        threading.Thread(target=server.serve_forever, name="kobrax-webui", daemon=True).start()
        log.info("Device (Web) page at %s", self.url)
        return True

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


def make_page_capability(orca: Any, get_agent: Callable[[], Any]) -> type | None:
    """The plugin tab, if this OrcaSlicer has the `pages` capability type."""
    pages = getattr(orca, "pages", None)
    if pages is None or not hasattr(pages, "PagesPluginCapabilityBase"):
        return None

    class KobraXPage(pages.PagesPluginCapabilityBase):
        def get_name(self) -> str:
            return "Kobra X"

        def get_ui(self) -> str:
            return PAGE_HTML

        def on_message(self, data: Any) -> None:
            agent = get_agent()
            if not isinstance(data, dict):
                return
            if data.get("type") == "state":
                state = agent.page_snapshot() if agent else {"connected": False, "temps": {}, "fans": {}}
                self.post_message({"type": "state", "data": state})
            elif data.get("type") == "action":
                result = agent.page_action(data) if agent else {"type": "result", "ok": False, "error": "agente no cargado"}
                self.post_message(result)

    return KobraXPage
