"""Archive downloader against a local server that mimics archive.org (metadata + Range)."""
from __future__ import annotations

import csv
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fstk.core.errors import ArtifactError, ToolError
from fstk.tools.archive_http import ArchiveClient, archive_identifier, read_download_manifest, select_files

IDENT = "DS9-S02E18-LaserDisc-Copies"
FILES = {f"DS9-S02E18-Profit and Loss-{x}{ext}": (x * 3 + ext).encode() * 40000
         for x in ("L1", "P1") for ext in (".tbc", ".tbc.json", ".efm")}


def metadata(files=FILES, **override):
    rows = [{"name": n, "size": str(len(b)), "md5": hashlib.md5(b).hexdigest()} for n, b in files.items()]
    rows.append({"name": "DS9-S02E18-Profit and Loss-L1.log", "size": "5", "md5": "0" * 32})
    rows.append({"name": f"{IDENT}_meta.xml", "size": "9", "md5": "1" * 32})
    doc = {"metadata": {"title": IDENT}, "files": rows}
    doc.update(override)
    return doc


class Server:
    def __init__(self, files=FILES, meta=None, drop_after=None):
        self.files, self.meta, self.drop_after, self.requests = files, meta or metadata(files), drop_after, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range")))
                if self.path.startswith("/metadata/"):
                    body = json.dumps(outer.meta).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                import urllib.parse

                name = urllib.parse.unquote(self.path.split("/", 3)[3])
                data = outer.files[name]
                start = 0
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.split("=")[1].rstrip("-"))
                    self.send_response(206)
                else:
                    self.send_response(200)
                chunk = data[start:]
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if outer.drop_after and not rng and name.endswith(".tbc"):
                    self.wfile.write(chunk[: outer.drop_after])  # then drop the connection
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(chunk)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def client(self):
        return ArchiveClient(base_url=self.url, retry_delay=0.01)

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


def test_identifier_rules():
    assert archive_identifier(f"https://archive.org/details/{IDENT}") == IDENT
    assert archive_identifier(f"https://www.archive.org/download/{IDENT}/x.tbc") == IDENT
    for bad in (f"http://archive.org/details/{IDENT}", "https://evil.org/details/x", "https://archive.org/search/x",
                "https://archive.org/details/../x"):
        with pytest.raises(ToolError):
            archive_identifier(bad)


def test_selection_rules():
    sel = select_files(IDENT, metadata())
    assert len(sel.files) == 6 and sel.capture_labels == ["L1", "P1"]
    assert not any(f.name.endswith((".log", ".xml")) for f in sel.files)
    unpaired = {k: v for k, v in FILES.items() if k != "DS9-S02E18-Profit and Loss-P1.tbc.json"}
    with pytest.raises(ArtifactError, match="paired JSON"):
        select_files(IDENT, metadata(unpaired))
    bad = metadata()
    bad["files"][0]["md5"] = ""
    with pytest.raises(ArtifactError, match="valid size/MD5"):
        select_files(IDENT, bad)
    unsafe = metadata()
    unsafe["files"][0]["name"] = "../x.tbc"
    with pytest.raises(ArtifactError, match="Unsafe"):
        select_files(IDENT, unsafe)


def test_full_download_and_manifest(server, tmp_path):
    c = server.client()
    sel = c.selection(IDENT)
    manifest = c.download_item(sel, tmp_path, source_url=f"https://archive.org/details/{IDENT}")
    rows = read_download_manifest(manifest)
    assert sorted(r["Name"] for r in rows) == sorted(FILES) and {r["ArchiveId"] for r in rows} == {IDENT}
    for name, data in FILES.items():
        assert (tmp_path / name).read_bytes() == data
    assert json.loads((tmp_path / "download-status.json").read_text())["status"] == "verified"
    assert len(list(csv.DictReader((tmp_path / "download-verified.csv").open()))) == 6
    # rerun: everything already verified, manifest preserved, no media re-requested
    n = len(server.requests)
    c.download_item(sel, tmp_path, source_url="x")
    assert all(p.startswith("/metadata") for p, _ in server.requests[n:])


def test_resume_partial_and_reject_oversize_or_corrupt(server, tmp_path):
    c = server.client()
    sel = c.selection(IDENT)
    name = "DS9-S02E18-Profit and Loss-L1.tbc"
    (tmp_path / name).write_bytes(FILES[name][:1000])
    c.fetch(IDENT, next(f for f in sel.files if f.name == name), tmp_path)
    assert (tmp_path / name).read_bytes() == FILES[name]
    assert (server.requests[-1][1] or "").startswith("bytes=1000-")
    big = tmp_path / "big"
    big.mkdir()
    (big / name).write_bytes(FILES[name] + b"x")
    with pytest.raises(ArtifactError, match="larger than Archive"):
        c.fetch(IDENT, next(f for f in sel.files if f.name == name), big)
    assert (big / name).stat().st_size == len(FILES[name]) + 1      # untouched
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / name).write_bytes(b"\0" * len(FILES[name]))
    with pytest.raises(ArtifactError, match="MD5 mismatch"):
        c.fetch(IDENT, next(f for f in sel.files if f.name == name), bad)
    assert (bad / name).exists()


def test_dropped_connection_resumes_with_range(tmp_path):
    s = Server(drop_after=5000)
    try:
        c = s.client()
        sel = c.selection(IDENT)
        c.download_item(sel, tmp_path, source_url="x")
        assert any(r and r.startswith("bytes=5000-") for _, r in s.requests)
        for name, data in FILES.items():
            assert (tmp_path / name).read_bytes() == data
    finally:
        s.close()


def test_differing_existing_manifest_is_preserved(server, tmp_path):
    (tmp_path / "download-manifest.csv").write_text('"Name","Bytes","MD5","ArchiveId","Status"\n"x","1","a","OTHER","verified"\n')
    c = server.client()
    with pytest.raises(ArtifactError, match="differs"):
        c.download_item(c.selection(IDENT), tmp_path, source_url="x")
    assert "OTHER" in (tmp_path / "download-manifest.csv").read_text()
    assert json.loads((tmp_path / "download-status.json").read_text())["status"] == "failed"


def test_previously_verified_unchanged_files_are_not_rehashed(server, tmp_path):
    import os

    c = server.client()
    sel = c.selection(IDENT)
    c.download_item(sel, tmp_path, source_url="x")
    name = "DS9-S02E18-Profit and Loss-L1.tbc"
    f = next(x for x in sel.files if x.name == name)
    manifest = tmp_path / "download-manifest.csv"
    when = manifest.stat().st_mtime
    assert c.fetch(IDENT, f, tmp_path, verified_before=when) == "previously-verified"
    # a file changed after verification (newer mtime) is hashed again, and corruption is caught
    p = tmp_path / name
    p.write_bytes(b"\0" * f.size)
    os.utime(p, (when + 10, when + 10))
    with pytest.raises(ArtifactError, match="MD5 mismatch"):
        c.fetch(IDENT, f, tmp_path, verified_before=when)
    before = len(server.requests)
    p.write_bytes(FILES[name])
    os.utime(p, (when - 10, when - 10))
    c.download_item(sel, tmp_path, source_url="x")                # whole item: nothing fetched again
    assert all("/metadata/" in r[0] for r in server.requests[before:]) or len(server.requests) == before
