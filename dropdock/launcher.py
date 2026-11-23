"""dropdock launcher: a small window for starting the dropdock server.

Pick the folder the files go to, tick the devices that may send (found on the local network and on
Tailscale), press Start. The window shows the link to open on the other device and the server's log, and
asks whether to let in a device that opens the link but isn't ticked yet.
"""
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from dropdock import server as srv
except ImportError:  # run straight from a source checkout
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server as srv

APP = "dropdock"
SERVER = os.path.abspath(srv.__file__)
WINDOWS = sys.platform == "win32"
NOWIN = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW
TAILNET = ipaddress.ip_network("100.64.0.0/10")
CHECK = "\u2714"


def config_path():
    if WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP, "launcher.json")


CFG = config_path()
_DEST = os.path.join(os.path.expanduser("~"), "Downloads", APP)
DEFAULTS = {"dest": _DEST, "library": _DEST, "port": 8765, "key": None, "allowed": {}, "whole_lan": False}


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    if not cfg.get("key"):
        cfg["key"] = secrets.token_urlsafe(9)
    return cfg


def save_cfg(cfg):
    try:
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        tmp = CFG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        os.replace(tmp, CFG)
    except OSError:
        pass


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_tailscale(ip):
    return ipaddress.ip_address(ip) in TAILNET


def local_addresses():
    """[(address, label)] for this computer's private IPv4 addresses (home/office network, Tailscale)."""
    out = []
    for ip in srv.local_ipv4():
        if is_tailscale(ip):
            out.append((ip, "Tailscale"))
        elif ipaddress.ip_address(ip).is_private:
            out.append((ip, "local network"))
    return out


def lan_of(ip):
    return ipaddress.ip_network(ip + "/24", strict=False)


# ---- finding devices -------------------------------------------------------------------------------------
# NetBIOS node-status query for "*": Windows machines (and Samba) answer with their name.
NBSTAT = b"\x13\x37\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x20CK" + b"AA" * 15 + b"\x00\x00\x21\x00\x01"


def netbios_sweep(net, wait=2.0):
    """Ask every address in `net` for its machine name. Returns {ip: name} for those that answer."""
    names = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        for host in net.hosts():
            try:
                s.sendto(NBSTAT, (str(host), 137))
            except OSError:
                pass
        end = time.time() + wait
        while time.time() < end:
            try:
                data, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:  # Windows reports an ICMP "port unreachable" from one host this way
                continue
            except OSError:
                break
            try:
                for i in range(data[56]):
                    rec = data[57 + 18 * i: 57 + 18 * (i + 1)]
                    if rec[15] == 0 and not (rec[16] & 0x80):
                        names[ip] = rec[:15].decode("ascii", "replace").strip()
                        break
            except IndexError:
                pass
    finally:
        s.close()
    return names


def arp_neighbours(own_ip):
    """Addresses this computer has recently talked to (the sweep above fills this table)."""
    out = run(["arp", "-a", "-N", own_ip] if WINDOWS else ["arp", "-an"])
    if not out and not WINDOWS and shutil.which("ip"):
        out = run(["ip", "-4", "neigh"])
    mac = r"[0-9a-f]{1,2}(?:[:-][0-9a-f]{1,2}){5}"
    return [m.group(1) for m in re.finditer(r"\(?(\d+\.\d+\.\d+\.\d+)\)?[^\n]*?" + mac, out, re.I)]


def reverse_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except OSError:
        return ""


