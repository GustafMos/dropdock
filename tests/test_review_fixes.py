"""Regression tests for problems found in review before the first release."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import pytest

from dropdock import server as S
from test_server import CH, KEY, conn, put, q, req, srv  # noqa: F401  (srv is a fixture)


def test_same_name_same_size_different_content_is_kept(srv):
    port, dest, _ = srv
    (dest / "x.bin").write_bytes(b"AAAAA")
    s, r = put(port, "x.bin", b"BBBBB", 0)
    assert r["done"] and r["note"] == "saved as x (2).bin"
    assert (dest / "x.bin").read_bytes() == b"AAAAA" and (dest / "x (2).bin").read_bytes() == b"BBBBB"


def test_same_name_identical_content_is_not_duplicated(srv):
    port, dest, _ = srv
    (dest / "x.bin").write_bytes(b"AAAAA")
    s, r = put(port, "x.bin", b"AAAAA", 0)
    assert r["done"] and r["note"] == "already there"
    assert sorted(os.listdir(dest)) == [".partial", "x.bin"]


def test_late_retry_after_rename_does_not_upload_again(srv):
    port, dest, _ = srv
    (dest / "x.bin").write_bytes(b"old!")
    data = os.urandom(CH + 10)
    put(port, "x.bin", data, 0, "f00d")
    s, r = put(port, "x.bin", data, 1, "f00d")
    assert r["note"] == "saved as x (2).bin"
    s, r = put(port, "x.bin", data, 1, "f00d")  # the answer to the last piece got lost; the page retries
    assert r["done"] and r["note"] == "already there"
    s, st = req(conn(port), "GET", f"/{KEY}/status?" + q("x.bin", len(data), fp="f00d"))
    assert st["done"]
    assert sorted(os.listdir(dest)) == [".partial", "x (2).bin", "x.bin"]


def test_part_without_progress_record_is_not_trusted(srv):
    port, dest, _ = srv
    data = os.urandom(3 * CH)
    part = dest / ".partial" / f"z.bin.{len(data)}.part"
    part.write_bytes(b"\0" * (3 * CH))          # e.g. only piece 2 arrived, then the record was lost
    s, st = req(conn(port), "GET", f"/{KEY}/status?" + q("z.bin", len(data)))
    assert st["missing"] == [0, 1, 2]


def test_deleted_part_while_running_restarts_cleanly(srv):
    port, dest, _ = srv
    data = os.urandom(2 * CH)
    put(port, "y.bin", data, 0)
    for f in os.listdir(dest / ".partial"):
        os.remove(dest / ".partial" / f)
    s, st = req(conn(port), "GET", f"/{KEY}/status?" + q("y.bin", len(data)))
    assert st["missing"] == [0, 1]


def test_absurd_size_is_refused_quickly(srv):
    port, _, _ = srv
    t = time.time()
    s, r = req(conn(port), "GET", f"/{KEY}/status?" + q("huge.bin", 3 * 10 ** 14))
    assert s == 400 and time.time() - t < 1


def test_negative_content_length_on_log(srv):
    port, _, _ = srv
    sk = socket.create_connection(("127.0.0.1", port), timeout=5)
    sk.sendall(f"POST /{KEY}/log HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\nhello".encode())
    assert sk.recv(100).startswith(b"HTTP/1.1 400")
    sk.close()


def test_long_and_non_ascii_names(srv):
    port, dest, _ = srv
    long = "é" * 200 + ".mp4"
    s, r = put(port, long, b"data", 0)
    assert s == 200 and r["done"]
    (saved,) = [f for f in os.listdir(dest) if f != ".partial"]
    assert saved.endswith(".mp4") and len(saved.encode()) <= S.NAME_BYTES


def test_log_injection_is_flattened():
    assert "\n" not in S.clean("hi\nrefused client 10.9.9.9")


def test_second_server_on_same_port_fails(srv, tmp_path):
    port, _, _ = srv
    with pytest.raises(OSError):
        S.make_servers(["127.0.0.1"], port, ["127.0.0.0/8"], str(tmp_path / "other"), KEY)


def test_refused_clients_without_key_are_not_reported(tmp_path, capsys):
    s = S.make_servers(["127.0.0.1"], 0, ["10.0.0.0/8"], str(tmp_path / "d"), KEY)[0]
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        port = s.server_address[1]
        assert req(conn(port), "GET", "/nope/")[0] == 403
        assert "refused client" not in capsys.readouterr().out
        assert req(conn(port), "GET", f"/{KEY}/")[0] == 403
        assert "refused client 127.0.0.1" in capsys.readouterr().out
    finally:
        s.shutdown()
        s.server_close()


def test_non_ascii_name_through_a_narrow_console(tmp_path):
    """The launcher reads the server through a pipe; a cp1252 pipe must not break uploads of other scripts."""
    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    p = port.getsockname()[1]
    port.close()
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    proc = subprocess.Popen([sys.executable, "-u", S.__file__, "--bind", "127.0.0.1", "--port", str(p), "--allow",
                             "127.0.0.1", "--dest", str(tmp_path / "in"), "--key", KEY], stdout=subprocess.PIPE,
                            env=env)
    try:
        proc.stdout.readline()
        proc.stdout.readline()
        s, r = put(p, "日本語.bin", b"x" * 10, 0)
        assert s == 200 and r["done"]
        assert (tmp_path / "in" / "日本語.bin").exists()
    finally:
        proc.terminate()
        proc.wait(5)


def test_duplicate_index_sees_files_uploaded_this_run(srv):
    port, dest, lib = srv
    servers_lib = S.Library(str(dest))
    servers_lib.find(5, b"x")                    # builds the (empty) index
    (dest / "new.bin").write_bytes(b"hello")
    servers_lib.add(str(dest / "new.bin"), 5)
    assert servers_lib.find(5, b"hello") == str(dest / "new.bin")


def test_second_different_file_with_same_name_and_size_is_kept(srv):
    """Two different files, same name and size, sent one after the other (e.g. from two phones)."""
    port, dest, _ = srv
    s, r = put(port, "clip.mov", b"first!", 0, "aaaa")
    assert r["done"] and r["note"] == ""
    s, st = req(conn(port), "GET", f"/{KEY}/status?" + q("clip.mov", 6, fp="bbbb"))
    assert st["missing"] == [0]
    s, r = put(port, "clip.mov", b"second", 0, "bbbb")
    assert r["done"] and r["note"] == "saved as clip (2).mov"
    assert (dest / "clip.mov").read_bytes() == b"first!" and (dest / "clip (2).mov").read_bytes() == b"second"


def test_bad_fingerprint_refused(srv):
    port, _, _ = srv
    assert req(conn(port), "GET", f"/{KEY}/status?" + q("a.bin", 3, fp="../x"))[0] == 400
