"""Read-only git access. Never executes repository code."""
from __future__ import annotations

import subprocess


class GitError(Exception):
    pass


def _git(repo_path: str, *args: str, text: bool = True):
    try:
        r = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, check=False)
    except FileNotFoundError as e:  # git not installed
        raise GitError(str(e))
    if r.returncode != 0:
        raise GitError(r.stderr.decode("utf-8", "replace").strip() or f"git {args} failed")
    return r.stdout.decode("utf-8", "replace") if text else r.stdout


def resolve_rev(repo_path: str, rev: str) -> str:
    return _git(repo_path, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def list_files(repo_path: str, rev: str) -> list[str]:
    out = _git(repo_path, "ls-tree", "-r", "--name-only", rev)
    return [l for l in out.splitlines() if l]


def list_tree(repo_path: str, rev: str) -> dict[str, str]:
    """Regular files at a revision: path -> git blob id (content identity; submodules and symlinks are skipped)."""
    out = _git(repo_path, "ls-tree", "-r", "-z", rev, text=False)
    tree = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, typ, sha = meta.decode().split()
        if typ == "blob" and mode in ("100644", "100755"):
            tree[path.decode("utf-8", "surrogateescape")] = sha
    return tree


def read_blobs(repo_path: str, shas: list) -> dict[str, bytes]:
    """Contents of the given blobs in one git process (git cat-file --batch)."""
    if not shas:
        return {}
    request = "".join(f"{s}\n" for s in shas).encode()
    try:
        r = subprocess.run(["git", "-C", repo_path, "cat-file", "--batch"], input=request,
                           capture_output=True, check=False)
    except FileNotFoundError as e:
        raise GitError(str(e))
    if r.returncode != 0:
        raise GitError(r.stderr.decode("utf-8", "replace").strip() or "git cat-file failed")
    out, buf, pos = {}, r.stdout, 0
    while pos < len(buf):
        nl = buf.index(b"\n", pos)
        head = buf[pos:nl].decode().split()
        if len(head) != 3:
            raise GitError(f"blob not found: {head[0] if head else '?'}")
        size = int(head[2])
        out[head[0]] = buf[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return out


def read_file(repo_path: str, rev: str, path: str) -> str:
    return _git(repo_path, "show", f"{rev}:{path}")


def read_tree(repo_path: str, rev: str) -> dict[str, str]:
    """All text files at a revision (binary / undecodable files are skipped)."""
    files = {}
    for p in list_files(repo_path, rev):
        raw = _git(repo_path, "show", f"{rev}:{p}", text=False)
        try:
            files[p] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return files


def changed_files(repo_path: str, base: str, head: str) -> list[tuple[str, str]]:
    out = _git(repo_path, "diff", "--name-status", "--no-renames", base, head)
    res = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        res.append((status[0], path))
    return res
