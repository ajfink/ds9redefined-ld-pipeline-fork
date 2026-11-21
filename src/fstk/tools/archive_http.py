"""Internet Archive item client: metadata, file selection, resumable MD5-verified downloads.

Python port of v38 ``download_archive.ps1`` (which wrapped curl). Rules kept:

* only ``https://archive.org/(details|download)/<identifier>`` URLs; identifiers ``[A-Za-z0-9._-]+``;
* only ``.tbc``, ``.tbc.json``, ``.efm`` and ``.efm.7z`` files; every selected file must carry a size
  and an MD5 in the live metadata, names must be plain file names, and every TBC needs its JSON;
* existing files are never deleted or replaced: a complete-sized file is MD5-checked (mismatch =
  stop, file kept); a shorter file is resumed with an HTTP Range request; a longer file = stop;
* a file counts only after its size and MD5 match; ``download-verified.csv`` grows per verified
  file, ``download-status.json`` says running/verified/failed, and ``download-manifest.csv`` (same
  columns as v38: Name, Bytes, MD5, ArchiveId, Status) is the completion record, never overwritten
  with different content.

Improvement over v38: on resume the partial prefix is hashed once and the MD5 continues over the
streamed bytes, so an 80 GB capture is not re-read after it finishes.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from fstk.core.errors import ArtifactError, ToolError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso

ARCHIVE_HOSTS = ("archive.org", "www.archive.org")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # leading alnum: rejects ".." (v38 relied on .NET Uri normalisation)
SELECT_RE = re.compile(r"\.tbc(?:\.json)?$|\.efm(?:\.7z)?$", re.IGNORECASE)
CHUNK = 4 * 1024 * 1024
MANIFEST_FIELDS = ("Name", "Bytes", "MD5", "ArchiveId", "Status")
VERIFIED_FIELDS = ("Name", "Bytes", "MD5", "ArchiveId")
RETRIES = 8


def archive_identifier(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ARCHIVE_HOSTS:
        raise ToolError(f"Only an https://archive.org URL is accepted: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        raise ToolError("Expected an Archive item URL of the form /details/<identifier> or /download/<identifier>.")
    ident = urllib.parse.unquote(parts[1])
    if not IDENTIFIER_RE.fullmatch(ident):
        raise ToolError(f"Unexpected Archive identifier: {ident}")
    return ident


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    md5: str

    @property
    def is_tbc(self) -> bool:
        return self.name.casefold().endswith(".tbc")


@dataclass(frozen=True)
class Selection:
    identifier: str
    title: str
    files: tuple[RemoteFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def capture_labels(self) -> list[str]:
        return sorted({PurePosixPath(f.name).stem.rsplit("-", 1)[-1] for f in self.files if f.is_tbc})


def select_files(identifier: str, metadata: dict[str, Any]) -> Selection:
    if not isinstance(metadata, dict) or not metadata.get("metadata") or not metadata.get("files"):
        raise ArtifactError(f"Archive metadata response has no item/file list for {identifier!r}.")
    chosen = []
    for f in metadata["files"]:
        name = str(f.get("name") or "")
        if not SELECT_RE.search(name):
            continue
        if not name or PurePosixPath(name).name != name or "\\" in name:
            raise ArtifactError(f"Unsafe Archive filename in metadata: {name!r}")
        size, md5 = str(f.get("size") or ""), str(f.get("md5") or "")
        if not size.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
            raise ArtifactError(f"Archive metadata lacks a valid size/MD5 for {name!r}; refusing an unverified download.")
        chosen.append(RemoteFile(name, int(size), md5.lower()))
    if not chosen:
        raise ArtifactError(f"No .tbc/.tbc.json/raw EFM inputs found in Archive item {identifier!r}.")
    names = {f.name for f in chosen}
    missing = [f"{f.name}.json" for f in chosen if f.is_tbc and f"{f.name}.json" not in names]
    if missing:
        raise ArtifactError(f"TBCs without paired JSON sidecars: {', '.join(missing)}. "
                            "Do not build a stack from an incomplete inventory.")
    title = str((metadata.get("metadata") or {}).get("title") or "")
    return Selection(identifier, title, tuple(sorted(chosen, key=lambda f: f.name)))


Opener = Callable[[urllib.request.Request, float], Any]


def _default_open(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class ArchiveClient:
    def __init__(self, *, base_url: str = "https://archive.org", opener: Opener | None = None,
                 timeout: float = 90.0, retry_delay: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.open = opener or _default_open
        self.timeout = timeout
        self.retry_delay = retry_delay

    def metadata(self, identifier: str) -> dict[str, Any]:
        url = f"{self.base_url}/metadata/{urllib.parse.quote(identifier)}"
        try:
            with self.open(urllib.request.Request(url, headers={"User-Agent": "fstk"}), self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise ToolError(f"Could not read Archive metadata for {identifier!r}: {exc}") from None

    def selection(self, identifier: str) -> Selection:
        return select_files(identifier, self.metadata(identifier))

    def file_url(self, identifier: str, name: str) -> str:
        return f"{self.base_url}/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}"

    # ------------------------------------------------------------------ one file
    def fetch(self, identifier: str, f: RemoteFile, dest_dir: Path, *,
              progress: Callable[[str, int, int], None] | None = None, cancel=None) -> str:
        """Download/resume/verify one file. Returns "verified" or "already-verified"."""
        path = dest_dir / f.name
        if path.exists() and not path.is_file():
            raise ArtifactError(f"Destination entry exists but is not a file; inspect without modifying it: {path}")
        have = path.stat().st_size if path.exists() else 0
        if have > f.size:
            raise ArtifactError(f"Existing file is larger than Archive metadata; leaving it untouched: {path}")
        digest = hashlib.md5()
        if have:
            with path.open("rb") as stream:
                while block := stream.read(CHUNK):
                    digest.update(block)
        if have == f.size:
            got = digest.hexdigest()
            if got != f.md5:
                raise ArtifactError(f"Existing complete-sized file has an MD5 mismatch; leaving it untouched: "
                                    f"{path} (got {got} expected {f.md5})")
            if progress:
                progress(f.name, f.size, f.size)
            return "already-verified"
        attempts = 0
        while have < f.size:
            if cancel is not None and cancel.is_set():
                raise ToolError(f"download cancelled; partial data retained for resume: {path}")
            headers = {"User-Agent": "fstk"}
            if have:
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(self.file_url(identifier, f.name), headers=headers)
            try:
                with self.open(req, self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    if have and status != 206:
                        raise ToolError(f"server ignored the resume request for {f.name} (HTTP {status}); "
                                        "partial data retained")
                    with path.open("ab") as out:
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise ToolError(f"download cancelled; partial data retained for resume: {path}")
                            block = resp.read(CHUNK)
                            if not block:
                                break
                            if have + len(block) > f.size:
                                raise ArtifactError(f"server sent more bytes than Archive metadata for {f.name}; "
                                                    "file retained, do not use it")
                            out.write(block)
                            digest.update(block)
                            have += len(block)
                            if progress:
                                progress(f.name, have, f.size)
                attempts = 0
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                    raise ToolError(f"Archive refused {f.name} (HTTP {exc.code}); partial data retained") from None
                attempts += 1
                if attempts > RETRIES:
                    raise ToolError(f"download of {f.name} failed after {RETRIES} retries: {exc}. "
                                    "Partial data is retained; rerun to resume.") from None
                time.sleep(self.retry_delay * attempts)
                have = path.stat().st_size
                digest = hashlib.md5()
                with path.open("rb") as stream:
                    while block := stream.read(CHUNK):
                        digest.update(block)
        got = digest.hexdigest()
        if got != f.md5:
            raise ArtifactError(f"MD5 mismatch for {f.name!r}: got {got}, expected {f.md5}. File retained; do not use it.")
        return "verified"

    # ------------------------------------------------------------------ whole item
    def download_item(self, sel: Selection, dest_dir: Path, *, source_url: str,
                      progress: Callable[[str, int, int], None] | None = None, cancel=None) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        status_path = dest_dir / "download-status.json"
        verified_path = dest_dir / "download-verified.csv"

        def status(state: str, detail: str = "") -> None:
            atomic_write_json(status_path, {"schema": "ld-pipeline-download-status-v1", "status": state,
                                            "archive_id": sel.identifier,
                                            "selected": [f.name for f in sel.files], "detail": detail,
                                            "updated_utc": utc_now_iso(), "writer": "fstk"})

        status("running")
        completed = False
        try:
            for f in sel.files:
                self.fetch(sel.identifier, f, dest_dir, progress=progress, cancel=cancel)
                new = not verified_path.exists()
                with verified_path.open("a", newline="", encoding="utf-8") as stream:
                    w = csv.DictWriter(stream, fieldnames=VERIFIED_FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow({"Name": f.name, "Bytes": f.size, "MD5": f.md5, "ArchiveId": sel.identifier})
            manifest = dest_dir / "download-manifest.csv"
            rows = [{"Name": f.name, "Bytes": str(f.size), "MD5": f.md5, "ArchiveId": sel.identifier,
                     "Status": "verified"} for f in sel.files]
            if manifest.exists():
                prior = read_download_manifest(manifest)
                key = lambda r: (r["Name"], int(r["Bytes"]), r["MD5"].lower(), r["ArchiveId"])  # noqa: E731
                if sorted(map(key, prior)) != sorted(map(key, rows)):
                    raise ArtifactError(f"Existing download manifest differs; preserving it and refusing to overwrite: {manifest}")
            else:
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=MANIFEST_FIELDS, quoting=csv.QUOTE_ALL)
                w.writeheader()
                w.writerows(rows)
                manifest.write_text(buf.getvalue(), encoding="utf-8-sig")
            note = dest_dir / "SOURCE-ITEM.txt"
            if note.exists():
                if f"Archive item: {sel.identifier}" not in note.read_text(encoding="utf-8-sig").splitlines():
                    raise ArtifactError(f"Existing source note belongs to another item; preserving it: {note}")
            else:
                note.write_text("\n".join([f"Archive item: {sel.identifier}", f"Title: {sel.title}",
                                           f"Source URL: {source_url}",
                                           "Downloaded file types: .tbc, .tbc.json, .efm/.efm.7z only.",
                                           "Manifest: download-manifest.csv", f"Verified files: {len(sel.files)}"])
                                + "\n", encoding="utf-8")
            completed = True
            return manifest
        finally:
            status("verified" if completed else "failed",
                   "" if completed else "downloader stopped before every selected file verified")


def read_download_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    for r in rows:
        if not {"Name", "Bytes", "MD5", "ArchiveId"} <= set(r):
            raise ArtifactError(f"{path} is not a download manifest (needs Name, Bytes, MD5, ArchiveId)")
    return rows
