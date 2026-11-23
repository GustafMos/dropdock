import http.client
import json
import socket

import pytest

tk = pytest.importorskip("tkinter")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


def test_unknown_device_is_offered_and_let_in(root, tmp_path, monkeypatch):
    from dropdock import launcher as L
    monkeypatch.setattr(L, "CFG", str(tmp_path / "cfg" / "launcher.json"))
    monkeypatch.setattr(L, "local_addresses", lambda: [("127.0.0.1", "test")])
    monkeypatch.setattr(L, "reverse_name", lambda ip: "laptop")
    asked = []
    monkeypatch.setattr(L.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
    app = L.App(root, scan=False)
    port = free_port()
    app.dest.set(str(tmp_path / "in"))
    app.lib.set("")
    app.port.set(str(port))
    app.put_row("192.0.2.10", "someone else", "local network", True)   # 127.0.0.1 itself is not ticked
    log = []

    def get():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/" + app.cfg["key"] + "/")
        return c.getresponse().status

    def step(n=[0]):
        n[0] += 1
        if n[0] == 1:
            app.start()
        elif n[0] == 4:
            log.append(("before", get()))
        elif n[0] == 8:
            log.append(("after", get()))
            log.append(("links", [w.winfo_children()[0].get() for w in app.links.winfo_children()]))
            app.quit()
            return
        root.after(500, step)

    root.after(200, step)
    root.after(30000, root.quit)  # never hang the test run if a step fails
    root.mainloop()
    if app.proc:  # the watchdog fired: don't leave a server running
        app.proc.kill()
    assert log[0] == ("before", 403)
    assert asked and "127.0.0.1" in asked[0]
    assert log[1] == ("after", 200)
    assert log[2][1] == ["http://127.0.0.1:%d/%s/" % (port, app.cfg["key"])]
    cfg = json.load(open(L.CFG, encoding="utf-8"))
    assert set(cfg["allowed"]) == {"192.0.2.10", "127.0.0.1"} and cfg["library"] == ""


def test_server_cmd_turns_off_duplicate_check_when_library_empty(root, tmp_path, monkeypatch):
    from dropdock import launcher as L
    monkeypatch.setattr(L, "CFG", str(tmp_path / "launcher.json"))
    monkeypatch.setattr(L, "local_addresses", lambda: [("192.168.1.20", "local network")])
    app = L.App(root, scan=False)
    app.lib.set("")
    app.whole.set(True)
    app.settings()
    cmd, allow = app.server_cmd()
    assert cmd[cmd.index("--library") + 1] == "none"
    assert allow == ["192.168.1.0/24"]
    assert cmd[cmd.index("--bind") + 1] == "192.168.1.20"


def test_arp_parsing(monkeypatch):
    from dropdock import launcher as L
    win = ("Interface: 192.168.1.20 --- 0xf\n  Internet Address      Physical Address      Type\n"
           "  192.168.1.1           02-10-18-5e-8d-d8     dynamic\n"
           "  192.168.1.255         ff-ff-ff-ff-ff-ff     static\n")
    mac = "? (192.168.1.1) at 2:10:18:5e:8d:d8 on en0 ifscope [ethernet]\n? (192.168.1.7) at (incomplete) on en0\n"
    monkeypatch.setattr(L, "run", lambda cmd, timeout=10: win)
    assert L.arp_neighbours("192.168.1.20")[0] == "192.168.1.1"
    monkeypatch.setattr(L, "run", lambda cmd, timeout=10: mac)
    assert L.arp_neighbours("192.168.1.20") == ["192.168.1.1"]