def tailscale_cli():
    for p in (shutil.which("tailscale"), r"C:\Program Files\Tailscale\tailscale.exe",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if p and os.path.exists(p):
            return p
    return None


def tailscale_peers():
    """[(ip, name, os, online)] from `tailscale status --json`."""
    exe = tailscale_cli()
    if not exe:
        return []
    try:
        st = json.loads(run([exe, "status", "--json"]) or "{}")
    except ValueError:
        return []
    out = []
    for p in (st.get("Peer") or {}).values():
        v4 = [a for a in p.get("TailscaleIPs", []) if "." in a]
        if v4:
            name = p.get("HostName", "")
            if not name or name == "localhost":
                name = (p.get("DNSName") or "").split(".")[0] or name
            out.append((v4[0], name, p.get("OS", ""), bool(p.get("Online"))))
    return out


def discover(own):
    """Every device we can see: [(ip, name, where)]."""
    found = {}
    for ip, label in own:
        if label != "local network":
            continue
        net = lan_of(ip)
        names = netbios_sweep(net)
        for n in set(arp_neighbours(ip)) | set(names):
            a = ipaddress.ip_address(n)
            if a in net and n != ip and a != net.broadcast_address and a != net.network_address:
                found[n] = names.get(n, "")
        missing = [n for n, v in found.items() if not v]
        res = {}
        threads = [threading.Thread(target=lambda n=n: res.__setitem__(n, reverse_name(n)), daemon=True)
                   for n in missing]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        for n in missing:
            found[n] = res.get(n, "")
    rows = [(ip, name or "(unnamed device)", "local network") for ip, name in found.items()]
    for ip, name, osname, online in tailscale_peers():
        rows.append((ip, name, "Tailscale, %s%s" % (osname, "" if online else ", offline")))
    rows.sort(key=lambda r: (r[2].startswith("Tailscale"), ipaddress.ip_address(r[0])))
    return rows


# ---- window ----------------------------------------------------------------------------------------------
class App:
    def __init__(self, root, scan=True):
        self.root, self.cfg = root, load_cfg()
        self.proc, self.q, self.asked = None, queue.Queue(), set()
        self.own = local_addresses()
        root.title("dropdock")
        root.geometry("760x640")
        root.minsize(640, 520)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        pad = dict(padx=8, pady=4)

        f = ttk.LabelFrame(root, text="1. Where should the files go?")
        f.pack(fill="x", **pad)
        self.dest = tk.StringVar(value=self.cfg["dest"])
        self.lib = tk.StringVar(value=self.cfg["library"])
        self._folder_row(f, "Save into:", self.dest, 0)
        self._folder_row(f, "Skip files already in:", self.lib, 1)
        ttk.Label(f, text="Files this computer already has anywhere in that folder are marked \"already there\" "
                          "instead of being sent again. Leave empty to turn this off.",
                  foreground="#666", wraplength=560).grid(row=2, column=1, columnspan=2, sticky="w")
        f.columnconfigure(1, weight=1)

        f = ttk.LabelFrame(root, text="2. Which devices may send files?")
        f.pack(fill="both", expand=True, **pad)
        cols = ("allow", "name", "ip", "where")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=8, selectmode="none")
        for c, t, w in zip(cols, ("Allow", "Name", "Address", "Network"), (50, 220, 130, 200)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c == "allow" else "w", stretch=c != "allow")
        self.tree.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        self.tree.bind("<Button-1>", self.toggle)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=4, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.scan_btn = ttk.Button(f, text="Scan network", command=self.scan)
        self.scan_btn.grid(row=1, column=0, sticky="w", padx=4)
        ttk.Label(f, text="Add address:").grid(row=1, column=1, sticky="e")
        self.manual = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.manual, width=18)
        e.grid(row=1, column=2, sticky="w")
        e.bind("<Return>", lambda _: self.add_manual())
        ttk.Button(f, text="Add", command=self.add_manual).grid(row=1, column=3, sticky="w", padx=4)
        self.lans = sorted({str(lan_of(ip)) for ip, lab in self.own if lab == "local network"})
        self.whole = tk.BooleanVar(value=self.cfg.get("whole_lan", False))
        ttk.Checkbutton(f, variable=self.whole, command=self.changed,
                        text="Allow any device on my local network (%s) - it still needs the link"
                        % ", ".join(self.lans or ["none found"])).grid(row=2, column=0, columnspan=4,
                                                                      sticky="w", padx=4)
        self.status = ttk.Label(f, text="Click a row to tick or untick it.", foreground="#666")
        self.status.grid(row=3, column=0, columnspan=4, sticky="w", padx=4)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.rows = {}
        for ip, (name, where) in self.cfg["allowed"].items():
            self.put_row(ip, name, where, True)

        f = ttk.Frame(root)
        f.pack(fill="x", **pad)
        self.go = ttk.Button(f, text="Start", command=self.start_stop)
        self.go.pack(side="left")
        ttk.Label(f, text="  Port:").pack(side="left")
        self.port = tk.StringVar(value=str(self.cfg["port"]))
        ttk.Entry(f, textvariable=self.port, width=6).pack(side="left")
        ttk.Button(f, text="New link", command=self.new_key).pack(side="left", padx=8)
        self.state = ttk.Label(f, text="Stopped", foreground="#a00")
        self.state.pack(side="left", padx=8)

        f = ttk.LabelFrame(root, text="3. Open this link on the other device")
        f.pack(fill="x", **pad)
        self.links = ttk.Frame(f)
        self.links.pack(fill="x")
        ttk.Label(self.links, text="(press Start)", foreground="#666").pack(anchor="w", padx=4)

        f = ttk.LabelFrame(root, text="Log")
        f.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(f, height=8, wrap="word", state="disabled", font=("TkFixedFont", 9))
        self.log.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(f, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

        root.after(200, self.pump)
        if scan:
            self.scan()

    # -- helpers
    def _folder_row(self, f, label, var, r):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(f, textvariable=var).grid(row=r, column=1, sticky="ew", pady=2)
        ttk.Button(f, text="Browse...", command=lambda: self._browse(var)).grid(row=r, column=2, padx=4)

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() if os.path.isdir(var.get()) else None)
        if d:
            var.set(os.path.normpath(d))

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def put_row(self, ip, name, where, allowed=None):
        if ip in self.rows:
            iid = self.rows[ip]
            if name and name != "(unnamed device)":
                self.tree.set(iid, "name", name)
            if where:
                self.tree.set(iid, "where", where)
            if allowed is not None:
                self.tree.set(iid, "allow", CHECK if allowed else "")
            return
        self.rows[ip] = self.tree.insert("", "end", values=(CHECK if allowed else "", name, ip, where))

    def allowed(self):
        return {self.tree.set(i, "ip"): (self.tree.set(i, "name"), self.tree.set(i, "where"))
                for i in self.tree.get_children() if self.tree.set(i, "allow")}

    def toggle(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid:
            self.tree.set(iid, "allow", "" if self.tree.set(iid, "allow") else CHECK)
            self.changed()

    def changed(self):
        if self.proc:
            self.status.configure(text="Changed - restarting (uploads in progress continue by themselves).")
            self.restart()

    def add_manual(self):
        txt = self.manual.get().strip()
        try:
            ipaddress.ip_network(txt, strict=False)
        except ValueError:
            messagebox.showerror(APP, "'%s' is not an address like 192.168.1.50 or a network like "
                                      "192.168.1.0/24" % txt)
            return
        self.put_row(txt, "(added by hand)", "", True)
        self.manual.set("")
        self.changed()

    def new_key(self):
        if messagebox.askyesno(APP, "Make a new link? The old link stops working."):
            self.cfg["key"] = secrets.token_urlsafe(9)
            save_cfg(self.cfg)
            self.changed()

    # -- discovery
    def scan(self):
        self.scan_btn.configure(state="disabled")
        self.status.configure(text="Looking for devices on the network...")

        def work():
            try:
                rows = discover(self.own)
            except Exception as e:  # discovery is best effort
                rows = []
                self.q.put(("line", "scan failed: %s" % e, None))
            self.q.put(("scan", rows))
        threading.Thread(target=work, daemon=True).start()

    # -- server
    def settings(self):
        self.cfg.update(dest=self.dest.get().strip(), library=self.lib.get().strip(),
                        allowed={ip: list(v) for ip, v in self.allowed().items()}, whole_lan=self.whole.get())
        try:
            self.cfg["port"] = int(self.port.get())
        except ValueError:
            pass
        save_cfg(self.cfg)

    def start_stop(self):
        if self.proc:
            self.stop()
        else:
            self.start()

    def server_cmd(self):
        allow = list(self.cfg["allowed"])
        if self.whole.get():
            allow += self.lans
        py = sys.executable
        if WINDOWS and py.lower().endswith("pythonw.exe"):
            py = py[:-len("pythonw.exe")] + "python.exe"
        cmd = [py, "-u", SERVER, "--port", str(self.cfg["port"]), "--dest", self.cfg["dest"],
               "--key", self.cfg["key"], "--library", self.cfg["library"] or "none"]
        for b in [ip for ip, _ in self.own] or ["0.0.0.0"]:
            cmd += ["--bind", b]
        for a in allow:
            cmd += ["--allow", a]
        return cmd, allow

    def start(self):
        self.settings()
        cmd, allow = self.server_cmd()
        if not allow:
            messagebox.showinfo(APP, "Tick at least one device (or the local-network box) first.\n\n"
                                     "Can't find the device in the list? Tick any row, start, then open the "
                                     "link on it - this window will ask whether to let it in.")
            return
        if not self.cfg["dest"]:
            messagebox.showerror(APP, "Choose the folder the files should go to.")
            return
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                         creationflags=NOWIN, bufsize=1, encoding="utf-8", errors="replace")
        except OSError as e:
            messagebox.showerror(APP, "Could not start the server:\n%s" % e)
            return
        self.write("--- started %s" % time.strftime("%H:%M:%S"))
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.go.configure(text="Stop")
        self.state.configure(text="Running", foreground="#080")
        for w in self.links.winfo_children():
            w.destroy()

    def reader(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line, proc))
        self.q.put(("exit", proc.wait(), proc))

    def stop(self):
        p, self.proc = self.proc, None
        if p:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.go.configure(text="Start")
        self.state.configure(text="Stopped", foreground="#a00")

    def restart(self):
        self.stop()
        self.start()

    def link_row(self, url, where):
        row = ttk.Frame(self.links)
        row.pack(fill="x", padx=4, pady=1)
        e = ttk.Entry(row, font=("TkFixedFont", 10))
        e.insert(0, url)
        e.configure(state="readonly")
        e.pack(side="left", fill="x", expand=True)
        ttk.Label(row, text=" " + where, width=18).pack(side="left")

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.status.configure(text="Link copied.")
        ttk.Button(row, text="Copy", command=copy).pack(side="left")

    def pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "scan":
                    for ip, name, where in msg[1]:
                        self.put_row(ip, name, where)
                    n = sum(1 for r in msg[1] if r[2] == "local network")
                    self.status.configure(text="Found %d device(s) on the local network. Click a row to tick it. "
                                               "Don't see the device? Open the link on it anyway - this window "
                                               "will ask." % n)
                    self.scan_btn.configure(state="normal")
                elif msg[0] == "line":
                    line, proc = msg[1], msg[2]
                    if proc is not None and proc is not self.proc:
                        continue
                    self.write(line)
                    m = re.match(r"URL: (http://([\d.]+):\d+/\S+)", line)
                    if m:
                        where = dict(self.own).get(m.group(2), "")
                        self.link_row(m.group(1), "(%s)" % where if where else "")
                    m = re.search(r"refused client ([\d.]+)", line)
                    if m and m.group(1) not in self.asked:
                        self.asked.add(m.group(1))
                        ip = m.group(1)  # look the name up off the UI thread, then ask
                        threading.Thread(target=lambda ip=ip: self.q.put(("ask", ip, reverse_name(ip))),
                                         daemon=True).start()
                elif msg[0] == "ask":
                    self.ask_allow(msg[1], msg[2])
                elif msg[0] == "exit":
                    if msg[2] is self.proc:
                        self.write("--- server stopped (exit code %s). Is the port already in use? Try another "
                                   "port." % msg[1])
                        self.proc = None
                        self.go.configure(text="Start")
                        self.state.configure(text="Stopped", foreground="#a00")
        except queue.Empty:
            pass
        self.root.after(200, self.pump)

    def ask_allow(self, ip, name=""):
        if messagebox.askyesno(APP, "%s (%s) opened the link but isn't allowed yet.\n\nLet it send files?"
                               % (name or "A device", ip)):
            self.put_row(ip, name or "(unnamed device)", "", True)
            self.changed()

    def quit(self):
        if self.proc and not messagebox.askyesno(APP, "Stop receiving and close? Unfinished uploads can be "
                                                      "resumed later."):
            return
        self.settings()
        self.stop()
        self.root.destroy()


def main():
    if WINDOWS:
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
