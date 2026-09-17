#!/usr/bin/env python3
"""figma-bridge — Figma REST API CLI (B route). Standard library only.

Each subcommand is independent: it does one thing and prints JSON to stdout.
Token is read from FIGMA_PERSONAL_TOKEN (env or ~/.claude/secrets/.env).
"""
import argparse
import base64
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.figma.com"
ENV_PATH = os.path.expanduser("~/.claude/secrets/.env")
TOKEN_KEY = "FIGMA_PERSONAL_TOKEN"
RELAY_PORT = int(os.getenv("FIGMA_RELAY_PORT", "3055"))
RELAY = f"http://127.0.0.1:{RELAY_PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_TOKEN_FILE = os.path.expanduser("~/.figma-bridge/relay-token")
INDEX_DIR = os.path.expanduser("~/.figma-bridge/index")

_SAVE_TO = None  # set once from --save; emit() honours it


# --- plumbing ---------------------------------------------------------------

def find_token():
    token = os.getenv(TOKEN_KEY)
    if token:
        return token
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{TOKEN_KEY}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def load_token():
    token = find_token()
    if not token:
        sys.exit(f"{TOKEN_KEY} not found. Add it to {ENV_PATH}")
    return token


def parse_target(s):
    """Figma URL or bare file key -> (file_key, node_id or None)."""
    if "figma.com" not in s:
        return s, None
    m = re.search(r"figma\.com/(?:file|design|board|slides|make)/([0-9A-Za-z]{10,128})", s)
    if not m:
        # Raised, not exited: this also runs inside the server, where exiting
        # would kill the request thread without a response.
        raise ValueError(f"no file key in URL: {s}")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(s).query)
    node = query.get("node-id", [None])[0]
    return m.group(1), node.replace("-", ":") if node else None


def api(path, token, params=None, method="GET", body=None):
    url = API + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Figma-Token": token}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        # A whole-file read can be megabytes; without a timeout a stalled
        # connection hangs the command forever.
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 429:
            sys.exit(f"429 rate limited. Retry-After={e.headers.get('Retry-After')}s :: {detail}")
        if e.code == 403:
            sys.exit(f"403 forbidden — token scope or plan gate. {detail}")
        sys.exit(f"HTTP {e.code}: {detail}")


