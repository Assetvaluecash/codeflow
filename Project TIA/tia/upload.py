"""Code upload (UC1 ingestion): a zip archive or a whole folder from the user's own drive becomes a git repository in TIA's
storage, so uploaded code gets the same revisions, diffs, graph and impact analysis as a cloned repository.

Every upload is a full snapshot: it becomes one commit on `main`. Uploading the same name again adds another commit, so
the difference between two uploads is analysed exactly like a pull request (base `main~1`, head `main`).

Security: uploaded bytes are untrusted. Paths are validated before anything touches the disk (no traversal, absolute or
drive paths, Windows device names, alternate data streams, symlinks or special files, case-colliding names); the size and
file-count caps are enforced on the bytes actually written, not on what an archive claims; nothing is ever executed; git is
used with line-ending conversion and ignore files disabled so the stored content is exactly the uploaded content.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .repos import IngestError, _rmtree

GIB = 1024 ** 3
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".idea"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
CHUNK = 1 << 20


@dataclass(frozen=True)
class UploadConfig:
    enabled: bool = True
    max_bytes: int = 2 * GIB             # per upload, enforced on the bytes actually written
    max_files: int = 200_000
    session_ttl_s: int = 24 * 3600

    @classmethod
    def from_env(cls) -> "UploadConfig":
        e = os.environ.get
        return cls(enabled=e("TIA_ALLOW_UPLOADS", "true").lower() in ("1", "true", "yes"),
                   max_bytes=int(e("TIA_MAX_UPLOAD_BYTES", str(2 * GIB))), max_files=int(e("TIA_MAX_UPLOAD_FILES", "200000")),
                   session_ttl_s=int(e("TIA_UPLOAD_SESSION_TTL_S", str(24 * 3600))))


class UploadTooLarge(IngestError):
    pass


def clean_member(name: str) -> Optional[str]:
    """Validate an archive/folder member name. Returns the safe relative POSIX path, None if it is a directory entry or
    inside an ignored directory (node_modules, .git, ...), and raises IngestError if the name is unsafe."""
    raw = name.replace("\\", "/")
    if _BAD_CHARS.search(raw):
        raise IngestError(f"unsafe file name in upload: {name!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise IngestError(f"absolute path in upload: {name!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return None
    if any(p == ".." for p in parts):
        raise IngestError(f"path traversal in upload: {name!r}")
    for p in parts:
        if p.endswith((" ", ".")) or p.split(".")[0].lower() in _RESERVED:
            raise IngestError(f"file name not allowed in upload: {name!r}")
    if len(raw) > 1024:
        raise IngestError("file path too long in upload")
    if raw.endswith("/"):
        return None
    if any(p in SKIP_DIRS for p in parts):
        return None
    return "/".join(parts)


class Session:
    """A staging directory for one upload. Tenant-bound; every write is quota-checked and path-checked.
    Metadata is held in memory for the duration of a request; call flush() when done (also after an error)."""

    def __init__(self, root: Path, sid: str, cfg: UploadConfig):
        self.dir, self.sid, self.cfg = root / sid, sid, cfg
        self.stage = self.dir / "tree"
        self.meta_path = self.dir / "meta.json"
        self.paths_path = self.dir / "paths.txt"
        self.m: dict = {}
        self._seen: Optional[dict] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    @staticmethod
    def create(root: Path, cfg: UploadConfig, tenant: str, name: str) -> "Session":
        root.mkdir(parents=True, exist_ok=True)
        for d in root.iterdir():                                   # expire abandoned sessions
            try:
                if d.is_dir() and time.time() - d.stat().st_mtime > cfg.session_ttl_s:
                    _rmtree(d)
            except OSError:
                pass
        s = Session(root, uuid.uuid4().hex, cfg)
        s.stage.mkdir(parents=True)
        s.paths_path.write_text("")
        s.m = {"tenant": tenant, "name": name, "bytes": 0, "files": 0, "skipped": 0, "source": None, "created": time.time()}
        s.flush()
        return s

    @staticmethod
    def open(root: Path, sid: str, cfg: UploadConfig, tenant: str) -> "Session":
        if not re.fullmatch(r"[0-9a-f]{32}", sid or ""):
            raise IngestError("unknown upload")
        s = Session(root, sid, cfg)
        if not s.meta_path.exists():
            raise IngestError("unknown upload")
        s.m = json.loads(s.meta_path.read_text())
        if s.m["tenant"] != tenant:
            raise IngestError("unknown upload")
        return s

    def flush(self):
        if self.dir.exists():
            self.meta_path.write_text(json.dumps(self.m))
            os.utime(self.dir)                                       # activity keeps the session from expiring

    def discard(self):
        _rmtree(self.dir)

    def use_source(self, source: str):
        if self.m["source"] not in (None, source):
            raise IngestError(f"this upload already receives a {self.m['source']}; start a new upload to send a {source}")
        self.m["source"] = source

    # ---- writes ----------------------------------------------------------------------------------------------
    def seen(self) -> dict:
        if self._seen is None:
            self._seen = {line.lower(): line for line in self.paths_path.read_text().splitlines()}
        return self._seen

    def add(self, name: str, chunks: Iterable[bytes]) -> bool:
        """Write one file. Returns False if it was skipped (ignored directory). Enforces path safety and the caps."""
        rel = clean_member(name)
        if rel is None:
            self.m["skipped"] += 1
            return False
        seen = self.seen()
        prior = seen.get(rel.lower())
        if prior is not None and prior != rel:
            raise IngestError(f"two files differ only by letter case: {prior!r} and {rel!r}")
        dest = self.stage / rel
        stage_real = str(self.stage.resolve())
        if os.path.commonpath([stage_real, str(dest.resolve())]) != stage_real:
            raise IngestError(f"path traversal in upload: {name!r}")
        replaced = dest.stat().st_size if dest.is_file() else 0          # a retried batch overwrites, it does not double count
        if prior is None and self.m["files"] + 1 > self.cfg.max_files:
            raise UploadTooLarge(f"upload exceeds the file-count limit ({self.cfg.max_files})")
        written = 0
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in chunks:
                    written += len(chunk)
                    if self.m["bytes"] - replaced + written > self.cfg.max_bytes:
                        raise UploadTooLarge(f"upload exceeds the size limit ({self.cfg.max_bytes // (1024 * 1024)} MB)")
                    f.write(chunk)
        except (NotADirectoryError, IsADirectoryError, FileExistsError, PermissionError) as e:
            raise IngestError(f"conflicting path in upload: {name!r}") from e
        self.m["bytes"] += written - replaced
        if prior is None:
            self.m["files"] += 1
            with open(self.paths_path, "a") as pf:
                pf.write(rel + "\n")
            seen[rel.lower()] = rel
        return True

    def add_zip(self, zip_path: Path, strip_root: str = "auto"):
        """Extract an archive into the staging tree, validating every member and counting the bytes actually written."""
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile:
            raise IngestError("the uploaded file is not a valid zip archive")
        with zf:
            infos = zf.infolist()
            if len(infos) > self.cfg.max_files:
                raise UploadTooLarge(f"archive has more than {self.cfg.max_files} entries")
            if sum(i.file_size for i in infos) > self.cfg.max_bytes:
                raise UploadTooLarge(f"archive expands beyond the size limit ({self.cfg.max_bytes // (1024 * 1024)} MB)")
            members = []
            for i in infos:
                if i.flag_bits & 0x1:
                    raise IngestError("encrypted archives are not supported")
                kind = stat.S_IFMT(i.external_attr >> 16)
                if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
                    raise IngestError(f"archive contains a symlink or special file: {i.filename!r}")
                if i.is_dir():
                    clean_member(i.filename)                                 # validates the name
                    continue
                members.append((i, clean_member(i.filename)))
            strip = self._root_to_strip([r for _, r in members if r], strip_root)
            for info, rel in members:
                if rel is None:
                    self.m["skipped"] += 1
                    continue
                if strip and rel.startswith(strip + "/"):
                    rel = rel[len(strip) + 1:]
                with zf.open(info) as src:
                    self.add(rel, iter(lambda: src.read(CHUNK), b""))

    def _root_to_strip(self, rels: list, mode: str) -> Optional[str]:
        tops = {r.split("/", 1)[0] for r in rels}
        if len(tops) != 1 or any("/" not in r for r in rels):
            return None
        top = next(iter(tops))
        if mode == "true" or (mode == "auto" and top.lower() == self.m["name"].lower()):
            return top
        return None


# ---- committing a staged tree to git -----------------------------------------------------------------------------
def _git(cwd, env_extra: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **env_extra}
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.fsmonitor=false", *args],
                       cwd=cwd, env=env, capture_output=True)
    if check and r.returncode != 0:
        raise IngestError("git failed while storing the upload: " + r.stderr.decode("utf-8", "replace").strip()[:300])
    return r


def commit_tree(bare: Path, stage: Path, label: str) -> tuple[str, bool]:
    """Store the staged tree as a commit on `main` of a TIA-owned bare repository. Returns (commit, created);
    created is False when the content is identical to the previous upload (nothing is added)."""
    bare, stage = bare.resolve(), stage.resolve()          # git runs from the staging directory: paths must not be relative to the server's cwd
    if not any(p.is_file() for p in stage.rglob("*")):
        raise IngestError("the upload contains no files to analyse (after skipping ignored folders such as node_modules and .git)")
    if not bare.exists():
        bare.mkdir(parents=True)
        _git(bare, {}, "init", "--bare", "-q", "-b", "main", str(bare))
    index = bare / f"upload-{uuid.uuid4().hex}.idx"
    env = {"GIT_DIR": str(bare), "GIT_WORK_TREE": str(stage), "GIT_INDEX_FILE": str(index),
           "GIT_AUTHOR_NAME": "TIA Upload", "GIT_AUTHOR_EMAIL": "upload@tia.invalid",
           "GIT_COMMITTER_NAME": "TIA Upload", "GIT_COMMITTER_EMAIL": "upload@tia.invalid"}
    try:
        _git(stage, env, "add", "-f", "-A", ".")
        tree = _git(stage, env, "write-tree").stdout.decode().strip()
        head = _git(stage, env, "rev-parse", "--verify", "-q", "refs/heads/main^{commit}", check=False)
        parent = head.stdout.decode().strip() if head.returncode == 0 else None
        if parent and _git(stage, env, "rev-parse", f"{parent}^{{tree}}").stdout.decode().strip() == tree:
            return parent, False
        args = ["commit-tree", tree, "-m", label] + (["-p", parent] if parent else [])
        sha = _git(stage, env, *args).stdout.decode().strip()
        update = ["update-ref", "refs/heads/main", sha] + ([parent] if parent else [""])
        if _git(stage, env, *update, check=False).returncode != 0:
            raise IngestError("another upload to this repository was committed at the same time; retry")
        return sha, True
    finally:
        try:
            index.unlink()
        except OSError:
            pass
