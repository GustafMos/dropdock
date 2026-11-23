"""dropdock server: a resumable, fast upload page for sending big files to this computer.

    python server.py --allow 192.168.1.0/24 --dest ~/Downloads/dropdock

Open the printed link on the other device (phone, laptop, tablet), pick files, and they upload as 16 MB
pieces, 4 at a time, over kept-alive connections. If the connection drops or the tab reloads, pick the same
files again and only the missing pieces are sent. Partial data lives in <dest>/.partial/<name>.<size>.part
(plus a .json sidecar listing the pieces that arrived) and is moved to <dest>/<name> only when every piece
is there. Before uploading, the page asks whether this computer already has the file (same size and 12
sampled blocks) and skips it if so.

Only clients in --allow (IP addresses or networks) may connect, and every request needs the secret key in
the URL. Traffic is plain HTTP: use it on a network you trust, or over a VPN such as Tailscale/WireGuard.

This file has no dependencies outside the Python standard library and can be copied and run on its own.
"""
import argparse
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__version__ = "0.1.0"

CHUNK = 16 << 20
LOCK = threading.Lock()
SAMPLE_N, SAMPLE_L = 12, 65536
MAX_SIZE = 1 << 42   # 4 TB per file
MAX_CONN = 64        # connections served at once

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Send to __HOST__</title>
<style>
:root{--bg:#111;--fg:#eee;--mut:#999;--acc:#4caf50;--bar:#333;--err:#ff6b6b}
@media (prefers-color-scheme: light){:root{--bg:#fafafa;--fg:#111;--mut:#666;--bar:#ddd;--err:#c62828}}
body{background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif;margin:0 auto;padding:16px;max-width:640px}
h1{font-size:20px;margin:0 0 4px}p{color:var(--mut);margin:4px 0 14px}
label.pick{display:block;background:var(--acc);color:#000;text-align:center;padding:16px;border-radius:10px;font-weight:600;cursor:pointer}
input{display:none}.f{margin:14px 0;padding:10px;border:1px solid var(--bar);border-radius:8px}
.n{word-break:break-all;font-weight:600}.s{color:var(--mut);font-size:14px;margin-top:4px}
.b{height:8px;background:var(--bar);border-radius:4px;margin-top:8px;overflow:hidden}
.b i{display:block;height:100%;width:0;background:var(--acc)}.e{color:var(--err)}
button{margin-left:6px}
</style></head><body>
<h1>Send to __HOST__</h1>
<p>Keep this tab open and the screen on while it runs. If it stops, pick the same files again and it continues where it left off.</p>
<label class="pick">Choose files<input id="p" type="file" multiple></label>
<div id="list"></div>
<script>
const KEY = location.pathname.split('/')[1];
const HOST = __HOSTJS__;
// report what happens in the browser to the server's console (helps when a phone screen misbehaves)
const log = (m) => { try { navigator.sendBeacon('/' + KEY + '/log', String(m).slice(0, 1000)); } catch (e) {} };
window.onerror = (m, src, line) => log('JS error: ' + m + ' line ' + line);
window.onunhandledrejection = (e) => log('JS rejection: ' + (e.reason && e.reason.message || e.reason));
document.addEventListener('visibilitychange', () => log('page ' + document.visibilityState));
log('page loaded: ' + navigator.userAgent);
if (/Firefox/.test(navigator.userAgent) && /Android/.test(navigator.userAgent)) {
  const w = document.createElement('p'); w.className = 'e';
  w.textContent = 'Firefox for Android copies every picked file before uploading, so big files freeze it. Open this link in Chrome or Samsung Internet instead.';
  document.querySelector('h1').after(w);
}
document.getElementById('p').onclick = () => log('file picker opened');
const PAR = 4;  // pieces in flight at once
const q = (o) => Object.entries(o).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
const fmt = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const bad = (el, text) => { el.innerHTML = '<span class="e"></span>'; el.firstChild.textContent = text; };
let queue = [], running = false, checking = Promise.resolve();
// Same sample positions as the server: N blocks of L bytes spread evenly over the file (whole file if small).
const N = 12, L = 65536;
// A 64-bit fingerprint of the sampled blocks: tells two different files with the same name and size apart.
function cyrb53(b) {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < b.length; i++) { h1 = Math.imul(h1 ^ b[i], 2654435761); h2 = Math.imul(h2 ^ b[i], 1597334677); }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h2 >>> 0).toString(16).padStart(8, '0') + (h1 >>> 0).toString(16).padStart(8, '0');
}
function sampleBlob(f) {
  if (f.size <= N * L) return f.slice(0);
  const parts = [];
  for (let i = 0; i < N; i++) { const o = Math.floor(i * (f.size - L) / (N - 1)); parts.push(f.slice(o, o + L)); }
  return new Blob(parts);
}
document.getElementById('p').onchange = (e) => {
  const picked = [...e.target.files];
  log('picked ' + picked.length + ' file(s): ' + picked.map(f => f.name + ' ' + fmt(f.size)).join(', '));
  e.target.value = '';
  const jobs = picked.map(f => {
    const d = document.createElement('div'); d.className = 'f';
    d.innerHTML = '<div class="n"></div><div class="s"></div><div class="b"><i></i></div>';
    d.querySelector('.n').textContent = f.name;
    d.querySelector('.s').textContent = 'checking whether ' + HOST + ' already has it...';
    document.getElementById('list').appendChild(d);
    return {f, d};
  });
  // check every picked file first (fast), then upload only the ones the computer doesn't have
  checking = checking.then(async () => {
    for (const j of jobs) {
      const st = j.d.querySelector('.s');
      let r = null, body = null;
      try { body = new Uint8Array(await sampleBlob(j.f).arrayBuffer()); j.fp = cyrb53(body); } catch (err) { j.fp = ''; }
      for (let t = 0; t < 5 && !r; t++) {
        try { r = await call('/check?' + q({name: j.f.name, size: j.f.size}), {method: 'POST', body: body || sampleBlob(j.f)}); }
        catch (err) { st.innerHTML = '<span class="e">check failed, retrying...</span>'; await sleep(2000); }
      }
      if (r && r.dup) {
        j.d.querySelector('i').style.width = '100%';
        st.textContent = 'already there as ' + r.dup + ' (skipped)';
        const b = document.createElement('button'); b.textContent = 'send anyway';
        b.onclick = () => { b.remove(); st.textContent = 'waiting'; queue.push(j); if (!running) run(); };
        st.appendChild(b);
      } else {
        st.textContent = 'waiting';
        queue.push(j);
        if (!running) run();
      }
    }
  });
};
async function call(path, opts) {
  const r = await fetch('/' + KEY + path, opts);
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return r.json();
}
async function run() {
  running = true;
  while (queue.length) { const j = queue.shift(); await send(j.f, j.d, j.fp); }
  running = false;
}
async function send(f, d, fp) {
  const st = d.querySelector('.s'), bar = d.querySelector('i');
  const id = {name: f.name, size: f.size, fp: fp || ''};
  const finish = (note) => { bar.style.width = '100%'; st.textContent = 'done - ' + fmt(f.size) + ' saved on ' + HOST + (note ? ' (' + note + ')' : ''); };
  let fails = 0;
  while (true) {
    let s;
    try { s = await call('/status?' + q(id)); fails = 0; }
    catch (err) {
      if (err.status === 400 || err.status === 507) { bad(st, err.message); return; }
      fails++;
      bad(st, HOST + ' not reachable, retrying (' + fails + ')...');
      await sleep(Math.min(30000, 1000 * fails));
      continue;
    }
    if (s.done) return finish(s.note);
    const CH = s.chunk, total = s.total, todo = s.missing.slice();
    let have = total - todo.length, errs = 0, result = null;
    const base = have, t0 = Date.now();
    const show = () => {
      const pct = have / total * 100, rate = (have - base) * CH / Math.max((Date.now() - t0) / 1000, 0.001);
      bar.style.width = pct.toFixed(1) + '%';
      st.textContent = fmt(Math.min(have * CH, f.size)) + ' / ' + fmt(f.size) + ' - ' + (rate / 1e6).toFixed(1) + ' MB/s' +
        (rate > 0 ? ' - ' + Math.ceil((total - have) * CH / rate / 60) + ' min left' : '');
    };
    show();
    const worker = async () => {
      while (todo.length && !result) {
        const i = todo.shift();
        try {
          const r = await call('/put?' + q({...id, index: i}), {method: 'PUT', body: f.slice(i * CH, Math.min((i + 1) * CH, f.size))});
          errs = 0; have = Math.max(have, r.have_chunks ?? have + 1); show();
          if (r.done) result = r;
        } catch (err) {
          if (err.status === 400 || err.status === 500 || err.status === 507) { result = {fatal: err.message}; break; }
          todo.push(i); errs++;
          st.innerHTML = '<span class="e">connection lost, retrying (' + errs + ')...</span>';
          await sleep(Math.min(30000, 1000 * errs));
        }
      }
    };
    await Promise.all(Array.from({length: PAR}, worker));
    if (result && result.fatal) { bad(st, result.fatal + ' - pick the file again to retry'); return; }
    if (result) return finish(result.note);
    // otherwise ask the server again what is still missing
  }
}
</script></body></html>"""


def stamp():
    return time.strftime("%H:%M:%S")


def clean(text, limit=300):
    """Client-supplied text made safe for a one-line log entry."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text))[:limit]


def say(*parts):
    print(stamp(), *parts, flush=True)


# ---- duplicate check -------------------------------------------------------------------------------------
_sample_cache = {}


def sample_bytes(path, size):
    """The same N evenly spaced L-byte blocks the page sends (whole file if small). Cached per mtime."""
    key = (path, size, os.path.getmtime(path))
    if key not in _sample_cache:
        if len(_sample_cache) > 4096:
            _sample_cache.clear()
        with open(path, "rb") as fh:
            if size <= SAMPLE_N * SAMPLE_L:
                _sample_cache[key] = fh.read()
            else:
                out = []
                for i in range(SAMPLE_N):
                    fh.seek(i * (size - SAMPLE_L) // (SAMPLE_N - 1))
                    out.append(fh.read(SAMPLE_L))
                _sample_cache[key] = b"".join(out)
    return _sample_cache[key]


class Library:
    """Files this computer already has, indexed by size. The index is rebuilt at most every 30 s."""

    def __init__(self, root):
        self.root, self.built, self.by_size, self.lock = root, 0.0, {}, threading.Lock()

    def _rebuild(self):
        by_size = {}
        for d, dirs, fs in os.walk(self.root):
            dirs[:] = [x for x in dirs if x != ".partial"]
            for f in fs:
                p = os.path.join(d, f)
                try:
                    by_size.setdefault(os.path.getsize(p), []).append(p)
                except OSError:
                    pass
        self.by_size, self.built = by_size, time.time()

    def add(self, path, size):
        with self.lock:
            if path not in self.by_size.get(size, []):
                self.by_size.setdefault(size, []).append(path)

    def find(self, size, samples):
        """A file with the same size and identical sample blocks, or None."""
        with self.lock:
            if time.time() - self.built > 30:
                self._rebuild()
            candidates = list(self.by_size.get(size, []))
        for p in candidates:
            try:
                if os.path.getsize(p) == size and sample_bytes(p, size) == samples:
                    return p
            except OSError:
                continue
        return None


def find_duplicate(library, size, samples):
    """One-off search: a file under `library` with the same size and sample blocks, or None."""
    return Library(library).find(size, samples)


# ---- file names and pieces -------------------------------------------------------------------------------
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
NAME_BYTES = 150   # leaves room for ".<size>.part.json.tmp" within the usual 255-byte limit


def safe_name(n):
    """A plain file name that is safe to create in the destination folder on any OS."""
    n = n.replace("\\", "/").rsplit("/", 1)[-1].strip()
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", n).rstrip(". ")
    if not n or n in (".", "..") or n.lower().startswith(".partial"):
        raise ValueError("bad name")
    if n.split(".")[0].upper() in _RESERVED:
        n = "_" + n
    if len(n.encode("utf-8")) > NAME_BYTES:
        base, ext = os.path.splitext(n)
        if len(ext.encode("utf-8")) > 20:
            base, ext = n, ""
        keep = NAME_BYTES - len(ext.encode("utf-8"))
        n = base.encode("utf-8")[:keep].decode("utf-8", "ignore").rstrip(". ") + ext
    return n


def n_chunks(size):
    return max(1, -(-size // CHUNK))


def chunk_len(size, i):
    return max(0, min(CHUNK, size - i * CHUNK))


_states = {}      # part path -> set of piece indices already on disk
_rate = {}        # part path -> [bytes since last report, time of last report]


def load_state(part, size):
    """Pieces already received, from the .json sidecar. Without a sidecar nothing is trusted."""
    if part in _states:
        if os.path.exists(part) or not _states[part]:
            return _states[part]
        del _states[part]  # the .part was deleted while we were running
    done = set()
    side = part + ".json"
    if os.path.exists(side) and os.path.exists(part):
        try:
            with open(side) as fh:
                st = json.load(fh)
            if st.get("chunk") == CHUNK and st.get("size") in (None, size):
                done = {i for i in st["done"] if 0 <= i < n_chunks(size)}
        except (OSError, ValueError, KeyError, TypeError):
            done = set()
    _states[part] = done
    return done


def save_state(part, size, done):
    tmp = part + ".json.tmp"
    with open(tmp, "w") as fh:
        json.dump({"chunk": CHUNK, "size": size, "done": sorted(done)}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, part + ".json")


# ---- HTTP ------------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "dropdock/" + __version__
    protocol_version = "HTTP/1.1"  # keep connections open between pieces
    timeout = 120                  # a stalled client must not hang a worker thread forever

    def log_message(self, fmt, *a):
        pass

    def _json(self, obj, code=200, close=False):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(b)

    def _allowed(self, ip):
        a = ipaddress.ip_address(ip.split("%")[0])
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        return any(a in net for net in self.server.allow)

    def _gate(self):
        """(path, query) for an allowed client with the right key; otherwise answer and return None.
        Error answers close the connection, so an unread request body never matters."""
        u = urlparse(self.path)
        parts = u.path.split("/")
        key_ok = len(parts) >= 2 and secrets.compare_digest(parts[1].encode("utf-8", "surrogateescape"),
                                                           self.server.key.encode("utf-8"))
        if not self._allowed(self.client_address[0]):
            if key_ok:  # only a device that has the link is worth reporting
                say("refused client", self.client_address[0])
            self.send_error(403)
            return None
        if not key_ok:
            self.send_error(404)
            return None
        return "/" + "/".join(parts[2:]), {k: v[0] for k, v in parse_qs(u.query).items()}

    def _paths(self, qs):
        name, size = safe_name(qs["name"]), int(qs["size"])
        if not 0 <= size <= MAX_SIZE:
            raise ValueError("bad size")
        fp = qs.get("fp", "")  # the page's fingerprint of the file's content: keeps same-name files apart
        if not re.fullmatch(r"[0-9a-f]{0,16}", fp):
            raise ValueError("bad fingerprint")
        self.fp = fp
        part = os.path.join(self.server.partdir, f"{name}.{size}{'.' + fp if fp else ''}.part")
        return name, size, part, os.path.join(self.server.dest, name)

    def _already_finished(self, name, size, part):
        """This exact upload was completed earlier in this run (a late retry): where it went, or None.
        Only uploads that carry a fingerprint can be recognised; others are simply received again."""
        fin = self.server.finished.get((name, size, self.fp)) if self.fp else None
        if fin and not os.path.exists(part) and os.path.exists(fin) and os.path.getsize(fin) == size:
            return fin
        return None

    def _finish(self, name, size, part, final):
        """Move a complete .part into place. An identical file of the same name -> keep that one.
        A different file of the same name -> save as 'name (2).ext'."""
        _states.pop(part, None)
        _rate.pop(part, None)
        with open(part, "r+b") as fh:
            fh.truncate(size)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(part + ".json"):
            os.remove(part + ".json")
        note = ""
        if os.path.exists(final):
            same = False
            try:
                same = os.path.getsize(final) == size and sample_bytes(final, size) == sample_bytes(part, size)
            except OSError:
                pass
            if same:
                os.remove(part)
                self.server.finished[(name, size, self.fp)] = final
                return "already there"
            base, ext = os.path.splitext(name)
            i = 2
            while os.path.exists(os.path.join(self.server.dest, f"{base} ({i}){ext}")):
                i += 1
            final = os.path.join(self.server.dest, f"{base} ({i}){ext}")
            note = "saved as " + os.path.basename(final)
        os.replace(part, final)
        self.server.finished[(name, size, self.fp)] = final
        if self.server.library:
            self.server.library.add(final, size)
        say(f"complete: {final} ({size / 1e9:.2f} GB)")
        return note

    def do_GET(self):
        g = self._gate()
        if not g:
            return
        path, qs = g
        if path in ("/", ""):
            say("page opened by", self.client_address[0])
            label = self.server.host_label
            b = (PAGE.replace("__HOSTJS__", json.dumps(label).replace("<", "\\" + "u003c"))
                 .replace("__HOST__", html.escape(label))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
            return
        if path == "/status":
            try:
                name, size, part, final = self._paths(qs)
            except (KeyError, ValueError):
                return self._json({"error": "bad request"}, 400)
            with LOCK:
                if self._already_finished(name, size, part):
                    return self._json({"done": True, "note": "already there"})
                total = n_chunks(size)
                done = load_state(part, size)
                if size == 0 or len(done) == total and os.path.exists(part):
                    if not os.path.exists(part):
                        open(part, "wb").close()
                    return self._json({"done": True, "note": self._finish(name, size, part, final)})
                need = size - sum(chunk_len(size, i) for i in done)
                free = shutil.disk_usage(self.server.dest).free
                if need > free:
                    return self._json({"error": "not enough space on %s: needs %.1f GB, %.1f GB free"
                                       % (self.server.host_label, need / 1e9, free / 1e9)}, 507)
                missing = [i for i in range(total) if i not in done]
            return self._json({"done": False, "chunk": CHUNK, "total": total, "missing": missing})
        self.send_error(404)

    def do_POST(self):
        g = self._gate()
        if not g:
            return
        path, qs = g
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._json({"error": "bad request"}, 400, close=True)
        if path == "/log":
            msg = self.rfile.read(min(length, 4096)).decode("utf-8", "replace")
            if length > 4096:
                self.close_connection = True
            say("browser:", clean(msg, 1000))
            self.send_response(204)
            self.end_headers()
            return
        if path != "/check":
            return self.send_error(404)
        try:
            safe_name(qs["name"])
            size = int(qs["size"])
        except (KeyError, ValueError):
            return self._json({"error": "bad request"}, 400, close=True)
        if not 0 <= size <= MAX_SIZE or length != min(size, SAMPLE_N * SAMPLE_L):
            return self._json({"error": "bad sample"}, 400, close=True)
        samples = self.rfile.read(length)
        if not self.server.library:
            return self._json({"dup": None})
        dup = self.server.library.find(size, samples)
        rel = os.path.relpath(dup, self.server.library.root) if dup else None
        if dup:
            say(f"skipped (already here): {clean(qs['name'])} == {rel}")
        self._json({"dup": rel})

    def do_PUT(self):
        g = self._gate()
        if not g:
            return
        path, qs = g
        if path != "/put":
            return self.send_error(404)
        try:
            name, size, part, final = self._paths(qs)
            index, length = int(qs["index"]), int(self.headers["Content-Length"])
        except (KeyError, ValueError, TypeError):
            return self._json({"error": "bad request"}, 400, close=True)
        if not 0 <= index < n_chunks(size) or length != chunk_len(size, index):
            return self._json({"error": "bad piece"}, 400, close=True)
        data, left = [], length          # read the whole piece first, outside the lock
        try:
            while left:
                b = self.rfile.read(min(left, 1 << 20))
                if not b:
                    break
                data.append(b)
                left -= len(b)
        except OSError:
            left = -1
        if left:  # connection dropped mid-piece: keep nothing from it
            self.close_connection = True
            return
        with LOCK:
            try:
                if self._already_finished(name, size, part):
                    return self._json({"done": True, "note": "already there"})
                done = load_state(part, size)
                if index not in done:
                    if not os.path.exists(part):
                        free = shutil.disk_usage(self.server.dest).free
                        if size > free:
                            return self._json({"error": "not enough space on %s: needs %.1f GB, %.1f GB free"
                                               % (self.server.host_label, size / 1e9, free / 1e9)}, 507)
                    with open(part, "r+b" if os.path.exists(part) else "w+b") as fh:
                        fh.seek(index * CHUNK)
                        for b in data:
                            fh.write(b)
                        fh.flush()
                        os.fsync(fh.fileno())
                    done.add(index)
                    save_state(part, size, done)
                    r = _rate.setdefault(part, [0, time.time()])
                    r[0] += length
                    if r[0] >= 1 << 30:
                        dt = time.time() - r[1]
                        say(f"receiving {name}: {len(done) / n_chunks(size) * 100:.0f}%"
                            f" ({len(done) * CHUNK / 1e9:.1f} / {size / 1e9:.1f} GB),"
                            f" {r[0] / 1e6 / max(dt, 1e-3):.1f} MB/s")
                        _rate[part] = [0, time.time()]
                have = len(done)
                all_done = have == n_chunks(size)
                note = self._finish(name, size, part, final) if all_done else ""
            except OSError as e:
                say(f"could not write {name}: {e}")
                return self._json({"error": "could not write on %s: %s" % (self.server.host_label, e.strerror or e)},
                                  500)
        self._json({"done": all_done, "have_chunks": have, "note": note})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let a second server share the port

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):  # too many connections at once
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


# ---- startup ---------------------------------------------------------------------------------------------
def local_ipv4():
    """This computer's IPv4 addresses that other devices can reach (no loopback / link-local)."""
    found = []
    try:
        for *_, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(sa[0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:  # the address used for the default route; nothing is actually sent
        s.connect(("192.0.2.1", 9))
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    out = []
    for ip in found:
        if not ip.startswith(("127.", "169.254.", "0.")) and ip not in out:
            out.append(ip)
    return out


def make_servers(binds, port, allow, dest, key, library=None, host_label=None):
    os.makedirs(dest, exist_ok=True)
    partdir = os.path.join(dest, ".partial")
    os.makedirs(partdir, exist_ok=True)
    nets = [ipaddress.ip_network(x, strict=False) for x in allow]
    lib = Library(library) if library else None
    slots = threading.BoundedSemaphore(MAX_CONN)
    finished = {}
    servers = []
    try:
        for b in binds:  # one process for every address, so all uploads share one write lock
            srv = Server((b, port), Handler)
            srv.allow, srv.dest, srv.partdir, srv.key = nets, dest, partdir, key
            srv.library, srv.slots, srv.finished = lib, slots, finished
            srv.host_label = host_label or socket.gethostname()
            servers.append(srv)
    except OSError:
        for srv in servers:
            srv.server_close()
        raise
    return servers


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # never crash on a file name the console can't show
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="dropdock serve", description="Receive big files from other devices "
                                 "through a resumable upload page.")
    ap.add_argument("--allow", action="append", required=True, metavar="IP_OR_NET",
                    help="client address or network allowed to connect, e.g. 192.168.1.50 or 192.168.1.0/24 "
                         "(repeatable)")
    ap.add_argument("--dest", required=True, help="folder the files are saved into")
    ap.add_argument("--library", default=None,
                    help="folder searched for files this computer already has, so they are skipped "
                         "(default: --dest; 'none' turns the check off)")
    ap.add_argument("--bind", action="append", default=None, metavar="ADDRESS",
                    help="address to listen on (repeatable; default: every address of this computer)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--key", default=None, help="secret part of the link (default: a new random one)")
    ap.add_argument("--name", default=None, help="name shown on the page (default: this computer's name)")
    ap.add_argument("--version", action="version", version="dropdock " + __version__)
    a = ap.parse_args(argv)
    dest = os.path.abspath(os.path.expanduser(a.dest))
    library = None if (a.library or "").lower() == "none" else os.path.abspath(os.path.expanduser(a.library or dest))
    key = a.key or secrets.token_urlsafe(9)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", key):
        ap.error("--key must be 4-64 letters, digits, '-' or '_'")
    try:
        for x in a.allow:
            ipaddress.ip_network(x, strict=False)
    except ValueError as e:
        ap.error(str(e))
    binds = a.bind or ["0.0.0.0"]
    try:
        servers = make_servers(binds, a.port, a.allow, dest, key, library, a.name)
    except OSError as e:
        print(f"could not listen on port {a.port}: {e} - is it already in use?", flush=True)
        return 1
    shown = local_ipv4() if binds == ["0.0.0.0"] else binds
    for b in shown or ["127.0.0.1"]:
        print(f"URL: http://{b}:{a.port}/{key}/", flush=True)
    free = shutil.disk_usage(dest).free
    print(f"saving to {dest}  ({free / 1e9:.0f} GB free), allowed clients: {', '.join(a.allow)}", flush=True)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