def download(url, path):
    with urllib.request.urlopen(url) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def save_result(obj, path):
    """Write the full result to disk and return the summary that goes to stdout
    instead. The point is to keep a large tree out of the caller's context, so
    read it back with a grep or a slice — not whole, which defeats the purpose."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    summary = {"saved": os.path.abspath(path), "bytes": os.path.getsize(path)}
    if isinstance(obj, dict):
        summary["keys"] = sorted(obj)
    return summary


def emit(obj):
    json.dump(save_result(obj, _SAVE_TO) if _SAVE_TO else obj,
              sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# --- normalization ----------------------------------------------------------
# Shared shape with the future plugin (C) route, so callers see one schema.
# Keys are omitted (not null) when the source cannot provide them.

def _hex(c):
    return "#%02X%02X%02X" % tuple(round(c[k] * 255) for k in ("r", "g", "b"))


def norm_paint(p):
    if p.get("type") == "SOLID":
        out = {"type": "SOLID", "hex": _hex(p["color"])}
        if p.get("opacity", 1) != 1:
            out["opacity"] = round(p["opacity"], 3)
        return out
    return {"type": p.get("type")}


def norm_node(n, depth=None):
    out = {"id": n.get("id"), "name": n.get("name"), "type": n.get("type")}

    box = n.get("absoluteBoundingBox")
    if box:
        out["box"] = {k: round(box[k], 2) for k in ("x", "y", "width", "height")
                      if box.get(k) is not None}

    if n.get("layoutMode") and n["layoutMode"] != "NONE":
        layout = {
            "mode": n["layoutMode"],
            "padding": [n.get("paddingTop", 0), n.get("paddingRight", 0),
                        n.get("paddingBottom", 0), n.get("paddingLeft", 0)],
        }
        if n.get("itemSpacing") is not None:
            layout["gap"] = n["itemSpacing"]
        out["layout"] = layout

    for key in ("fills", "strokes"):
        paints = [norm_paint(p) for p in (n.get(key) or []) if p.get("visible", True)]
        if paints:
            out[key] = paints

    if n.get("cornerRadius") is not None:
        out["cornerRadius"] = n["cornerRadius"]
    if n.get("effects"):
        out["effects"] = [e.get("type") for e in n["effects"] if e.get("visible", True)]

    if n.get("characters") is not None:
        out["text"] = n["characters"]
    style = n.get("style") or {}
    picked = {k: style[k] for k in ("fontFamily", "fontWeight", "fontSize", "lineHeightPx",
                                    "letterSpacing", "textAlignHorizontal") if style.get(k) is not None}
    if picked:
        out["style"] = picked

    if n.get("boundVariables"):
        out["boundVariables"] = sorted(n["boundVariables"].keys())
    if n.get("componentProperties"):
        out["componentProperties"] = {k: v.get("value") for k, v in n["componentProperties"].items()}
    if n.get("componentId"):
        out["componentId"] = n["componentId"]
    if n.get("interactions"):
        out["interactionCount"] = len(n["interactions"])
    if n.get("visible") is False:
        out["visible"] = False
    if n.get("opacity") is not None and n["opacity"] != 1:
        out["opacity"] = n["opacity"]

    kids = n.get("children") or []
    if kids and (depth is None or depth > 0):
        out["children"] = [norm_node(k, None if depth is None else depth - 1) for k in kids]
    elif kids:
        out["childCount"] = len(kids)
    return out


# --- commands ---------------------------------------------------------------

def cmd_ping(args, token):
    me = api("/v1/me", token)
    emit({"_source": "rest", "ok": True, "handle": me.get("handle"), "email": me.get("email")})


def cmd_tree(args, token):
    key, node = parse_target(args.target)
    node = args.node or node
    if node:
        data = api(f"/v1/files/{key}/nodes", token,
                   {"ids": node, "depth": args.depth, "geometry": "paths" if args.geometry else None})
        entry = (data.get("nodes") or {}).get(node)
        if not entry:
            sys.exit(f"node {node} not found in file {key}")
        doc, name = entry["document"], data.get("name")
    else:
        data = api(f"/v1/files/{key}", token,
                   {"depth": args.depth, "geometry": "paths" if args.geometry else None})
        doc, name = data["document"], data.get("name")
    if args.raw:
        emit(doc)
    else:
        emit({"_source": "rest", "file": name, "fileKey": key, "node": norm_node(doc, args.prune)})


def cmd_render(args, token):
    key, node = parse_target(args.target)
    ids = args.node or node
    if not ids:
        sys.exit("render needs a node: use a URL with node-id or pass --node")
    data = api(f"/v1/images/{key}", token, {
        "ids": ids, "format": args.format, "scale": args.scale,
        "use_absolute_bounds": "true" if args.absolute else None,
    })
    images = data.get("images") or {}
    result = {"_source": "rest", "fileKey": key, "images": images}
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        saved = {}
        for nid, url in images.items():
            if not url:
                saved[nid] = None
                continue
            saved[nid] = download(url, os.path.join(
                args.out, f"{nid.replace(':', '-')}.{args.format}"))
        result["saved"] = saved
    emit(result)


def cmd_assets(args, token):
    key, _ = parse_target(args.target)
    images = (api(f"/v1/files/{key}/images", token).get("meta") or {}).get("images", {})
    result = {"_source": "rest", "fileKey": key, "count": len(images), "images": images}
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        result["saved"] = [download(url, os.path.join(args.out, f"{ref}.png"))
                           for ref, url in images.items() if url]
    emit(result)


def cmd_comments(args, token):
    key, node = parse_target(args.target)
    if args.post:
        body = {"message": args.post}
        if args.reply_to:
            body["comment_id"] = args.reply_to
        elif node:
            body["client_meta"] = {"node_id": node, "node_offset": {"x": 0, "y": 0}}
        emit({"_source": "rest", "posted": api(f"/v1/files/{key}/comments", token,
                                               method="POST", body=body)})
        return
    data = api(f"/v1/files/{key}/comments", token)
    emit({"_source": "rest", "fileKey": key, "comments": [
        {"id": c.get("id"), "user": (c.get("user") or {}).get("handle"),
         "message": c.get("message"), "at": c.get("created_at"),
         "resolved": bool(c.get("resolved_at")), "parent": c.get("parent_id") or None,
         "node": ((c.get("client_meta") or {}).get("node_id"))}
        for c in data.get("comments", [])]})


def cmd_meta(args, token):
    key, _ = parse_target(args.target)
    emit({"_source": "rest", **api(f"/v1/files/{key}/meta", token)})


# --- relay (C route: local Figma plugin) ------------------------------------
# The plugin cannot open a socket from its main thread, so its UI iframe polls
# this relay over HTTP. Bound to 127.0.0.1 only.

class _Relay:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = []
        self.results = {}
        self.events = {}
        self.last_poll = 0.0
        self.plugin = {}
        self.token = None
        self.abandoned = set()

    def submit(self, code, timeout):
        job = {"id": uuid.uuid4().hex, "code": code}
        done = threading.Event()
        with self.lock:
            self.jobs.append(job)
            self.events[job["id"]] = done

        arrived = done.wait(timeout)
        with self.lock:
            self.events.pop(job["id"], None)
            result = self.results.pop(job["id"], None)
            if result is None:
                # Drop it from the queue so a plugin that reconnects later does
                # not run it, and remember to discard a result that still lands.
                self.jobs = [j for j in self.jobs if j["id"] != job["id"]]
                self.abandoned.add(job["id"])
        if result is not None:
            return result
        return {"ok": False, "error": "result lost" if arrived else f"no result within {timeout}s"}

    def take(self, meta):
        with self.lock:
            self.last_poll = time.time()
            if meta:
                self.plugin = meta
            return self.jobs.pop(0) if self.jobs else None

    def finish(self, payload):
        job_id = payload.get("id")
        with self.lock:
            if job_id in self.abandoned:
                self.abandoned.discard(job_id)  # nobody is waiting any more
                return
            self.results[job_id] = payload
            done = self.events.get(job_id)
        if done:
            done.set()

    def status(self):
        with self.lock:
            age = time.time() - self.last_poll if self.last_poll else None
            return {
                "pluginConnected": age is not None and age < 5,
                "lastPollAgo": round(age, 1) if age is not None else None,
                "file": self.plugin,
                "queued": len(self.jobs),
            }


STATE = _Relay()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, content_type="application/json", cors=True):
        body = raw if raw is not None else (
            b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode())
        self.send_response(code)
        if cors:
            # The plugin iframe has a null origin, so it needs a wildcard. Access is
            # gated by X-Relay-Token, not by this header — see _authorized().
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Relay-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_page(self):
        """The page carries the token, so it must never be readable cross-origin:
        sent with no CORS headers, which makes another site's fetch opaque."""
        try:
            with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
                html = f.read()
        except OSError as e:
            self._send(500, {"error": str(e)}, cors=False)
            return
        html = html.replace("__RELAY_TOKEN__", STATE.token or "")
        self._send(200, raw=html.encode(), content_type="text/html; charset=utf-8", cors=False)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _host_ok(self):
        """Blocks DNS rebinding: a name the attacker controls that resolves to
        127.0.0.1 would otherwise be same-origin, making CORS useless and the
        token in the served page readable. Applies to every route, the HTML
        page included, because that page carries the token.
        """
        host = (self.headers.get("Host") or "").strip()
        if host in (f"127.0.0.1:{RELAY_PORT}", f"localhost:{RELAY_PORT}", f"[::1]:{RELAY_PORT}"):
            return True
        self._send(403, {"error": f"unexpected Host: {host}"}, cors=False)
        return False

    def _authorized(self):
        """This endpoint executes code, so a page the user visits must not reach it.

        CORS alone does not help: it gates reading the response, not delivering the
        request, and a sandboxed iframe can forge `Origin: null`. The shared secret
        forces a preflight the attacker cannot satisfy.
        """
        if not self._host_ok():
            return False
        if not STATE.token or self.headers.get("X-Relay-Token") != STATE.token:
            self._send(403, {"error": "missing or wrong X-Relay-Token"})
            return False
        return True

    def do_OPTIONS(self):
        if self._host_ok():
            self._send(204)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if self._host_ok():
                self._send_page()
            return
        if not self._authorized():
            return
        path, _, query = self.path.partition("?")
        if path == "/status":
            self._send(200, STATE.status())
        elif path == "/files":
            self._send(200, {"files": list_files(wants_verify(query)), "hidden": read_hidden()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        try:
            self._route_post()
        except ValueError as e:      # bad URL from the page, not a server fault
            self._send(400, {"error": str(e)})

    def _route_post(self):
        if self.path == "/poll":
            job = STATE.take(self._read())
            self._send(200, job) if job else self._send(204)
        elif self.path == "/result":
            STATE.finish(self._read())
            self._send(200, {"ok": True})
        elif self.path == "/job":
            payload = self._read()
            self._send(200, STATE.submit(payload.get("code", ""), payload.get("timeout", 60)))
        elif self.path == "/open":
            payload = self._read()
            self._send(200, open_in_figma(payload.get("key"), payload.get("autorun", False),
                                          float(payload.get("wait", 6.0))))
        elif self.path == "/favorites":
            self._send(200, {"favorites": edit_favorites(self._read())})
        elif self.path == "/hidden":
            self._send(200, {"hidden": edit_hidden(self._read())})
        elif self.path == "/clean":
            self._send(200, clean_files())
        else:
            self._send(404, {"error": "not found"})


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    """`localhost` resolves to ::1 first on macOS, so listen there too.

    Each listener binds a loopback address explicitly — never "::" or "0.0.0.0",
    which would expose an endpoint that executes code to the network.
    """
    address_family = socket.AF_INET6


def read_relay_token():
    try:
        with open(RELAY_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def relay_call(path, payload=None, timeout=70):
    secret = read_relay_token()
    if not secret:
        sys.exit(f"no relay token at {RELAY_TOKEN_FILE} — start the app: python3 {__file__} serve")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(RELAY + path, data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          "X-Relay-Token": secret})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return None if r.status == 204 else json.load(r)
    except urllib.error.URLError as e:
        sys.exit(f"relay unreachable at {RELAY} — start it: python3 {__file__} serve  ({e})")


def cmd_serve(args, token=None):
    # Reuse the stored token so the plugin does not ask again on every restart.
    stored = None if getattr(args, "rotate", False) else read_relay_token()
    STATE.token = stored or uuid.uuid4().hex
    os.makedirs(os.path.dirname(RELAY_TOKEN_FILE), mode=0o700, exist_ok=True)
    with open(os.open(RELAY_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(STATE.token)

    servers = [_Server(("127.0.0.1", RELAY_PORT), _Handler)]
    try:
        servers.append(_Server6(("::1", RELAY_PORT), _Handler))
    except OSError as e:
        print(f"note: no IPv6 loopback listener ({e}) — localhost must fall back to 127.0.0.1")

    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    listening = ", ".join(f"{s.server_address[0]}:{RELAY_PORT}" for s in servers)
    print(f"listening on {listening} — open {RELAY}/ for the file list. Ctrl-C to stop.")
    print(f"plugin: import {os.path.join(HERE, 'plugin')} in Figma (Plugins > Development)")
    print(f"\nplugin token (paste it into the plugin window once):\n  {STATE.token}\n")
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def same_file(open_name, wanted_name):
    """None means 'cannot tell' — the caller warns instead of blocking."""
    if not open_name or not wanted_name:
        return None
    return open_name.strip() == wanted_name.strip()


def run_js(code, target=None, timeout=60):
    status = relay_call("/status")
    if not status["pluginConnected"]:
        sys.exit("plugin not connected — open the file in the Figma desktop app and run the "
                 "figma-bridge plugin. For files that are not open, use the official MCP use_figma.")

    if target:
        # The plugin cannot report a file key (figma.fileKey does not exist and
        # figma.root.id is "0:0" everywhere), so the name is all both sides share.
        key, _ = parse_target(target)
        open_name = (status.get("file") or {}).get("fileName")
        verdict = same_file(open_name, file_meta(key)[1])
        if verdict is False:
            sys.exit(f"플러그인이 붙어 있는 파일은 '{open_name}' 입니다 — 요청한 파일이 아닙니다. "
                     f"Figma에서 대상 파일을 열고 플러그인을 실행하세요.")
        if verdict is None:
            print(f"경고: 파일 대조를 못 했습니다 (열린 파일: {open_name}). "
                  f"이름이 맞는지 직접 확인하세요.", file=sys.stderr)

    return relay_call("/job", {"code": code, "timeout": timeout}, timeout=timeout + 10)


def run_js_emit(code, target=None, timeout=60):
    emit({"_source": "plugin", **run_js(code, target, timeout)})


def cmd_exec(args, token=None):
    code = open(args.file, encoding="utf-8").read() if args.file else args.code
    if not code:
        sys.exit("exec needs --code or --file")
    run_js_emit(code, args.target, args.timeout)


JS_TOKENS = """
const collections = await figma.variables.getLocalVariableCollectionsAsync();
const variables = [];
for (const c of collections) {
  for (const id of c.variableIds) {
    const v = await figma.variables.getVariableByIdAsync(id);
    variables.push({
      collection: c.name, name: v.name, type: v.resolvedType,
      scopes: v.scopes,
      values: c.modes.map(m => ({ mode: m.name, value: v.valuesByMode[m.modeId] })),
    });
  }
}
const paint = await figma.getLocalPaintStylesAsync();
const text = await figma.getLocalTextStylesAsync();
return {
  collections: collections.map(c => ({ name: c.name, modes: c.modes.map(m => m.name) })),
  variables,
  paintStyles: paint.map(s => s.name),
  textStyles: text.map(s => ({ name: s.name, size: s.fontSize, font: s.fontName })),
};
"""

JS_SELECTION = """
return figma.currentPage.selection.map(n => ({
  id: n.id, name: n.name, type: n.type,
  x: Math.round(n.x), y: Math.round(n.y),
  width: Math.round(n.width), height: Math.round(n.height),
}));
"""

JS_PAGES = """
// documentAccess is dynamic-page, so only the current page's children are loaded.
const current = figma.currentPage.id;
return figma.root.children.map(p => {
  const out = { id: p.id, name: p.name, current: p.id === current };
  if (p.id === current) out.children = p.children.length;
  return out;
});
"""


def cmd_tokens(args, token=None):
    run_js_emit(JS_TOKENS, args.target, 60)


def cmd_selection(args, token=None):
    run_js_emit(JS_SELECTION, args.target, 30)


def cmd_pages(args, token=None):
    run_js_emit(JS_PAGES, args.target, 30)


# --- wrappers ---------------------------------------------------------------
# Thin shells over exec: the JS is the same code you would write by hand, with
# arguments injected as a JSON literal so quoting can never break out.

JS_FIND = """
const P = %s;
const page = figma.currentPage;
const nodes = P.types.length
  ? page.findAllWithCriteria({ types: P.types })   // indexed lookup, far faster
  : page.findAll(function () { return true; });
const needle = (P.name || "").toLowerCase();
const hits = needle
  ? nodes.filter(function (n) { return n.name.toLowerCase().indexOf(needle) !== -1; })
  : nodes;
return {
  total: hits.length,
  nodes: hits.slice(0, P.limit).map(function (n) {
    const out = { id: n.id, name: n.name, type: n.type };
    if ("x" in n) out.box = { x: Math.round(n.x), y: Math.round(n.y),
                              width: Math.round(n.width), height: Math.round(n.height) };
    return out;
  })
};
"""

JS_INSPECT = """
const node = await figma.getNodeByIdAsync(%s);
if (!node) return { error: "node not found on this page" };

function hex(c) {
  const v = function (x) { return ("0" + Math.round(x * 255).toString(16)).slice(-2).toUpperCase(); };
  return "#" + v(c.r) + v(c.g) + v(c.b);
}
function paints(list) {
  if (!list || list === figma.mixed) return undefined;
  return list.filter(function (p) { return p.visible !== false; })
             .map(function (p) { return p.type === "SOLID" ? { type: "SOLID", hex: hex(p.color) } : { type: p.type }; });
}

const out = { id: node.id, name: node.name, type: node.type };
if ("x" in node) out.box = { x: Math.round(node.x), y: Math.round(node.y),
                             width: Math.round(node.width), height: Math.round(node.height) };
if (node.layoutMode && node.layoutMode !== "NONE") {
  out.layout = { mode: node.layoutMode, gap: node.itemSpacing,
                 padding: [node.paddingTop, node.paddingRight, node.paddingBottom, node.paddingLeft] };
}
const fills = paints(node.fills), strokes = paints(node.strokes);
if (fills && fills.length) out.fills = fills;
if (strokes && strokes.length) out.strokes = strokes;
if ("cornerRadius" in node && node.cornerRadius !== figma.mixed) out.cornerRadius = node.cornerRadius;
if ("characters" in node) {
  out.text = node.characters;
  if (node.fontSize !== figma.mixed) out.fontSize = node.fontSize;
  if (node.fontName !== figma.mixed) out.font = node.fontName;
}
if (node.boundVariables && Object.keys(node.boundVariables).length) {
  out.boundVariables = Object.keys(node.boundVariables);
}
if (node.type === "INSTANCE") {
  const main = await node.getMainComponentAsync();
  if (main) out.mainComponent = main.name;
  out.componentProperties = Object.keys(node.componentProperties || {});
}
if ("children" in node) out.childCount = node.children.length;
if (node.visible === false) out.visible = false;

// Why is this node the size it is? The answer lives in the parent's auto-layout
// and this node's own sizing, so a lone node is never enough to judge by.
for (const key of ["layoutSizingHorizontal", "layoutSizingVertical", "layoutAlign", "layoutGrow"]) {
  try {
    const v = node[key];
    if (v !== undefined && v !== figma.mixed) out[key] = v;
  } catch (e) { /* not in an auto-layout context */ }
}

function layoutBits(n) {
  const b = { id: n.id, name: n.name, type: n.type };
  if (n.layoutMode && n.layoutMode !== "NONE") {
    b.layout = { mode: n.layoutMode, gap: n.itemSpacing,
                 padding: [n.paddingTop, n.paddingRight, n.paddingBottom, n.paddingLeft] };
  }
  if ("clipsContent" in n && n.clipsContent) b.clipsContent = true;
  return b;
}

const parents = [];
let up = node.parent;
while (up && parents.length < 3 && up.type !== "PAGE" && up.type !== "DOCUMENT") {
  parents.push(layoutBits(up));   // nearest first
  up = up.parent;
}
if (parents.length) out.parents = parents;
while (up && up.type !== "PAGE" && up.type !== "DOCUMENT") up = up.parent;
if (up && up.type === "PAGE") out.page = up.name;
return out;
"""

JS_COMPONENTS = """
const found = figma.currentPage.findAllWithCriteria({ types: ["COMPONENT", "COMPONENT_SET"] });
return found.map(function (n) {
  const out = { id: n.id, name: n.name, type: n.type };
  if (n.type === "COMPONENT_SET") {
    out.variants = n.children.map(function (c) { return c.name; });
    out.properties = Object.keys(n.componentPropertyDefinitions || {});
  } else if (n.parent && n.parent.type === "COMPONENT_SET") {
    // Reading definitions off a variant throws; the set owns them.
    out.variantOf = n.parent.name;
  } else {
    out.properties = Object.keys(n.componentPropertyDefinitions || {});
  }
  if (n.description) out.description = n.description;
  return out;
});
"""

JS_EXPORT = """
const P = %s;
const node = await figma.getNodeByIdAsync(P.node);
if (!node) return { error: "node not found on this page" };
const settings = P.format === "SVG"
  ? { format: "SVG_STRING" }
  : { format: P.format, constraint: { type: "SCALE", value: P.scale } };
const data = await node.exportAsync(settings);
if (typeof data === "string") return { format: "SVG", text: data, name: node.name };
if (typeof figma.base64Encode !== "function") {
  return { error: "figma.base64Encode 없음 — REST render를 쓰세요" };
}
return { format: P.format, base64: figma.base64Encode(data), name: node.name };
"""

JS_TEXT = """
return figma.currentPage.findAllWithCriteria({ types: ["TEXT"] }).map(function (n) {
  return { id: n.id, name: n.name, text: n.characters };
});
"""

JS_TEXT_REPLACE = """
const P = %s;
const nodes = figma.currentPage.findAllWithCriteria({ types: ["TEXT"] });
const changed = [];
for (const n of nodes) {
  if (n.characters.indexOf(P.from) === -1) continue;
  // Every font in the node must be loaded before its characters can change.
  for (const seg of n.getStyledTextSegments(["fontName"])) {
    await figma.loadFontAsync(seg.fontName);
  }
  n.characters = n.characters.split(P.from).join(P.to);
  changed.push({ id: n.id, text: n.characters });
}
return { changed: changed.length, nodes: changed };
"""


def node_from(args):
    node = getattr(args, "node", None)
    if not node and args.target:
        node = parse_target(args.target)[1]
    return node


def cmd_find(args, token=None):
    types = [t.strip().upper() for t in (args.type or "").split(",") if t.strip()]
    params = {"name": args.name, "types": types, "limit": args.limit}
    run_js_emit(JS_FIND % json.dumps(params), args.target, 60)


def cmd_inspect(args, token=None):
    node = node_from(args)
    if not node:
        sys.exit("inspect needs --node or a URL containing node-id")
    run_js_emit(JS_INSPECT % json.dumps(node), args.target, 30)


def cmd_components(args, token=None):
    run_js_emit(JS_COMPONENTS, args.target, 60)


def save_export(value, out_dir, node):
    os.makedirs(out_dir, exist_ok=True)
    stem = node.replace(":", "-")
    if value.get("format") == "SVG":
        path = os.path.join(out_dir, stem + ".svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(value["text"])
    else:
        path = os.path.join(out_dir, f"{stem}.{value['format'].lower()}")
        with open(path, "wb") as f:
            f.write(base64.b64decode(value["base64"]))
    return path


def cmd_export(args, token=None):
    node = node_from(args)
    if not node:
        sys.exit("export needs --node or a URL containing node-id")
    params = {"node": node, "format": args.format.upper(), "scale": args.scale}
    out = run_js(JS_EXPORT % json.dumps(params), args.target, 60)
    value = out.get("value") or {}
    if not out.get("ok") or value.get("error"):
        emit({"_source": "plugin", **out})
        return
    emit({"_source": "plugin", "name": value.get("name"),
          "saved": save_export(value, args.out, node)})


def cmd_text(args, token=None):
    if args.replace:
        params = {"from": args.replace[0], "to": args.replace[1]}
        run_js_emit(JS_TEXT_REPLACE % json.dumps(params), args.target, 120)
    else:
        run_js_emit(JS_TEXT, args.target, 60)


# --- file list, opening, launch agent -----------------------------------------

FAVORITES_FILE = os.path.expanduser("~/.figma-bridge/favorites.json")
HIDDEN_FILE = os.path.expanduser("~/.figma-bridge/hidden.json")
FIGMA_SETTINGS = os.path.expanduser("~/Library/Application Support/Figma/settings.json")
AGENT_LABEL = "dev.jsyoo.figma-bridge"
AGENT_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")


def parse_tabs(settings):
    """Figma desktop records every open tab, which is the closest thing to a file
    list that exists: REST has no endpoint for a user's own files, and drafts are
    not in the API model at all."""
    found = {}
    for window in settings.get("windows", []):
        for tab in window.get("tabs", []):
            match = re.match(r"^/file/([0-9A-Za-z]{10,128})", tab.get("path") or "")
            if not match or tab.get("isDiscarded"):
                continue
            entry = {"key": match.group(1), "title": tab.get("title") or "(제목 없음)",
                     "source": "tab"}
            for src, dst in (("editorType", "editorType"), ("lastViewedAt", "lastViewedAt")):
                if tab.get(src) is not None:
                    entry[dst] = tab[src]
            thumb = (tab.get("thumbnail") or {}).get("url")
            if thumb:
                entry["thumbnail"] = thumb
            if tab.get("isPinned"):
                entry["pinnedInFigma"] = True
            prior = found.get(entry["key"])
            if not prior or entry.get("lastViewedAt", 0) > prior.get("lastViewedAt", 0):
                found[entry["key"]] = entry
    return list(found.values())


def wants_verify(query):
    """`?verify` carries no value, and parse_qs drops blank values by default —
    which silently turned every verify request into a plain listing."""
    return "verify" in urllib.parse.parse_qs(query, keep_blank_values=True)


def _read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def read_favorites():
    return _read_json(FAVORITES_FILE, [])


def edit_favorites(payload):
    favorites = read_favorites()
    action, key = payload.get("action"), payload.get("key")
    if action == "add":
        key, _ = parse_target(payload.get("url") or key or "")
        if key and not any(f["key"] == key for f in favorites):
            favorites.append({"key": key, "title": payload.get("title") or key})
    elif action == "remove":
        favorites = [f for f in favorites if f["key"] != key]
    _write_json(FAVORITES_FILE, favorites)
    return favorites


def read_hidden():
    return _read_json(HIDDEN_FILE, [])


def edit_hidden(payload):
    """Hiding is reversible and local: the file itself is never touched."""
    hidden = set(read_hidden())
    keys = payload.get("keys") or ([payload["key"]] if payload.get("key") else [])
    action = payload.get("action")
    if action == "hide":
        hidden |= set(keys)
    elif action == "show":
        hidden -= set(keys)
    elif action == "clear":
        hidden = set()
    _write_json(HIDDEN_FILE, sorted(hidden))
    return sorted(hidden)


def file_meta(key):
    """(exists, name). exists is None when we cannot tell — no token, no network,
    or a 403, which means 'not mine to see' rather than 'gone'."""
    token = find_token()
    if not token:
        return None, None
    req = urllib.request.Request(f"{API}/v1/files/{key}/meta", headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, (json.load(r).get("file") or {}).get("name")
    except urllib.error.HTTPError as e:
        return (False, None) if e.code == 404 else (None, None)
    except Exception:
        return None, None


def list_files(verify=False):
    tabs = parse_tabs(_read_json(FIGMA_SETTINGS, {}))

    by_key = {t["key"]: t for t in tabs}
    for fav in read_favorites():
        entry = by_key.setdefault(fav["key"], {"key": fav["key"], "title": fav["title"],
                                               "source": "favorite"})
        entry["favorite"] = True

    for key in read_hidden():
        by_key.pop(key, None)

    files = sorted(by_key.values(),
                   key=lambda f: (not f.get("favorite"), -f.get("lastViewedAt", 0)))
    if verify:
        for f in files:
            exists, _ = file_meta(f["key"])
            if exists is False:
                f["missing"] = True
            elif exists is None:
                f["unverified"] = True
    return files


# --- index: the relationships a single-node fetch cannot show -----------------

def _destinations(node):
    """Prototype targets: the older transitionNodeID and the interactions list."""
    out = []
    if node.get("transitionNodeID"):
        out.append(node["transitionNodeID"])
    for interaction in node.get("interactions") or []:
        for action in interaction.get("actions") or []:
            if action.get("destinationId"):
                out.append(action["destinationId"])
    return out


def _useful_text(value):
    """Status-bar clutter ("9:41", "100%") identifies nothing, so it is not worth
    a slot in the six texts that stand in for an unreliable layer name."""
    text = (value or "").strip()
    return len(text) >= 2 and any(ch.isalpha() for ch in text)


def build_index(data):
    """Compress a whole-file response into the relationships that a narrow fetch
    throws away: which screen owns a node, what a screen actually says, which
    components it uses, and where it leads."""
    doc = data.get("document") or {}

    # A component's own name is often just the variant ("Size=32"); the set it
    # belongs to carries the meaning.
    sets = {sid: (meta or {}).get("name")
            for sid, meta in (data.get("componentSets") or {}).items()}
    components = {}
    for cid, meta in (data.get("components") or {}).items():
        name = (meta or {}).get("name")
        set_name = sets.get((meta or {}).get("componentSetId"))
        components[cid] = f"{set_name}/{name}" if set_name and name else name

    screens, parent, label = {}, {}, {}

    def walk(node, parent_id, screen):
        nid = node.get("id")
        parent[nid] = parent_id
        label[nid] = f"{node.get('name')} ({node.get('type')})"
        if screen is not None:
            # Layer names are often meaningless ("Frame 427"); the words on the
            # screen are what actually identify it.
            if len(screen["texts"]) < 6 and _useful_text(node.get("characters")):
                screen["texts"].append(node["characters"].strip()[:40])
            cid = node.get("componentId")
            if cid and cid not in screen["components"]:
                screen["components"].append(cid)
            screen["to"].extend(_destinations(node))
        for kid in node.get("children") or []:
            walk(kid, nid, screen)

    for page in doc.get("children") or []:
        parent[page["id"]] = doc.get("id")
        label[page["id"]] = f"{page.get('name')} (PAGE)"
        for frame in page.get("children") or []:
            box = frame.get("absoluteBoundingBox") or {}
            screens[frame["id"]] = {
                "page": page.get("name"), "name": frame.get("name"),
                "box": {k: round(v) for k, v in box.items() if isinstance(v, (int, float))},
                "texts": [], "components": [], "to": [],
            }
            walk(frame, page["id"], screens[frame["id"]])

    def screen_of(node_id):
        seen = 0
        while node_id and seen < 200:      # guard against a cycle in bad data
            if node_id in screens:
                return node_id
            node_id = parent.get(node_id)
            seen += 1
        return None

    # A prototype points at some node; what matters is the screen containing it.
    for sid, screen in screens.items():
        targets = {screen_of(t) for t in screen["to"]}
        screen["to"] = sorted(t for t in targets if t and t != sid)

        # Not every top-level node is a screen — files keep reference images,
        # notes and spare frames beside the artboards. Rather than guess with a
        # filter that would drop real screens, record why each one might be one.
        screen["signals"] = [name for name, present in (
            ("text", bool(screen["texts"])),
            ("flow", bool(screen["to"])),
            ("components", bool(screen["components"])),
        ) if present]

    return {"screens": screens, "components": components,
            "parent": parent, "label": label}


def index_path(key):
    return os.path.join(INDEX_DIR, f"{key}.json")


def cmd_index(args, token):
    key, _ = parse_target(args.target)
    data = api(f"/v1/files/{key}", token)     # one tier 1 call, compressed locally
    index = build_index(data)
    index.update({"fileKey": key, "name": data.get("name"), "version": data.get("version"),
                  "indexedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    os.makedirs(INDEX_DIR, mode=0o700, exist_ok=True)
    _write_json(index_path(key), index)
    tally = {}
    for screen in index["screens"].values():
        for signal in screen["signals"] or ["bare"]:
            tally[signal] = tally.get(signal, 0) + 1
    emit({"_source": "rest", "saved": index_path(key), "file": index["name"],
          "screens": len(index["screens"]), "withSignal": tally,
          "components": len(index["components"]), "nodes": len(index["parent"]),
          "bytes": os.path.getsize(index_path(key))})


def resolve_context(index, node_id):
    """Everything a narrow fetch of this node leaves out."""
    parent, label, screens = index["parent"], index["label"], index["screens"]

    chain, cur, seen = [], parent.get(node_id), 0
    while cur and seen < 50:
        chain.append({"id": cur, "label": label.get(cur)})
        if cur in screens:
            break
        cur = parent.get(cur)
        seen += 1

    out = {"node": {"id": node_id, "label": label.get(node_id)}}
    if chain:
        out["parents"] = chain
    if node_id not in label:
        out["warning"] = "이 노드는 인덱스에 없습니다 — 인덱스가 낡았거나 다른 파일입니다"

    screen_id = node_id if node_id in screens else next(
        (c["id"] for c in chain if c["id"] in screens), None)
    if screen_id:
        screen = screens[screen_id]
        # Nested under "screen" because it describes the screen, not the node —
        # flat keys read as if the node itself used every one of these.
        out["screen"] = {"id": screen_id, "name": screen["name"],
                         "page": screen["page"], "texts": screen["texts"],
                         "signals": screen.get("signals", [])}
        if screen["components"]:
            out["screen"]["usesComponents"] = [{"id": c, "name": index["components"].get(c)}
                                               for c in screen["components"]]
        if screen["to"]:
            out["screen"]["linkedScreens"] = [{"id": t, "name": screens[t]["name"]}
                                              for t in screen["to"] if t in screens]
    return out


def cmd_context(args, token):
    key, node = parse_target(args.target)
    node = args.node or node
    if not node:
        sys.exit("context needs --node or a URL containing node-id")

    index = _read_json(index_path(key), None)
    if index is None:
        sys.exit(f'no index for {key} — build one: python3 {__file__} index "{args.target}"')

    out = resolve_context(index, node)
    out["indexedAt"] = index.get("indexedAt")
    live = (api(f"/v1/files/{key}/meta", token).get("file") or {})   # tier 3, cheap
    if live.get("version") != index.get("version"):
        out["stale"] = True
        print("경고: 파일이 인덱스보다 최신입니다 — 노드 ID가 어긋날 수 있습니다. "
              f'다시 만들려면: python3 {__file__} index "{args.target}"', file=sys.stderr)
    emit({"_source": "index", **out})


def clean_files():
    """Hide everything Figma says is gone. Files we could not check are left
    alone — a network hiccup must not make a live file disappear."""
    files = list_files(verify=True)
    missing = [f["key"] for f in files if f.get("missing")]
    if missing:
        edit_hidden({"action": "hide", "keys": missing})
    return {"hidden": missing,
            "unverified": [f["key"] for f in files if f.get("unverified")],
            "remaining": len(files) - len(missing)}


# Figma exposes no way to trigger a plugin from outside, so this drives the
# command palette: ⌘/ then the plugin name. Needs Accessibility permission.
QUICK_ACTIONS = '''
tell application "Figma" to activate
-- activate is a request, not a guarantee: if another window (the app window we
-- just opened, say) keeps focus, the keystrokes land in the wrong app.
set ready to false
repeat 40 times
    tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
    end tell
    if frontApp is "Figma" then
        set ready to true
        exit repeat
    end if
    delay 0.25
end repeat
if not ready then error "Figma did not come to the front"
delay {wait}
tell application "System Events"
    keystroke "/" using {{command down}}
    delay 0.6
    keystroke "{name}"
    delay 1.0
    key code 36
end tell
'''


def accessibility_target():
    """What macOS actually lists in Accessibility: the interpreter's .app bundle
    when there is one, since keystrokes are attributed to the calling process."""
    real = os.path.realpath(sys.executable)
    bundle = os.path.join(os.path.dirname(os.path.dirname(real)), "Resources", "Python.app")
    return bundle if os.path.exists(bundle) else real


def run_plugin(name="figma-bridge", wait=6.0):
    proc = subprocess.run(["osascript", "-e", QUICK_ACTIONS.format(name=name, wait=wait)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        error = proc.stderr.strip()[:300]
        out = {"ok": False, "error": error}
        if "1002" in error or "not allowed" in error or "허용되지 않" in error:
            out["hint"] = "시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용에 아래 항목을 추가하세요"
            out["add"] = accessibility_target()
        return out
    return {"ok": True, "note": "명령 팔레트로 실행을 시도했습니다 — 플러그인 창을 확인하세요"}


def open_in_figma(key, autorun=False, wait=6.0):
    if not key:
        return {"ok": False, "error": "key required"}
    # The figma: scheme routes to the desktop app explicitly, unlike an https URL.
    subprocess.run(["open", f"figma://file/{key}"], check=False)
    result = {"ok": True, "opened": key}
    if autorun:
        # Switching files takes a while; typing into the palette before the file
        # is ready silently does nothing, so err on the side of waiting.
        result["plugin"] = run_plugin(wait=wait)
    return result


def cmd_open(args, token=None):
    key, _ = parse_target(args.target)
    if not args.autorun:
        emit(open_in_figma(key))
        return

    wanted = file_meta(key)[1]

    def attached():
        status = relay_call("/status")
        name = (status.get("file") or {}).get("fileName")
        return status["pluginConnected"] and same_file(name, wanted) is not False, name

    connected, name = attached()
    result = {"opened": key, "alreadyConnected": connected}
    if not connected:
        # Keystrokes go through the relay: Accessibility is granted to the launchd
        # Python, while a process started from an agent's terminal gets refused.
        result.update(relay_call("/open", {"key": key, "autorun": True, "wait": args.wait}))
        deadline = time.time() + 20
        while result["plugin"]["ok"] and not connected and time.time() < deadline:
            time.sleep(1)
            connected, name = attached()
    emit({**result, "ok": connected, "connected": connected, "file": name})


def cmd_files(args, token=None):
    if args.clean:
        emit({"_source": "local", **clean_files(), "files": list_files()})
    else:
        emit({"_source": "local", "files": list_files(args.verify), "hidden": read_hidden()})


def cmd_unhide(args, token=None):
    if not args.all and not args.target:
        sys.exit("unhide needs a figma URL or --all")
    payload = {"action": "clear"} if args.all else {
        "action": "show", "key": parse_target(args.target)[0]}
    emit({"_source": "local", "hidden": edit_hidden(payload)})


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{python}</string><string>{script}</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def cmd_install_agent(args, token=None):
    log = os.path.expanduser("~/.figma-bridge/relay.log")
    os.makedirs(os.path.dirname(log), mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
    with open(AGENT_PLIST, "w", encoding="utf-8") as f:
        f.write(PLIST.format(label=AGENT_LABEL, python=sys.executable,
                             script=os.path.abspath(__file__), log=log))

    uid = os.getuid()
    target = f"gui/{uid}/{AGENT_LABEL}"

    def launchctl(*argv):
        return subprocess.run(["launchctl", *argv], capture_output=True, text=True)

    launchctl("bootout", target)  # ignore "not loaded"
    # bootout is asynchronous: bootstrapping while the old job is still on its way
    # out fails with "Input/output error" and leaves nothing running at all.
    for _ in range(50):
        if launchctl("print", target).returncode != 0:
            break
        time.sleep(0.1)

    out = None
    for _ in range(20):
        out = launchctl("bootstrap", f"gui/{uid}", AGENT_PLIST)
        if out.returncode == 0:
            break
        time.sleep(0.3)

    loaded = launchctl("print", target).returncode == 0
    emit({"plist": AGENT_PLIST, "log": log, "loaded": loaded,
          "error": None if loaded else (out.stderr.strip() if out else "bootstrap failed"),
          "url": RELAY + "/"})


def cmd_uninstall_agent(args, token=None):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                   capture_output=True)
    existed = os.path.exists(AGENT_PLIST)
    if existed:
        os.remove(AGENT_PLIST)
    emit({"removed": existed, "plist": AGENT_PLIST})


def cmd_setup(args, token=None):
    checks = []
    checks.append(("python", sys.version.split()[0], sys.version_info >= (3, 9)))

    has_token = bool(find_token())
    checks.append((TOKEN_KEY, "set" if has_token else f"missing in {ENV_PATH}", has_token))

    app = os.path.isdir("/Applications/Figma.app")
    checks.append(("Figma desktop", "installed" if app else "not installed "
                   "(brew install --cask figma)", app))

    try:
        secret = read_relay_token() or ""
        probe = urllib.request.Request(RELAY + "/status", headers={"X-Relay-Token": secret})
        status = json.loads(urllib.request.urlopen(probe, timeout=2).read())
        relay_up, plugin_up = True, status["pluginConnected"]
    except Exception:
        relay_up, plugin_up = False, False
    checks.append(("relay", RELAY if relay_up else "not running (figma.py relay)", relay_up))
    checks.append(("plugin", "connected" if plugin_up else "not connected", plugin_up))

    width = max(len(name) for name, _, _ in checks)
    for name, detail, ok in checks:
        print(f"{'OK ' if ok else '-- '} {name.ljust(width)}  {detail}")

    if not has_token:
        print(f"\n1. figma.com > Settings > Security > Personal access tokens > Generate new token")
        print(f"2. echo 'FIGMA_PERSONAL_TOKEN=figd_...' >> {ENV_PATH}")
    if not plugin_up:
        print(f"\n3. Figma desktop > Plugins > Development > Import plugin from manifest")
        print(f"   {os.path.join(HERE, 'plugin', 'manifest.json')}")
        print(f"4. python3 {__file__} serve    (leave it running)")
        print(f"5. open the target file in Figma, then run the figma-bridge plugin")


# --- cli --------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="figma", description="Figma REST bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="verify token").set_defaults(fn=cmd_ping)

    t = sub.add_parser("tree", help="node tree, normalized")
    t.add_argument("target", help="figma URL or file key")
    t.add_argument("--node", help="node id (overrides URL)")
    t.add_argument("--depth", type=int, help="API-side depth limit")
    t.add_argument("--prune", type=int, help="local child depth limit in output")
    t.add_argument("--geometry", action="store_true", help="include vector paths")
    t.add_argument("--raw", action="store_true", help="raw API node, no normalization")
    t.set_defaults(fn=cmd_tree)

    r = sub.add_parser("render", help="render node to image")
    r.add_argument("target")
    r.add_argument("--node", help="node id, comma separated for several")
    r.add_argument("--format", default="png", choices=["png", "jpg", "svg", "pdf"])
    r.add_argument("--scale", type=float, default=2.0, help="0.01-4")
    r.add_argument("--absolute", action="store_true", help="use_absolute_bounds")
    r.add_argument("--out", help="directory to save into")
    r.set_defaults(fn=cmd_render)

    a = sub.add_parser("assets", help="source images used as fills")
    a.add_argument("target")
    a.add_argument("--out", help="directory to save into")
    a.set_defaults(fn=cmd_assets)

    c = sub.add_parser("comments", help="read or post comments")
    c.add_argument("target")
    c.add_argument("--post", help="comment text to post")
    c.add_argument("--reply-to", help="comment id to reply to")
    c.set_defaults(fn=cmd_comments)

    m = sub.add_parser("meta", help="file metadata")
    m.add_argument("target")
    m.set_defaults(fn=cmd_meta)

    ix = sub.add_parser("index", help="map a whole file once: screens, components, flow")
    ix.add_argument("target", help="figma URL or file key")
    ix.set_defaults(fn=cmd_index)

    cx = sub.add_parser("context", help="what a narrow fetch of a node leaves out")
    cx.add_argument("target", help="figma URL or file key")
    cx.add_argument("--node", help="node id (or use a URL with node-id)")
    cx.set_defaults(fn=cmd_context)

    # --- plugin route (no REST token needed) ---
    sub.add_parser("setup", help="check this machine and print setup steps") \
        .set_defaults(fn=cmd_setup, needs_token=False)
    for name, help_text in (("serve", "run the app: web UI plus the plugin relay"),
                            ("relay", "same as serve (kept for muscle memory)")):
        r = sub.add_parser(name, help=help_text)
        r.add_argument("--rotate", action="store_true",
                       help="mint a new token instead of reusing the stored one")
        r.set_defaults(fn=cmd_serve, needs_token=False)

    f = sub.add_parser("files", help="files Figma has open or you pinned")
    f.add_argument("--verify", action="store_true", help="check each file still exists")
    f.add_argument("--clean", action="store_true", help="hide the ones that are gone")
    f.set_defaults(fn=cmd_files, needs_token=False)

    u = sub.add_parser("unhide", help="bring hidden files back into the list")
    u.add_argument("target", nargs="?", help="figma URL or file key")
    u.add_argument("--all", action="store_true", help="unhide everything")
    u.set_defaults(fn=cmd_unhide, needs_token=False)

    o = sub.add_parser("open", help="open a file in the Figma desktop app")
    o.add_argument("target", help="figma URL or file key")
    o.add_argument("--autorun", action="store_true",
                   help="also try to start the plugin via the command palette")
    o.add_argument("--wait", type=float, default=6.0,
                   help="seconds to let the file load before typing (default 6)")
    o.set_defaults(fn=cmd_open, needs_token=False)

    sub.add_parser("install-agent", help="start the app at login (launchd)") \
        .set_defaults(fn=cmd_install_agent, needs_token=False)
    sub.add_parser("uninstall-agent", help="stop starting the app at login") \
        .set_defaults(fn=cmd_uninstall_agent, needs_token=False)

    e = sub.add_parser("exec", help="run Plugin API JS in the open file")
    e.add_argument("target", nargs="?", help="figma URL — guards against the wrong file being open")
    e.add_argument("--code", help="JS body; top-level await and return are allowed")
    e.add_argument("--file", help="read the JS body from a file")
    e.add_argument("--timeout", type=int, default=60)
    e.set_defaults(fn=cmd_exec, needs_token=False)

    for name, fn, help_text in (
        ("tokens", cmd_tokens, "local variables and styles with their values"),
        ("selection", cmd_selection, "what is selected right now"),
        ("pages", cmd_pages, "pages in the open file"),
    ):
        w = sub.add_parser(name, help=help_text)
        w.add_argument("target", nargs="?")
        w.set_defaults(fn=fn, needs_token=False)

    fi = sub.add_parser("find", help="find nodes by name or type on the current page")
    fi.add_argument("target", nargs="?")
    fi.add_argument("--name", help="substring, case-insensitive")
    fi.add_argument("--type", help="comma separated node types, e.g. FRAME,TEXT")
    fi.add_argument("--limit", type=int, default=50)
    fi.set_defaults(fn=cmd_find, needs_token=False)

    ins = sub.add_parser("inspect", help="one node's properties, normalized")
    ins.add_argument("target", nargs="?")
    ins.add_argument("--node", help="node id (or use a URL with node-id)")
    ins.set_defaults(fn=cmd_inspect, needs_token=False)

    co = sub.add_parser("components", help="components and variant sets on the page")
    co.add_argument("target", nargs="?")
    co.set_defaults(fn=cmd_components, needs_token=False)

    ex = sub.add_parser("export", help="export a node via the plugin — no REST quota")
    ex.add_argument("target", nargs="?")
    ex.add_argument("--node", help="node id (or use a URL with node-id)")
    ex.add_argument("--format", default="png", choices=["png", "jpg", "svg", "pdf"])
    ex.add_argument("--scale", type=float, default=2.0)
    ex.add_argument("--out", default=".", help="directory to save into")
    ex.set_defaults(fn=cmd_export, needs_token=False)

    tx = sub.add_parser("text", help="read every text node, or replace a string in all of them")
    tx.add_argument("target", nargs="?")
    tx.add_argument("--replace", nargs=2, metavar=("FROM", "TO"),
                    help="replace FROM with TO in every text node")
    tx.set_defaults(fn=cmd_text, needs_token=False)

    for parser in sub.choices.values():
        parser.add_argument("--save", metavar="FILE",
                            help="write the JSON result to a file, print a summary")
    return p


def main(argv=None):
    global _SAVE_TO
    args = build_parser().parse_args(argv)
    _SAVE_TO = args.save
    try:
        args.fn(args, load_token() if getattr(args, "needs_token", True) else None)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
