#!/usr/bin/env python3
"""figma-bridge — Figma REST API CLI (B route). Standard library only.

Each subcommand is independent: it does one thing and prints JSON to stdout.
Token is read from FIGMA_PERSONAL_TOKEN (env or ~/.claude/secrets/.env).
"""
import argparse
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
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


def read_relay_token():
    try:
        with open(RELAY_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


# --- plumbing ---------------------------------------------------------------

def load_token():
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
    sys.exit(f"{TOKEN_KEY} not found. Add it to {ENV_PATH}")


def parse_target(s):
    """Figma URL or bare file key -> (file_key, node_id or None)."""
    if "figma.com" not in s:
        return s, None
    m = re.search(r"figma\.com/(?:file|design|board|slides|make)/([0-9A-Za-z]{10,128})", s)
    if not m:
        sys.exit(f"no file key in URL: {s}")
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
        with urllib.request.urlopen(req) as r:
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


def emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
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
        out["layout"] = {
            "mode": n["layoutMode"],
            "gap": n.get("itemSpacing"),
            "padding": [n.get("paddingTop", 0), n.get("paddingRight", 0),
                        n.get("paddingBottom", 0), n.get("paddingLeft", 0)],
        }

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

    def submit(self, code, timeout):
        job = {"id": uuid.uuid4().hex, "code": code}
        done = threading.Event()
        with self.lock:
            self.jobs.append(job)
            self.events[job["id"]] = done
        if not done.wait(timeout):
            with self.lock:
                self.events.pop(job["id"], None)
            return {"ok": False, "error": f"no result within {timeout}s"}
        with self.lock:
            self.events.pop(job["id"], None)
            return self.results.pop(job["id"], {"ok": False, "error": "result lost"})

    def take(self, meta):
        with self.lock:
            self.last_poll = time.time()
            if meta:
                self.plugin = meta
            return self.jobs.pop(0) if self.jobs else None

    def finish(self, payload):
        with self.lock:
            self.results[payload.get("id")] = payload
            done = self.events.get(payload.get("id"))
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

    def _send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        # The plugin iframe has a null origin, so it needs a wildcard. Access is
        # gated by X-Relay-Token, not by this header — see _authorized().
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Relay-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _authorized(self):
        """This endpoint executes code, so a page the user visits must not reach it.

        CORS alone does not help: it gates reading the response, not delivering the
        request, and a sandboxed iframe can forge `Origin: null`. The shared secret
        forces a preflight the attacker cannot satisfy; the Host check blocks DNS
        rebinding from a name that resolves to 127.0.0.1.
        """
        host = (self.headers.get("Host") or "").strip()
        if host not in (f"127.0.0.1:{RELAY_PORT}", f"localhost:{RELAY_PORT}"):
            self._send(403, {"error": f"unexpected Host: {host}"})
            return False
        if not STATE.token or self.headers.get("X-Relay-Token") != STATE.token:
            self._send(403, {"error": "missing or wrong X-Relay-Token"})
            return False
        return True

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        if not self._authorized():
            return
        self._send(200, STATE.status()) if self.path == "/status" else self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        if self.path == "/poll":
            job = STATE.take(self._read())
            self._send(200, job) if job else self._send(204)
        elif self.path == "/result":
            STATE.finish(self._read())
            self._send(200, {"ok": True})
        elif self.path == "/job":
            payload = self._read()
            self._send(200, STATE.submit(payload.get("code", ""), payload.get("timeout", 60)))
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


def relay_call(path, payload=None, timeout=70):
    secret = read_relay_token()
    if not secret:
        sys.exit(f"no relay token at {RELAY_TOKEN_FILE} — start the relay: python3 {__file__} relay")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(RELAY + path, data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          "X-Relay-Token": secret})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return None if r.status == 204 else json.load(r)
    except urllib.error.URLError as e:
        sys.exit(f"relay unreachable at {RELAY} — start it: python3 {__file__} relay  ({e})")


def cmd_relay(args, token=None):
    STATE.token = uuid.uuid4().hex
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
    print(f"relay on {listening} — import {os.path.join(HERE, 'plugin')} in Figma "
          f"(Plugins > Development) and run figma-bridge. Ctrl-C to stop.")
    print(f"\nplugin token (paste it into the plugin window once):\n  {STATE.token}\n")
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def run_js(code, target=None, timeout=60):
    status = relay_call("/status")
    if not status["pluginConnected"]:
        sys.exit("plugin not connected — open the file in the Figma desktop app and run the "
                 "figma-bridge plugin. For files that are not open, use the official MCP use_figma.")
    if target:
        want, _ = parse_target(target)
        have = (status.get("file") or {}).get("fileKey")
        if have and want and have != want:
            sys.exit(f"plugin is attached to file {have}, not {want} — open the right file first")
    out = relay_call("/job", {"code": code, "timeout": timeout}, timeout=timeout + 10)
    emit({"_source": "plugin", **out})


def cmd_exec(args, token=None):
    code = open(args.file, encoding="utf-8").read() if args.file else args.code
    if not code:
        sys.exit("exec needs --code or --file")
    run_js(code, args.target, args.timeout)


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
return figma.root.children.map(p => ({ id: p.id, name: p.name, children: p.children.length }));
"""


def cmd_tokens(args, token=None):
    run_js(JS_TOKENS, args.target, 60)


def cmd_selection(args, token=None):
    run_js(JS_SELECTION, args.target, 30)


def cmd_pages(args, token=None):
    run_js(JS_PAGES, args.target, 30)


def cmd_setup(args, token=None):
    checks = []
    checks.append(("python", sys.version.split()[0], sys.version_info >= (3, 9)))

    has_token = False
    try:
        load_token_quiet = os.getenv(TOKEN_KEY) or (
            TOKEN_KEY + "=" in open(ENV_PATH, encoding="utf-8").read())
        has_token = bool(load_token_quiet)
    except OSError:
        pass
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
        print(f"4. python3 {__file__} relay    (leave it running)")
        print(f"5. open the target file in Figma, then run the figma-bridge plugin")
    print(f"\ngit: {shutil.which('git') or 'not found'}")


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

    # --- plugin route (no REST token needed) ---
    sub.add_parser("setup", help="check this machine and print setup steps") \
        .set_defaults(fn=cmd_setup, needs_token=False)
    sub.add_parser("relay", help="run the local relay the Figma plugin talks to") \
        .set_defaults(fn=cmd_relay, needs_token=False)

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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args, load_token() if getattr(args, "needs_token", True) else None)


if __name__ == "__main__":
    main()
