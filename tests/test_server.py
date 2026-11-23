import hashlib
import http.client
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest

from dropdock import server as S

CH = S.CHUNK
KEY = "k3yk3y"


@pytest.fixture
def srv(tmp_path):
    """A server on 127.0.0.1 with a random port; yields (port, dest, library)."""
    dest, lib = tmp_path / "dest", tmp_path / "lib"
    lib.mkdir()
    servers = S.make_servers(["127.0.0.1"], 0, ["127.0.0.0/8"], str(dest), KEY, str(lib), host_label="test<box>")
    s = servers[0]
    threading.Thread(target=s.serve_forever, daemon=True).start()
    S._states.clear()
    yield s.server_address[1], dest, lib
    s.shutdown()
    s.server_close()


def conn(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=30)


def req(c, method, path, body=None):
    c.request(method, path, body=body)
    r = c.getresponse()
    b = r.read()
    try:
        return r.status, json.loads(b or b"null")
    except ValueError:
        return r.status, b


def q(name, size, **k):
    return urlencode({"name": name, "size": size, **k})


def put(port, name, data, i, fp=""):
    return req(conn(port), "PUT", f"/{KEY}/put?" + q(name, len(data), index=i, fp=fp), data[i * CH:(i + 1) * CH])


def test_resumable_parallel_upload(srv):
    port, dest, _ = srv
    data = os.urandom(5 * CH + 12345)          # 6 pieces, last one short
    name, size = "clip.mp4", len(data)
    part = dest / ".partial" / f"{name}.{size}.ab12.part"
    part.write_bytes(data[:2 * CH])            # a run that stopped after two pieces
    (dest / ".partial" / f"{name}.{size}.ab12.part.json").write_text(json.dumps({"chunk": CH, "size": size, "done": [0, 1]}))
    c = conn(port)
    s, st = req(c, "GET", f"/{KEY}/status?" + q(name, size, fp="ab12"))
    assert st["missing"] == [2, 3, 4, 5] and st["total"] == 6
    # a piece cut off halfway is not recorded
    sk = socket.create_connection(("127.0.0.1", port))
    sk.sendall((f"PUT /{KEY}/put?{q(name, size, index=3, fp="ab12")} HTTP/1.1\r\nHost: x\r\nContent-Length: {CH}\r\n\r\n")
               .encode() + data[3 * CH:3 * CH + CH // 2])
    sk.close()
    time.sleep(0.3)
    assert req(c, "GET", f"/{KEY}/status?" + q(name, size, fp="ab12"))[1]["missing"] == [2, 3, 4, 5]
    # wrong length refused
    assert req(conn(port), "PUT", f"/{KEY}/put?" + q(name, size, index=2), data[:100])[0] == 400
    # keep-alive: one connection serves several requests
    c2 = conn(port)
    assert req(c2, "GET", f"/{KEY}/status?" + q(name, size))[0] == req(c2, "GET", f"/{KEY}/status?" + q(name, size))[0] == 200
    # remaining pieces in parallel, out of order, one twice
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda i: put(port, name, data, i, 'ab12'), [5, 3, 4, 3]))
    assert all(s == 200 for s, _ in res)
    s, r = put(port, name, data, 2, 'ab12')
    assert r["done"]
    got = (dest / name).read_bytes()
    assert hashlib.sha256(got).digest() == hashlib.sha256(data).digest()
    assert os.listdir(dest / ".partial") == []
    # a late retry after the file is finished is harmless
    s, r = put(port, name, data, 1, 'ab12')
    assert r["done"] and r["note"] == "already there"
    assert (dest / name).read_bytes() == got and os.listdir(dest / ".partial") == []
    assert req(conn(port), "GET", f"/{KEY}/status?" + q(name, size, fp="ab12"))[1]["done"]


def test_same_name_different_content_gets_new_name(srv):
    port, dest, _ = srv
    (dest / "a.bin").write_bytes(b"old")
    data = b"new content"
    s, r = put(port, "a.bin", data, 0)
    assert r["done"]
    assert (dest / "a.bin").read_bytes() == b"old" and (dest / "a (2).bin").read_bytes() == data


def test_empty_file(srv):
    port, dest, _ = srv
    s, st = req(conn(port), "GET", f"/{KEY}/status?" + q("empty.txt", 0))
    assert st["done"] and (dest / "empty.txt").read_bytes() == b""


def samples(data):
    if len(data) <= S.SAMPLE_N * S.SAMPLE_L:
        return data
    L, N = S.SAMPLE_L, S.SAMPLE_N
    return b"".join(data[i * (len(data) - L) // (N - 1):][:L] for i in range(N))


@pytest.mark.parametrize("size", [1000, S.SAMPLE_N * S.SAMPLE_L, 3 * 1024 * 1024 + 7])
def test_duplicate_check(srv, size):
    port, _, lib = srv
    data = os.urandom(size)
    (lib / "sub").mkdir()
    (lib / "sub" / "have.mov").write_bytes(data)
    s, r = req(conn(port), "POST", f"/{KEY}/check?" + q("phone_name.mov", size), samples(data))
    assert s == 200 and r["dup"] == os.path.join("sub", "have.mov")
    other = bytearray(samples(data))
    other[len(other) // 2] ^= 0xFF
    assert req(conn(port), "POST", f"/{KEY}/check?" + q("x.mov", size), bytes(other))[1]["dup"] is None
    assert req(conn(port), "POST", f"/{KEY}/check?" + q("x.mov", size + 1),
               samples(data + b"!"))[1]["dup"] is None


def test_wrong_key_and_refused_client(srv, tmp_path):
    port, _, _ = srv
    assert req(conn(port), "GET", "/wrong/status?" + q("a", 1))[0] == 404
    servers = S.make_servers(["127.0.0.1"], 0, ["10.0.0.0/8"], str(tmp_path / "d2"), KEY)
    s = servers[0]
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        assert req(conn(s.server_address[1]), "GET", f"/{KEY}/")[0] == 403
    finally:
        s.shutdown()
        s.server_close()


def test_page_escapes_host_name(srv):
    port, _, _ = srv
    s, body = req(conn(port), "GET", f"/{KEY}/")
    assert s == 200
    text = body.decode()
    assert "<title>Send to test&lt;box&gt;</title>" in text
    assert 'const HOST = "test\\u003cbox>";' in text
    assert "<box>" not in text


def test_not_enough_space(srv, monkeypatch):
    port, _, _ = srv
    real = S.shutil.disk_usage
    monkeypatch.setattr(S.shutil, "disk_usage", lambda p: real(p)._replace(free=10))
    s, r = req(conn(port), "GET", f"/{KEY}/status?" + q("big.bin", 10 ** 9))
    assert s == 507 and "not enough space" in r["error"]


def test_log_is_capped(srv):
    port, _, _ = srv
    assert req(conn(port), "POST", f"/{KEY}/log", b"x" * 100000)[0] == 204


@pytest.mark.parametrize("raw,expect", [
    ("photo.jpg", "photo.jpg"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\x\\video.mp4", "video.mp4"),
    ("a:b?.txt", "a_b_.txt"),
    ("CON.txt", "_CON.txt"),
    ("trailing. ", "trailing"),
])
def test_safe_name(raw, expect):
    assert S.safe_name(raw) == expect


@pytest.mark.parametrize("raw", ["", "..", ".partial", "  "])
def test_safe_name_rejects(raw):
    with pytest.raises(ValueError):
        S.safe_name(raw)
