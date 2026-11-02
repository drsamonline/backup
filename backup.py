#!/usr/bin/env python3
"""backup.py — snapshot a repository BEFORE it changes, into a dumps repo.

Run this before a merge. It takes the repository's tarball at the given ref (the
state that is about to be replaced), stores it compressed under

    <dumps-repo>/<owner>__<repo>/<YYYY-MM-DDTHHMMSSZ>/<repo>.tar.gz

and rotates: it keeps the newest N (default 100) per repository and deletes the
oldest beyond that. If the dumps repo passes its size cap, the next dumps repo is
created (`dumps-2`, `dumps-3`, ...) and the dump continues there, so a full repo
never stops a backup.

Zero dependencies — stdlib only, so it runs anywhere a runner has Python.

    GITHUB_TOKEN=... python3 backup.py --repo drsamonline/ml-kit --ref main

The token needs `contents: write` on the dumps repos and `contents: read` on the
source repo. For a dumps repo that is not the same owner, use a fine-grained PAT
with both.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import gzip
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
UA = "drsamonline-backup"


# --------------------------------------------------------------------------- #
# The GitHub API, in about twenty lines
# --------------------------------------------------------------------------- #
def call(method, path, token, data=None, raw=False, timeout=180):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if raw:
                return payload
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return {"_error": e.code, "_detail": detail}


# --------------------------------------------------------------------------- #
# 7z -mx=9 instead of gzip. LZMA2 usually beats gzip by 15-25% on a source tarball,
# which matters when the store is measured in gigabytes.
#
# THE RULE: a dump is only stored as 7z if it is proven readable first. GitHub hands us a
# .tar.gz; we recompress it, immediately decompress it again, and compare the bytes with
# the original. If anything about that fails - 7z missing, the round trip differs, the
# result is not smaller - the gzip tarball is stored instead. A backup is never written in
# a format it cannot be read back from.
# --------------------------------------------------------------------------- #
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
LAST_7Z_ERR = ""


def _7z(args, data=None, timeout=900):
    return subprocess.run(["7z"] + args, input=data, capture_output=True, timeout=timeout)


def compress_7z(tar_bytes):
    """Compress a RAW TAR with 7z -mx=9. Returns the bytes, or None.

    Raw tar, not the .tar.gz GitHub gives us. Recompressing an already-gzipped stream
    gained 0-2% - the first honest measurement - because gzip had already done the work.
    LZMA on the uncompressed tar is where the saving actually is.

    Through a temporary file, not -si/-so: p7zip does not reliably support streaming
    archive CREATION to stdout, and the first attempt failed for all 94 repositories
    because of it. Extraction to stdout is supported, so decompression streams fine.
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="7z-") as d:
        src = os.path.join(d, "in.tar")
        out = os.path.join(d, "out.7z")
        with open(src, "wb") as fh:
            fh.write(tar_bytes)
        global LAST_7Z_ERR
        try:
            r = _7z(["a", "-t7z", "-mx=9", "-mmt=on", "-y", out, src])
        except Exception as exc:
            LAST_7Z_ERR = "%s: %s" % (type(exc).__name__, exc)
            return None
        if r.returncode != 0 or not os.path.exists(out):
            LAST_7Z_ERR = ((r.stderr or r.stdout or b"").decode("utf-8", "replace")
                           .strip().splitlines() or ["7z exited %d" % r.returncode])[-1][:200]
            return None
        with open(out, "rb") as fh:
            data = fh.read()
    return data if data.startswith(SEVEN_ZIP_MAGIC) else None


def decompress_7z(blob):
    """Back to the original bytes. Returns None if 7z is missing or the archive is bad."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="7z-") as d:
        src = os.path.join(d, "in.7z")
        with open(src, "wb") as fh:
            fh.write(blob)
        try:
            r = _7z(["x", "-so", "-y", src])
        except Exception:
            return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def tarball(owner, repo, ref, token):
    """The whole repository at `ref`, already tar+gzipped by GitHub."""
    return call("GET", f"/repos/{owner}/{repo}/tarball/{ref}", token, raw=True)


# --------------------------------------------------------------------------- #
# The two decisions, kept pure so they can be tested without a network
# --------------------------------------------------------------------------- #
def dumps_to_delete(names, keep):
    """Given one repository's dump folder names (any order), which to delete.

    Keeps the newest `keep` by the timestamp in the name; deletes the rest,
    oldest first. `keep <= 0` deletes nothing (rotation off).
    """
    if keep <= 0:
        return []
    ordered = sorted(names)                      # the timestamp sorts lexicographically
    return ordered[:-keep] if len(ordered) > keep else []


def stamp(now=None):
    """The folder name: date and time, UTC, filesystem- and URL-safe."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H%M%SZ")


def folder_for(owner, repo):
    """One folder per repository, so dumps of different repos never collide."""
    return f"{owner}__{repo}"


MANIFEST = "manifest.json"


def manifest_from(entries, generated_at=""):
    """The dumps index, DERIVED from the dumps repo's own tree.

    `entries` are the tree's nodes (dicts with `path`, `size`, `type`). A dump is a
    path of exactly three parts - <owner>__<repo>/<stamp>/<repo>.tar.gz - so anything
    else in the repo (README, LICENSE, .github, a file loose in a repository folder)
    is not a dump and is not indexed.

    This exists because the manifest was hand-written once and never again: it went on
    claiming one repository was backed up while the store held 78. A derived index
    cannot drift, and if it is wrong the tree it was derived from is wrong too.
    """
    dumps, total = {}, 0
    # A dump is EITHER one tarball OR numbered parts with a descriptor. Grouping first
    # matters: a chunked dump has no .tar.gz at all, so a check that looked only for that
    # name would silently drop the two repositories that most needed backing up.
    groups = {}
    for e in entries:
        if e.get("type") not in (None, "blob"):
            continue
        parts = (e.get("path") or "").split("/")
        if len(parts) != 3:
            continue
        folder, when, name = parts
        if "T" not in when or not when.endswith("Z"):
            continue
        g = groups.setdefault((folder, when), {"tar": None, "desc": None, "part_bytes": 0})
        size = int(e.get("size") or 0)
        if name.endswith(".tar.gz") or name.endswith(".tar.7z"):
            g["tar"] = (e["path"], size)
        elif name.endswith(".tar.gz.parts.json") or name.endswith(".tar.7z.parts.json"):
            g["desc"] = e["path"]
        elif ".tar.gz.part" in name or ".tar.7z.part" in name:
            g["part_bytes"] += size

    for (folder, when), g in sorted(groups.items()):
        if g["tar"]:
            path, size = g["tar"]
        elif g["desc"]:
            path, size = g["desc"], g["part_bytes"]
        else:
            continue
        dumps.setdefault(folder, []).append(
            {"at": when, "file": path, "bytes": size,
             "chunked": bool(g["desc"] and not g["tar"]),
             "source": folder.replace("__", "/", 1)})
        total += size
    for snaps in dumps.values():
        snaps.sort(key=lambda d: d["at"])
    return {
        "written_by": "drsamonline/backup",
        "generated_at": generated_at,
        "note": ("Derived from the dumps repository's own tree, so it cannot go stale. One folder "
                 "per repository, one folder per snapshot named with the date and time, and the "
                 "repository inside it as a .tar.gz. The commit each dump holds is inside the "
                 "tarball: read it with --restore-test."),
        "repositories": len(dumps),
        "snapshots": sum(len(v) for v in dumps.values()),
        "bytes": total,
        "dumps": {k: dumps[k] for k in sorted(dumps)},
    }


def write_manifest(dump_owner, dumps, token, log=print):
    """Regenerate <dumps>/manifest.json from the tree, and prove it landed.

    Returns 0/1. A failure here FAILS the run on purpose: a derived index that is
    allowed to go stale quietly is exactly the defect this replaces.
    """
    info = call("GET", f"/repos/{dump_owner}/{dumps}", token)
    if isinstance(info, dict) and info.get("_error"):
        log(f"  manifest: cannot read {dump_owner}/{dumps} ({info.get('_error')})")
        return 1
    branch = info.get("default_branch") or "main"
    r = call("GET", f"/repos/{dump_owner}/{dumps}/git/trees/{branch}?recursive=1", token)
    if isinstance(r, dict) and r.get("_error"):
        log(f"  manifest: cannot read the tree of {dump_owner}/{dumps} ({r.get('_error')})")
        return 1
    if r.get("truncated"):
        log(f"  manifest: GitHub truncated the tree of {dump_owner}/{dumps}; "
            f"an incomplete index is not written")
        return 1

    doc = manifest_from(r.get("tree") or [], stamp())
    body = (json.dumps(doc, indent=1) + "\n").encode()
    payload = {"message": f"manifest: {doc['repositories']} repositories, "
                          f"{doc['snapshots']} snapshots ({doc['generated_at']})",
               "content": base64.b64encode(body).decode()}
    cur = call("GET", f"/repos/{dump_owner}/{dumps}/contents/{MANIFEST}", token)
    if isinstance(cur, dict) and not cur.get("_error") and cur.get("sha"):
        payload["sha"] = cur["sha"]
    put = call("PUT", f"/repos/{dump_owner}/{dumps}/contents/{MANIFEST}", token, payload)
    if isinstance(put, dict) and put.get("_error"):
        log(f"  manifest: could not write ({put.get('_error')})")
        return 1
    got = ((put.get("content") or {}).get("sha") or "")
    want = git_blob_sha(body)
    if got and got != want:
        log(f"  manifest: INTEGRITY FAILURE - stored {got[:10]} != written {want[:10]}")
        return 1
    log(f"  manifest: {doc['repositories']} repositories, {doc['snapshots']} snapshots, "
        f"{doc['bytes']/1e6:.1f} MB")
    return 0


# --------------------------------------------------------------------------- #
# The dumps repo: pick one under the cap, creating the next if it is full
# --------------------------------------------------------------------------- #
def stored_bytes(owner, name, token):
    """How much dump payload a dumps repo actually holds, from its own tree.

    WHY NOT `info["size"]`: the repository API's `size` lags badly. The dumps repo was
    reported as 653 MB while its manifest said 1778 MB, so the rollover cap was being
    checked against a number days out of date - which is why it never rolled over and
    the store kept growing past its own limit. The tree is the source of truth.
    """
    tree = call("GET", f"/repos/{owner}/{name}/git/trees/main?recursive=1", token)
    if not isinstance(tree, dict) or tree.get("_error"):
        return None
    total = 0
    for e in tree.get("tree") or []:
        if e.get("type") != "blob":
            continue
        bits = (e.get("path") or "").split("/")
        if len(bits) == 3 and "T" in bits[1] and bits[1].endswith("Z"):
            total += int(e.get("size") or 0)   # tarballs, .partNNN pieces and descriptors
    return total


def pick_dumps_repo(owner, base, token, cap_bytes, private=True, log=print):
    """The first of base, base-2, base-3, ... that exists and is under the cap.

    A dumps repo that does not exist is created. This is the whole "the repo
    creates a new repo when the size limit comes" rule.
    """
    n = 1
    while True:
        name = base if n == 1 else f"{base}-{n}"
        info = call("GET", f"/repos/{owner}/{name}", token)
        if isinstance(info, dict) and info.get("_error") == 404:
            log(f"  creating {owner}/{name} ...")
            made = call("POST", "/user/repos", token, {
                "name": name, "private": private, "auto_init": True,
                "description": f"Pre-merge backups (part {n}). Written by drsamonline/backup.",
            })
            if isinstance(made, dict) and made.get("_error"):
                raise SystemExit(f"could not create {owner}/{name}: {made}")
            return name
        if isinstance(info, dict) and info.get("_error"):
            raise SystemExit(f"could not read {owner}/{name}: {info}")
        # Prefer what the tree says over what the repository API reports: `size` lags.
        size = stored_bytes(owner, name, token)
        if size is None:
            size = (info.get("size") or 0) * 1024      # the API reports KB
            log(f"  {owner}/{name}: tree unreadable, falling back to the API size")
        if size < cap_bytes:
            log(f"  using {owner}/{name} ({size/1e6:.1f} MB of {cap_bytes/1e6:.0f} MB)")
            return name
        log(f"  {owner}/{name} is full ({size/1e6:.1f} MB) - rolling over")
        n += 1


def git_blob_sha(data: bytes) -> str:
    """The sha git itself would give these bytes: sha1("blob <len>\\0" + data).

    GitHub's contents API returns the stored object's git blob sha in the PUT
    response, so comparing it with this proves the file that landed is byte-for-byte
    what we fetched - without a second request and without downloading it again.
    """
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# --restore-test: unpack ONE snapshot and prove it rebuilds the repository
# --------------------------------------------------------------------------- #
def parse_tarball_root(top, owner=""):
    """'drsamonline-today-card-57d64d9...' -> ('drsamonline', 'today-card', '57d64d9...').

    GitHub's tarball API wraps the whole repository in ONE directory named
    <owner>-<repo>-<sha>. That name is the only record inside a dump of WHICH commit it
    holds, so a restore test begins by reading it.

    IT USED TO SPLIT ON THE LAST TWO HYPHENS, which reads `drsamonline-today-card-abc` as
    owner `drsamonline-today` and repo `card`. Every repository with a hyphen in its name -
    which is most of them - therefore failed the restore test with a message blaming the
    dump. The check was broken, not the backups.

    The sha never contains a hyphen, so the last one separates it. What remains is
    <owner>-<repo>, and the owner is known: match it exactly rather than guessing where
    the boundary is.
    """
    rest, _, sha = top.rpartition("-")
    if not rest or not sha:
        raise SystemExit(f"cannot read <owner>-<repo>-<sha> from the tarball top directory {top!r}")
    if owner and rest.startswith(owner + "-"):
        return owner, rest[len(owner) + 1:], sha
    first, _, repo = rest.partition("-")
    return first, repo, sha


def restored_tree(blob):
    """(top directory, {path: git blob sha}) for every file inside a .tar.gz.

    Every member is read to the end, which is what verifies the gzip CRC: a
    truncated or corrupted dump raises here rather than passing quietly.
    """
    mode = "r:gz"
    if blob[:6] == SEVEN_ZIP_MAGIC:
        raw = decompress_7z(blob)
        if raw is None:
            raise SystemExit("this dump is 7z and 7z is not available to read it")
        blob, mode = raw, "r:"      # a 7z dump holds the uncompressed tar
    out, tops = {}, set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode=mode) as tf:
        for m in tf:
            name = m.name
            while name.startswith("./"):
                name = name[2:]
            if not name or name == "pax_global_header":
                continue
            top, _, rel = name.partition("/")
            tops.add(top)
            if m.isfile() and rel:
                fh = tf.extractfile(m)
                if fh is not None:
                    out[rel] = git_blob_sha(fh.read())
    if len(tops) != 1:
        raise SystemExit(f"a GitHub tarball has one top-level directory; this has {sorted(tops)}")
    return tops.pop(), out


def compare_trees(restored, tree):
    """(missing, extra, altered) between a restored dump and the commit it claims."""
    missing = sorted(set(tree) - set(restored))
    extra = sorted(set(restored) - set(tree))
    altered = sorted(p for p in set(restored) & set(tree) if restored[p] != tree[p])
    return missing, extra, altered


def commit_tree(owner, repo, sha, token):
    """({path: blob sha}, truncated) for every file at one commit."""
    r = call("GET", f"/repos/{owner}/{repo}/git/trees/{sha}?recursive=1", token)
    if isinstance(r, dict) and r.get("_error"):
        raise SystemExit(f"could not read the tree of {owner}/{repo}@{sha[:12]}: {r}")
    blobs = {i["path"]: i["sha"] for i in r.get("tree", []) if i.get("type") == "blob"}
    return blobs, bool(r.get("truncated"))


# 40 MB, not 90. The contents API's ceiling applies to the REQUEST BODY, and the body is
# base64 - a third larger than the bytes. A 94.4 MB chunk was refused as "too large"
# because it travelled as a ~126 MB request. 40 MB raw is ~53 MB on the wire, with room.
# 80 MB raw. The git path is the one that can carry a large file - the contents API
# refused even a 41.9 MB part as "too large" - and a push accepts up to 100 MB per file.
CHUNK = 80 * 1024 * 1024


def write_chunked(dump_owner, dumps, path, blob, token, log=print):
    """Store a dump too large for one file as numbered parts, pushed in ONE commit.

    WHY GIT AND NOT THE CONTENTS API: HCS and ziptastic exceed GitHub's per-file limit,
    and the contents API refused even a 41.9 MB part with "file is too large". A git push
    carries up to 100 MB per file, so the parts travel that way - all of them in a single
    commit, so a dump is either fully there or not there at all.

    The descriptor records the part count, the total size and the sha256 of the whole, so
    a restore can prove it reassembled the right bytes rather than merely some bytes.
    """
    size = len(blob)
    pieces = [blob[i:i + CHUNK] for i in range(0, size, CHUNK)] or [b""]
    extra, shas = {}, []
    for n, piece in enumerate(pieces, 1):
        rel = "%s.part%03d" % (path, n)
        extra[rel] = piece
        shas.append(git_blob_sha(piece))
    desc = {"parts": len(pieces), "size": size,
            "sha256": hashlib.sha256(blob).hexdigest(), "part_shas": shas}
    extra[path + ".parts.json"] = json.dumps(desc, indent=1).encode()

    log("  %s: %.1f MB in %d part(s) via git push" % (path, size / 1e6, len(pieces)))
    # blob=None: the parts ARE the payload. Passing the whole blob here as well made it
    # try to push the full 133.8 MB file alongside its own parts, which is over the
    # per-file limit and failed the entire dump.
    ok = write_via_git(dump_owner, dumps, path, None, "backup: %s (%d parts)" % (path, len(pieces)),
                       token, log=log, extra=extra)
    if ok:
        log("  %s: %d part(s) pushed, sha256 %s" % (path, len(pieces), desc["sha256"][:12]))
    return ok


def dump_blob(dump_owner, dumps, path, token):
    """The raw bytes of a dump, through the blobs API so a dump of any size works.

    A chunked dump has no single file: it is numbered parts plus a descriptor. It is
    reassembled here and checked against the descriptor's size and sha256, so a caller
    that asks for a path gets the same bytes back whether it was stored in one piece or
    twenty.
    """
    dpath = path + ".parts.json"
    dmeta = call("GET", f"/repos/{dump_owner}/{dumps}/contents/{dpath}", token)
    if isinstance(dmeta, dict) and not dmeta.get("_error") and dmeta.get("sha"):
        draw = call("GET", f"/repos/{dump_owner}/{dumps}/git/blobs/{dmeta['sha']}", token)
        if isinstance(draw, dict) and draw.get("_error"):
            raise SystemExit(f"could not read {dpath}: {draw}")
        desc = json.loads(base64.b64decode(draw.get("content") or ""))
        chunks = []
        for n in range(1, int(desc.get("parts") or 0) + 1):
            p = "%s.part%03d" % (path, n)
            m = call("GET", f"/repos/{dump_owner}/{dumps}/contents/{p}", token)
            if not isinstance(m, dict) or m.get("_error"):
                raise SystemExit(f"chunked dump {path}: part {n} is missing ({m})")
            b = call("GET", f"/repos/{dump_owner}/{dumps}/git/blobs/{m.get('sha')}", token)
            if not isinstance(b, dict) or b.get("_error"):
                raise SystemExit(f"chunked dump {path}: part {n} unreadable ({b})")
            data = base64.b64decode(b.get("content") or "")
            if b.get("size") is not None and len(data) != b["size"]:
                raise SystemExit(f"chunked dump {path}: part {n} decoded {len(data)} "
                                 f"bytes, the API reports {b['size']}")
            chunks.append(data)
        blob = b"".join(chunks)
        if len(blob) != desc.get("size"):
            raise SystemExit(f"chunked dump {path}: reassembled {len(blob)} bytes, "
                             f"descriptor says {desc.get('size')}")
        if hashlib.sha256(blob).hexdigest() != desc.get("sha256"):
            raise SystemExit(f"chunked dump {path}: sha256 does not match the descriptor")
        return blob, dmeta["sha"]

    meta = call("GET", f"/repos/{dump_owner}/{dumps}/contents/{path}", token)
    if isinstance(meta, dict) and meta.get("_error"):
        raise SystemExit(f"could not read {dumps}/{path}: {meta}")
    sha = (meta or {}).get("sha") if isinstance(meta, dict) else None
    if not sha:
        raise SystemExit(f"could not read {dumps}/{path}: the response carries no blob sha")
    raw = call("GET", f"/repos/{dump_owner}/{dumps}/git/blobs/{sha}", token)
    if isinstance(raw, dict) and raw.get("_error"):
        raise SystemExit(f"could not read blob {sha}: {raw}")
    if not isinstance(raw, dict) or raw.get("encoding") != "base64":
        raise SystemExit(f"unexpected encoding for {dumps}/{path}: {(raw or {}).get('encoding')!r}")
    blob = base64.b64decode(raw.get("content") or "")
    if raw.get("size") is not None and len(blob) != raw["size"]:
        raise SystemExit(f"{dumps}/{path}: decoded {len(blob)} bytes, the API reports {raw['size']}")
    return blob, sha


def restore_test(src_owner, src_repo, dump_owner, dumps, at, token):
    """Unpack ONE snapshot and compare it, file by file, with the commit it holds.

    This is the answer to "a backup workflow that ran is not proof of
    recoverability". It exits 0 only when the restored tree is byte-for-byte the
    repository at the commit the dump names: nothing missing, nothing extra,
    nothing altered.
    """
    folder = folder_for(src_owner, src_repo)
    stamps = list_dump_folders(dump_owner, dumps, folder, token)
    if not stamps:
        print(f"  FAIL  {dump_owner}/{dumps} holds no dumps for {src_owner}/{src_repo}")
        return 1
    at = at or stamps[-1]
    if at not in stamps:
        print(f"  FAIL  no dump at {at} for {src_owner}/{src_repo}; "
              f"have {stamps[0]} .. {stamps[-1]} ({len(stamps)} of them)")
        return 1
    # Find the file that is actually there. This used to assume `.tar.gz`, which stopped
    # being true the moment new dumps were written as `.tar.7z`. The restore test then
    # 404'd on every store and failed the whole backup run - blaming the backups for a
    # filename.
    path = ""
    for candidate in (f"{folder}/{at}/{src_repo}.tar.7z",
                      f"{folder}/{at}/{src_repo}.tar.gz"):
        probe, _ = dump_blob(dump_owner, dumps, candidate, token)
        if not isinstance(probe, dict):
            path = candidate
            break
    if not path:
        print(f"  FAIL  no dump file in {dump_owner}/{dumps}/{folder}/{at}")
        return 1
    print(f"restore test: {dump_owner}/{dumps}/{path}")

    blob, stored = dump_blob(dump_owner, dumps, path, token)
    print(f"  read {len(blob)/1e6:.2f} MB; the API's stored blob sha is {stored[:10]}")

    try:
        top, restored = restored_tree(blob)
    except (tarfile.TarError, OSError, EOFError) as e:
        print(f"  FAIL  the dump will not unpack: {type(e).__name__}: {e}")
        return 1
    got_owner, got_repo, commit = parse_tarball_root(top, src_owner)
    if (got_owner, got_repo) != (src_owner, src_repo):
        print(f"  FAIL  this dump holds {got_owner}/{got_repo}, not {src_owner}/{src_repo}")
        return 1
    fmt = "7z (LZMA2)" if path.endswith(".tar.7z") else "gzip (CRC verified)"
    print(f"  unpacks clean, {fmt}: {len(restored)} files, taken at {commit[:12]}")

    tree, truncated = commit_tree(src_owner, src_repo, commit, token)
    if truncated:
        print(f"  FAIL  GitHub truncated the tree of {commit[:12]}; the comparison is "
              f"incomplete, which is not a pass")
        return 1
    missing, extra, altered = compare_trees(restored, tree)
    if not (missing or extra or altered):
        print(f"  PASS  EXACT RESTORE - {len(restored)} files match "
              f"{src_owner}/{src_repo}@{commit[:12]} file for file")
        return 0
    print(f"  FAIL  restored {len(restored)} files, the commit has {len(tree)}")
    for label, items in (("missing", missing), ("extra", extra), ("altered", altered)):
        if items:
            print(f"        {label} ({len(items)}): {', '.join(items[:5])}")
    return 1


def list_dump_folders(owner, dumps, folder, token):
    """The timestamp folders already in <dumps>/<folder>/."""
    items = call("GET", f"/repos/{owner}/{dumps}/contents/{folder}", token)
    if isinstance(items, dict) and items.get("_error") == 404:
        return []
    if isinstance(items, dict) and items.get("_error"):
        raise SystemExit(f"could not list {folder}: {items}")
    return sorted(i["name"] for i in items if i.get("type") == "dir")


def delete_folder(owner, dumps, path, token, log=None):
    """Delete a folder (a dump): remove its files, then confirm it is gone.

    It used to return silently whenever the listing was not a list, and never checked
    that a DELETE had worked - so a failed rotation looked exactly like a successful one
    and the store kept growing. A cleanup that cannot fail loudly is not a cleanup.
    """
    items = call("GET", f"/repos/{owner}/{dumps}/contents/{path}", token)
    if isinstance(items, dict) and items.get("_error") == 404:
        return True                      # already gone - the desired end state
    if not isinstance(items, list):
        if log:
            log(f"  rotate: could not list {path}: {items}")
        return False
    failed = 0
    for it in items:
        res = call("DELETE", f"/repos/{owner}/{dumps}/contents/{it['path']}", token,
                   {"message": f"rotate: drop old backup {path}", "sha": it["sha"]})
        if isinstance(res, dict) and res.get("_error"):
            failed += 1
            if log:
                log(f"  rotate: could not delete {it['path']} ({res.get('_error')})")
    if failed:
        return False
    after = call("GET", f"/repos/{owner}/{dumps}/contents/{path}", token)
    gone = isinstance(after, dict) and after.get("_error") == 404
    if not gone and log:
        log(f"  rotate: {path} still present after deleting {len(items)} file(s)")
    return gone


# --------------------------------------------------------------------------- #
# --check: prove the token can do BOTH halves, before trusting it
# --------------------------------------------------------------------------- #
# --all: dump every repository, from ONE workflow in ONE repository
# --------------------------------------------------------------------------- #
SKIP = {"dumps", "backup", "drsamonline", "drsamonline.github.io"}


def _is_dump_store(name):
    """dumps, dumps-2, dumps-3 ... are where backups live, never what gets backed up."""
    n = (name or "").lower()
    return n == "dumps" or n.startswith("dumps-")

def all_repos(owner, token):
    """Every repo the token can see, minus the stores and the meta repos.

    The dump stores are excluded by pattern rather than by name: dumps-2 was created
    during a rollover and the very next run tried to back it up, which is nonsense - it
    is the place backups are written, not a source.

    Falls back to the public list when the token cannot see private repos.
    """
    out, page = [], 1
    while True:
        batch = call("GET", f"/user/repos?per_page=100&page={page}&affiliation=owner", token)
        if isinstance(batch, dict) and batch.get("_error"):
            batch = call("GET", f"/users/{owner}/repos?per_page=100&page={page}&sort=pushed", token)
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
    names = sorted(r["name"] for r in out
                   if r.get("owner", {}).get("login", owner) == owner
                   and r["name"] not in SKIP and not _is_dump_store(r["name"])
                   and not r.get("fork") and not r.get("archived"))
    return names



# --------------------------------------------------------------------------- #
# Change detection. A backup of a repository that has not changed is a copy of
# something already stored: measured on this corpus, 53% of every run was that.
# So before downloading anything, ask what commit the default branch is at and
# compare it with the one recorded last time. Same commit and no new tag means
# skip. The check costs two cheap API calls; the download it avoids costs a
# tarball, a recompress and a push.
#
# IF THE CHECK FAILS, THE REPOSITORY IS BACKED UP. A failed lookup is not
# evidence of no change, and treating it as such is how a backup quietly stops
# covering something.
# --------------------------------------------------------------------------- #
STATE_FILE = "state.json"


def repo_state(owner, repo, token):
    """(default branch, head sha, newest tag) for one repository, or None on failure."""
    meta = call("GET", f"/repos/{owner}/{repo}", token)
    if isinstance(meta, dict) and meta.get("_error"):
        return None
    branch = meta.get("default_branch") or "main"
    head = call("GET", f"/repos/{owner}/{repo}/commits/{branch}", token)
    if isinstance(head, dict) and head.get("_error"):
        return None
    sha = head.get("sha") or ""
    if not sha:
        return None
    tags = call("GET", f"/repos/{owner}/{repo}/tags?per_page=1", token)
    tag = tags[0].get("name", "") if isinstance(tags, list) and tags else ""
    return branch, sha, tag


def read_state(dump_owner, dumps, token):
    """The state file in one store, or {}."""
    blob, _ = dump_blob(dump_owner, dumps, STATE_FILE, token)
    if isinstance(blob, dict):
        return {}
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:
        return {}


def dump_store_names(owner, base, token):
    """Every store matching the base name: dumps, dumps-2, dumps-3 ..."""
    out, page = [], 1
    while True:
        batch = call("GET", f"/user/repos?per_page=100&page={page}&affiliation=owner", token)
        if not isinstance(batch, list) or not batch:
            break
        for r in batch:
            n = r.get("name", "")
            if n == base or (n.startswith(base + "-") and n[len(base) + 1:].isdigit()):
                out.append(n)
        if len(batch) < 100:
            break
        page += 1
    return sorted(out, key=lambda n: (len(n), n))


def read_state_all(owner, base, token):
    """Merge the state files of every store. The newest entry per repository wins.

    The state has to be read across stores, not just the one in use: snapshots roll
    over into dumps-2, dumps-3 ..., so a repository last backed up before a rollover
    has its record in the older store.
    """
    merged = {}
    for name in dump_store_names(owner, base, token):
        for key, entry in (read_state(owner, name, token).get("repos") or {}).items():
            if key not in merged or (entry.get("at") or "") > (merged[key].get("at") or ""):
                merged[key] = entry
    return merged


def _norm_ts(text):
    """2026-11-01T19:23:10Z and 2026-11-01T192310Z both -> 20261101T192310."""
    t = re.sub(r"[-:]", "", str(text or ""))
    return t.rstrip("Z")[:15]


def seed_state(owner, base, token, log=print):
    """Record a starting state WITHOUT downloading anything.

    The first change-triggered run would otherwise back up all 96 repositories again, which
    is exactly the duplication the change is meant to stop. A repository whose last push is
    no newer than its newest stored snapshot cannot have changed since that snapshot, so its
    current head is recorded and it will be skipped. Anything newer is left unrecorded and
    will be backed up - the seeding only ever errs toward backing something up.
    """
    stores = dump_store_names(owner, base, token)
    newest = {}
    for st in stores:
        blob, _ = dump_blob(owner, st, "manifest.json", token)
        if isinstance(blob, dict):
            continue
        try:
            data = json.loads(blob.decode("utf-8"))
        except Exception:
            continue
        for folder, lst in (data.get("dumps") or {}).items():
            for snap in lst:
                if _norm_ts(snap.get("at")) > newest.get(folder, ""):
                    newest[folder] = _norm_ts(snap.get("at"))
    log(f"  seed: newest stored snapshot known for {len(newest)} repositories across {len(stores)} stores")

    known, seeded, pending = {}, 0, []
    for name in all_repos(owner, token):
        key = folder_for(owner, name)
        meta = call("GET", f"/repos/{owner}/{name}", token)
        want = repo_state(owner, name, token)
        if isinstance(meta, dict) and meta.get("_error") or not want:
            pending.append(name)
            continue
        pushed = _norm_ts(meta.get("pushed_at"))
        stored = newest.get(key, "")
        if stored and pushed and pushed <= stored:
            known[key] = {"sha": want[1], "tag": want[2], "at": stored, "seeded": True}
            seeded += 1
        else:
            pending.append(name)
    log(f"  seed: {seeded} recorded as up to date, {len(pending)} will be backed up on the next run")
    if pending:
        log(f"  seed: pending: {', '.join(pending[:8])}" + (" ..." if len(pending) > 8 else ""))
    dumps = pick_dumps_repo(owner, base, token, 800 * 1024 * 1024)
    if not write_state(owner, dumps, token, known, log=log):
        log("  seed: could not write state.json")
        return 1
    log(f"  seed: state.json written to {owner}/{dumps}")
    return 0


def write_state(dump_owner, dumps, token, known, log=print):
    payload = {"written_by": "drsamonline/backup",
               "note": ("The commit each repository was last backed up at. A repository whose "
                        "head matches this is skipped: backing it up again would store a copy "
                        "of what is already there. Deleting this file makes the next run a "
                        "full one."),
               "updated_at": stamp(),
               "repos": known}
    data = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
    return write_via_git(dump_owner, dumps, STATE_FILE, data,
                         "chore(state): record the commit each repository was last backed up at",
                         token, log=log)

def write_via_git(dump_owner, dumps, path, blob, message, token, log=print, extra=None):
    """Write one dump by committing it in a clone and pushing.

    WHY THIS EXISTS: the contents API refuses a large file with
        422 "Sorry, the file is too large to be processed.
             Consider creating/updating the file in a local clone and pushing it."
    Two repositories hit that ceiling (HCS 125 MB, The_Browser 165 MB) and were never
    backed up at all. This is GitHub's own suggested route.

    THREE WAYS THIS COULD SILENTLY DESTROY BACKUPS, avoided:
      * a `--no-checkout` clone has an EMPTY index, so `git add` + commit would write a
        tree holding only the new file and delete every other snapshot. `git read-tree
        HEAD` populates the index from the commit first.
      * the token must not go in the remote URL where `git remote -v` would print it.
        It travels in an http extraheader.
      * a push that exits 0 is a claim about a command. The committed blob sha is
        compared with the sha of the bytes we meant to store, and the branch is asked
        afterwards what it holds.
    """
    import shutil
    import tempfile

    work = tempfile.mkdtemp(prefix="dumps-git-")
    repo = os.path.join(work, "repo")
    url = "https://github.com/%s/%s.git" % (dump_owner, dumps)
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    header = "AUTHORIZATION: basic " + auth
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="backup-bot",
               GIT_AUTHOR_EMAIL="backup@users.noreply.github.com",
               GIT_COMMITTER_NAME="backup-bot",
               GIT_COMMITTER_EMAIL="backup@users.noreply.github.com")

    def git(*args, cwd=None):
        return subprocess.run(["git", "-c", "http.https://github.com/.extraheader=" + header]
                              + list(args),
                              cwd=cwd or repo, capture_output=True, text=True, env=env)

    try:
        log("  %s/%s: writing through git push" % (dumps, path))
        # blob:none keeps this cheap on a repository already holding 1.6 GB: we want the
        # history and trees, not every snapshot's bytes.
        r = git("clone", "--filter=blob:none", "--no-checkout", url, repo, cwd=work)
        if r.returncode != 0:
            log("  %s: clone failed: %s" % (dumps, (r.stderr or "").strip()[:200]))
            return False
        r = git("read-tree", "HEAD")
        if r.returncode != 0:
            log("  %s: read-tree failed: %s" % (dumps, (r.stderr or "").strip()[:200]))
            return False

        payload = {} if blob is None else {path: blob}
        if extra:
            payload.update(extra)
        if not payload:
            log("  %s: nothing to write" % dumps)
            return False
        for rel, data in payload.items():
            dest = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)

        for step in (("add", "--") + tuple(payload.keys()), ("commit", "-m", message)):
            r = git(*step)
            if r.returncode != 0:
                log("  %s: %s failed: %s" % (dumps, step[0], (r.stderr or "").strip()[:200]))
                return False

        # Verify EVERY file we committed, not just one: in chunked mode the payload is
        # several parts plus a descriptor, and checking only the first would let a bad
        # part through on the strength of a good neighbour.
        for rel, data in payload.items():
            want = git_blob_sha(data)
            got = (git("rev-parse", "HEAD:%s" % rel).stdout or "").strip()
            if got != want:
                log("  %s/%s: INTEGRITY FAILURE - committed %s != intended %s - not pushing"
                    % (dumps, rel, got[:10], want[:10]))
                return False

        pushed = False
        for attempt in range(1, 4):
            r = git("push", "origin", "HEAD:main")
            if r.returncode == 0:
                pushed = True
                break
            log("  %s: push attempt %d rejected: %s"
                % (dumps, attempt, (r.stderr or "").strip()[:150]))
            if git("fetch", "--filter=blob:none", "origin", "main").returncode == 0:
                git("rebase", "origin/main")
        if not pushed:
            return False

        local_head = (git("rev-parse", "HEAD").stdout or "").strip()
        remote = (git("ls-remote", "origin", "refs/heads/main").stdout or "").split()
        remote_head = remote[0] if remote else ""
        if remote_head != local_head:
            log("  %s: pushed, but main is %s and we committed %s - NOT verified"
                % (dumps, remote_head[:10], local_head[:10]))
            return False

        total = sum(len(d) for d in payload.values())
        log("  %s/%s: pushed %d file(s), %.2f MB, every blob sha confirmed on main"
            % (dumps, path, len(payload), total / 1e6))
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def dump_one(src_owner, src_repo, dump_owner, dumps, token, keep, ref="", log=print, seven_zip=True):
    """Fetch one repo's tarball and write it into the dumps repo. Returns True/False."""
    blob = tarball(src_owner, src_repo, ref, token)
    if isinstance(blob, dict):
        log(f"  {src_repo}: could not fetch ({blob.get('_error')}) - skipped")
        return False
    folder = folder_for(src_owner, src_repo)
    when = stamp()
    path = f"{folder}/{when}/{src_repo}.tar.gz"

    # 7z -mx=9 instead of gzip, but ONLY if it can be read straight back and is smaller.
    # The round trip is the whole safety argument: a dump is never stored in a format it
    # cannot be restored from, and a compression step that gains nothing is not worth the
    # dependency.
    if seven_zip:
        # Uncompressed tar in, 7z out. GitHub hands us gzip, and gzipping gzip gains
        # nothing - measured at 0-2% before this changed.
        try:
            raw_tar = gzip.decompress(blob)
        except Exception:
            raw_tar = None
        packed = compress_7z(raw_tar) if raw_tar else None
        if packed is None:
            log(f"  {src_repo}: 7z failed ({LAST_7Z_ERR or 'no detail'}) - keeping the gzip tarball")
        else:
            back = decompress_7z(packed)
            if back is None or hashlib.sha256(back).hexdigest() != hashlib.sha256(raw_tar).hexdigest():
                log(f"  {src_repo}: 7z round trip did NOT match - keeping the gzip tarball")
            elif len(packed) >= len(blob):
                log(f"  {src_repo}: 7z was no smaller than gzip ({len(packed)} vs {len(blob)} bytes) - keeping gzip")
            else:
                log(f"  {src_repo}: 7z -mx=9 {len(blob)/1e6:.2f} MB -> {len(packed)/1e6:.2f} MB "
                    f"({100 - 100.0*len(packed)/len(blob):.0f}% smaller), round trip verified")
                blob = packed
                path = f"{folder}/{when}/{src_repo}.tar.7z"
    # Over the per-file ceiling: do not bother attempting a write that will be refused.
    if len(blob) >= CHUNK:
        log("  %s: %.1f MB is over the per-file ceiling - storing it in parts"
            % (src_repo, len(blob) / 1e6))
        if not write_chunked(dump_owner, dumps, path, blob, token, log=log):
            log("  %s: NOT BACKED UP - the chunked write failed" % src_repo)
            return False
        have = list_dump_folders(dump_owner, dumps, folder, token)
        for old in dumps_to_delete(have, keep):
            if not delete_folder(dump_owner, dumps, f"{folder}/{old}", token, log=log):
                log("  %s: rotation could NOT delete %s" % (src_repo, old))
        log("  %s: %.2f MB -> %s (in parts, kept %d)"
            % (src_repo, len(blob) / 1e6, path, min(len(have), keep)))
        return True

    via_git = False
    put = call("PUT", f"/repos/{dump_owner}/{dumps}/contents/{path}", token, {
        "message": f"backup: {src_owner}/{src_repo} ({when})",
        "content": base64.b64encode(blob).decode()})
    if isinstance(put, dict) and put.get("_error"):
        code = put.get("_error")
        detail = (put.get("_detail") or "").strip().replace("\n", " ")[:200]
        # 422 "file is too large to be processed" is a ceiling, not a fault: the same
        # bytes go in by committing in a clone and pushing. Anything else is a real
        # failure and is reported as one, with the API's own words.
        if code == 422 and "too large" in detail.lower():
            if not write_via_git(dump_owner, dumps, path, blob,
                                 "backup: %s/%s (%s)" % (src_owner, src_repo, when),
                                 token, log=log):
                log("  %s: NOT BACKED UP - the API and the git fallback both refused it"
                    % src_repo)
                return False
            # The git path already compared the committed blob sha with the sha of these
            # bytes and asked the branch what it holds. Running the API-sha check after
            # it would print "integrity NOT verified" about a dump that WAS verified.
            via_git = True
        else:
            log("  %s: could not write (%s) %s" % (src_repo, code, detail))
            return False

    # VERIFY WHAT LANDED. A successful PUT only means the request was accepted; it is
    # not evidence that the stored object is complete. The response carries the stored
    # blob's sha, so comparing it with the sha of the bytes we sent proves it exactly.
    # Without this, a truncated or corrupted dump was indistinguishable from a good one
    # and the only proof of recoverability was a green workflow.
    want = git_blob_sha(blob)
    got = ((put.get("content") or {}).get("sha") or "")
    if via_git:
        got = want            # verified by the git path; see write_via_git
    if got and got != want:
        log(f"  {src_repo}: INTEGRITY FAILURE - stored {got[:10]} != fetched {want[:10]} "
            f"({len(blob)} bytes) - NOT keeping this snapshot's retention sweep")
        return False
    if not got:
        log(f"  {src_repo}: written, but the API returned no sha - integrity NOT verified")

    have = list_dump_folders(dump_owner, dumps, folder, token)
    for old in dumps_to_delete(have, keep):
        if not delete_folder(dump_owner, dumps, f"{folder}/{old}", token, log=log):
            log(f"  {src_repo}: rotation could NOT delete {old} - the store will keep growing")
    log(f"  {src_repo}: {len(blob)/1e6:.2f} MB -> {dumps}/{path}  (kept {min(len(have), keep)})")
    return True


# --------------------------------------------------------------------------- #
def check(src_owner, src_repo, dump_owner, dumps, token):
    """Read the source repo, write a probe into the dumps repo, delete it again.

    Also reports whether the token can CREATE a repo - needed only for the
    rollover when a dumps repo fills up. A missing repo is not a failure here;
    it is reported, because the token then needs `administration: write`.
    """
    good = True

    r = call("GET", f"/repos/{src_owner}/{src_repo}", token)
    if isinstance(r, dict) and r.get("_error"):
        print("  FAIL  cannot READ %s/%s  (%s)" % (src_owner, src_repo, r.get("_error"))); good = False
    else:
        print("  ok    can READ the source repo %s/%s" % (src_owner, src_repo))

    info = call("GET", f"/repos/{dump_owner}/{dumps}", token)
    if isinstance(info, dict) and info.get("_error"):
        print("  FAIL  cannot see the dumps repo %s/%s  (%s)" % (dump_owner, dumps, info.get("_error"))); good = False
        return 1

    probe = "_token_check.txt"
    put = call("PUT", f"/repos/{dump_owner}/{dumps}/contents/{probe}", token,
               {"message": "backup: token check", "content": base64.b64encode(b"ok").decode()})
    if isinstance(put, dict) and put.get("_error"):
        print("  FAIL  cannot WRITE the dumps repo  (%s)" % put.get("_error")); good = False
    else:
        print("  ok    can WRITE the dumps repo %s/%s" % (dump_owner, dumps))
        sha = (put.get("content") or {}).get("sha")
        if sha:
            call("DELETE", f"/repos/{dump_owner}/{dumps}/contents/{probe}", token,
                 {"message": "backup: token check cleanup", "sha": sha})
            print("  ok    cleaned up the probe")

    made = call("POST", "/user/repos", token, {"name": "_backup-token-check-delete-me",
                                               "private": True, "auto_init": True,
                                               "description": "temporary: backup token check"})
    if isinstance(made, dict) and made.get("_error"):
        print("  note  cannot CREATE a repo (%s) - fine UNLESS a dumps repo fills up and must roll over;"
              % made.get("_error"))
        print("        either add `administration: write` to the token, or create the next dumps repo by hand.")
    else:
        print("  ok    can CREATE a repo (rollover will work)")
        call("DELETE", "/repos/%s/%s" % (dump_owner, "_backup-token-check-delete-me"), token)
        print("  ok    cleaned up the test repo")
    return 0 if good else 1


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# --rollover-test: prove the rollover against the LIVE API, on a scratch repo
# --------------------------------------------------------------------------- #
def rollover_test(owner, scratch, token, log=print):
    """Create a scratch dumps repo, force the cap, and prove pick_dumps_repo rolls over.

    The DECISION is already proven offline (tests/test_backup.py drives pick_dumps_repo
    against a stubbed API). What has never been proven is the live call: creating a real
    repository, which needs `administration: write`. That is the whole reason this
    function exists, and the reason F-61 stayed open.

    `--max-bytes 1` is the trick: any repo that exists is already "full", so the very
    first call must roll over. No 800 MB of data is needed to exercise the path.

    It only ever touches names beginning with the scratch prefix, and it refuses to run
    if the scratch name is the real dumps repo.
    """
    if not scratch.startswith("zz-rollover-test"):
        raise SystemExit(f"refusing: the scratch name must start with zz-rollover-test (got {scratch!r})")
    if scratch == "dumps" or scratch.startswith("dumps-"):
        raise SystemExit("refusing: that is the real dumps repo")

    made = []
    try:
        # 1. the scratch repo must exist, so the first candidate is "full" at 1 byte
        info = call("GET", f"/repos/{owner}/{scratch}", token)
        if isinstance(info, dict) and info.get("_error") == 404:
            log(f"  creating {owner}/{scratch} (scratch) ...")
            r = call("POST", "/user/repos", token, {
                "name": scratch, "private": True, "auto_init": True,
                "description": "Scratch repo for backup.py --rollover-test. Safe to delete.",
            })
            if isinstance(r, dict) and r.get("_error"):
                raise SystemExit(f"could not create the scratch repo: {r}\n"
                                 "  This is the `administration: write` failure F-61 describes.")
            made.append(scratch)
        elif isinstance(info, dict) and info.get("_error"):
            raise SystemExit(f"could not read {owner}/{scratch}: {info}")

        # 2. force the cap to 1 byte and ask the REAL function which repo to use.
        #    The trick only works if the scratch repo reports at least 1 byte: an
        #    auto-initialised repo has a README commit, so GitHub reports >= 1 KB.
        #    Check the assumption rather than trusting it - a 0-byte repo would make
        #    this test silently pass by NOT rolling over.
        fresh = call("GET", f"/repos/{owner}/{scratch}", token)
        size_kb = (fresh.get("size") or 0) if isinstance(fresh, dict) else 0
        if size_kb < 1:
            raise SystemExit(
                f"{owner}/{scratch} reports {size_kb} KB, so a 1-byte cap cannot make it "
                "'full' and this test would prove nothing.\n"
                "  Re-run in a minute (GitHub updates the size field asynchronously), or "
                "delete the scratch repo and let this recreate it.")
        log(f"  scratch repo reports {size_kb} KB, so a 1-byte cap makes it 'full'")
        log("  asking pick_dumps_repo with a 1-byte cap, so any existing repo is 'full' ...")
        chosen = pick_dumps_repo(owner, scratch, token, 1, private=True, log=log)
        expected = f"{scratch}-2"
        if chosen != expected:
            raise SystemExit(f"FAILED: expected rollover to {expected}, got {chosen}")
        log(f"  PASSED: rolled over to {chosen}")

        # 3. the rolled-over repo must really exist now
        info2 = call("GET", f"/repos/{owner}/{chosen}", token)
        if isinstance(info2, dict) and info2.get("_error"):
            raise SystemExit(f"FAILED: {chosen} was returned but does not exist: {info2}")
        made.append(chosen)
        log(f"  PASSED: {owner}/{chosen} exists")
        log("")
        log("  rollover is verified against the live API.")
        log(f"  clean up with:  gh repo delete {owner}/{scratch} --yes")
        log(f"                  gh repo delete {owner}/{chosen} --yes")
        return 0
    finally:
        if made:
            log(f"  (created this run: {', '.join(made)})")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Snapshot a repo before it changes.")
    ap.add_argument("--repo", default="", help="owner/name of the SOURCE repo (not needed with --all)")
    ap.add_argument("--ref", default="", help="the ref about to be replaced (default: the default branch)")
    ap.add_argument("--dumps", default="dumps", help="the dumps repo name (default: dumps)")
    ap.add_argument("--owner", default="", help="the dumps repo owner (default: the source owner)")
    ap.add_argument("--keep", type=int, default=100, help="how many dumps to keep per repo (default 100)")
    ap.add_argument("--no-7z", action="store_true",
                    help="store GitHub's gzip tarball as-is instead of recompressing with 7z -mx=9")
    ap.add_argument("--max-bytes", type=int, default=800 * 1024 * 1024,
                    help="the dumps repo size cap before rolling to a new repo (default 800 MB)")
    ap.add_argument("--public", action="store_true",
                    help="create new dumps repos PUBLIC (default: private, because they hold source code)")
    ap.add_argument("--token", default="", help="a token (default: $GITHUB_TOKEN / $GH_TOKEN)")
    ap.add_argument("--seed-state", action="store_true",
                    help="record a starting state from what is already stored, downloading nothing")
    ap.add_argument("--force", action="store_true",
                    help="ignore the state file and back up every repository, changed or not")
    ap.add_argument("--all", action="store_true",
                    help="dump EVERY repo the token can see (one workflow, one secret, in one repo)")
    ap.add_argument("--check", action="store_true",
                    help="do not back up: verify the token can read the source and write the dumps repo")
    ap.add_argument("--manifest", action="store_true",
                    help="do not back up: regenerate the dumps repo's manifest.json from its tree")
    ap.add_argument("--restore-test", action="store_true",
                    help="do not back up: unpack one dump and compare it, file by file, with the commit it holds")
    ap.add_argument("--at", default="",
                    help="with --restore-test: which snapshot (a YYYY-MM-DDTHHMMSSZ folder). Default: the newest")
    ap.add_argument("--rollover-test", action="store_true",
                    help="do not back up: prove the rollover against the LIVE API on a scratch repo "
                         "(needs administration: write)")
    ap.add_argument("--scratch", default="zz-rollover-test",
                    help="with --rollover-test: the scratch repo name (must start with zz-rollover-test)")
    a = ap.parse_args(argv)

    import os
    token = a.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("no token: set GITHUB_TOKEN (contents:write on the dumps repo, contents:read on the source)")

    if a.rollover_test:
        owner = a.owner or (a.repo.split("/")[0] if "/" in a.repo else "drsamonline")
        print(f"rollover test against {owner}/{a.scratch} (live API)")
        return rollover_test(owner, a.scratch, token)

    if a.all:
        owner = a.owner or (a.repo.split("/")[0] if "/" in a.repo else "drsamonline")
        print(f"backup --all: every repo of {owner}  ->  {owner}/{a.dumps}")
        dumps = pick_dumps_repo(owner, a.dumps, token, a.max_bytes, private=not a.public)
        names = all_repos(owner, token)
        print(f"  {len(names)} repos")

        known = {} if a.force else read_state_all(owner, a.dumps, token)
        if a.force:
            print("  --force: ignoring the state file - every repository will be backed up")
        elif known:
            print(f"  state: {len(known)} repositories have a recorded commit")

        done, failed, skipped, retagged = 0, [], [], 0
        for name in names:
            key = folder_for(owner, name)
            prev = known.get(key) or {}

            # What commit is it on now? Two cheap calls, against a tarball download,
            # a recompress and a push.
            want = repo_state(owner, name, token)
            if want and not a.force:
                _branch, sha, tag = want
                same_sha = bool(sha) and sha == (prev.get("sha") or "")
                same_tag = (tag or "") == (prev.get("tag") or "")
                if same_sha and same_tag:
                    skipped.append(name)
                    continue
                if same_sha and not same_tag:
                    # unchanged code, but a new tag: a release is worth a snapshot even
                    # when nothing moved, because it is the one you would look for later
                    retagged += 1

            if dump_one(owner, name, owner, dumps, token, a.keep,
                        seven_zip=not a.no_7z):
                done += 1
                if want:
                    known[key] = {"sha": want[1], "tag": want[2], "at": stamp()}
            else:
                failed.append(name)
            dumps = pick_dumps_repo(owner, a.dumps, token, a.max_bytes, private=not a.public)  # roll over mid-run

        print(f"  backed up {done} of {len(names)}  "
              f"(skipped {len(skipped)} unchanged, {retagged} re-snapshotted for a new tag)")
        if skipped:
            print(f"  unchanged: {', '.join(skipped[:8])}"
                  + (f" ... and {len(skipped) - 8} more" if len(skipped) > 8 else ""))
        manifest_rc = write_manifest(owner, dumps, token)
        if known and (done or skipped):
            if not write_state(owner, dumps, token, known):
                print("  WARNING: could not write state.json - the next run will re-back-up everything")
                failed.append("state.json")
        if failed:
            # A PARTIAL BACKUP IS NOT A SUCCESSFUL ONE. This returned `0 if done else 1`,
            # so 78 of 83 backed up exited 0 and the five that were missing were a log
            # line inside a green run - which is how The_Browser went unbacked for the
            # life of the corpus without anyone noticing. The run now fails, and names
            # what it could not take.
            print(f"  NOT BACKED UP ({len(failed)}): {', '.join(failed)}")
            return 1
        return manifest_rc

    # These modes do not take a source repository, so they run before it is split.
    if a.seed_state:
        who = a.owner or (a.repo.split("/")[0] if "/" in a.repo else "drsamonline")
        print(f"seed state: {who}/{a.dumps} (nothing will be downloaded)")
        return seed_state(who, a.dumps, token)
    if a.manifest:
        who = a.owner or (a.repo.split("/")[0] if "/" in a.repo else "drsamonline")
        print(f"manifest: {who}/{a.dumps}")
        return write_manifest(who, a.dumps, token)

    if "/" not in a.repo:
        raise SystemExit("--repo must be owner/name (or use --all)")
    src_owner, src_repo = a.repo.split("/", 1)
    dump_owner = a.owner or src_owner
    ref = a.ref or call("GET", f"/repos/{src_owner}/{src_repo}", token).get("default_branch", "main")

    if a.check:
        print(f"token check: {a.repo}  ->  {dump_owner}/{a.dumps}")
        return check(src_owner, src_repo, dump_owner, a.dumps, token)

    if a.restore_test:
        return restore_test(src_owner, src_repo, dump_owner, a.dumps, a.at, token)

    print(f"backup: {a.repo} @ {ref}  ->  {dump_owner}/{a.dumps}")
    blob = tarball(src_owner, src_repo, ref, token)
    if isinstance(blob, dict):
        raise SystemExit(f"could not fetch the tarball: {blob}")
    print(f"  fetched {len(blob)/1e6:.2f} MB")

    dumps = pick_dumps_repo(dump_owner, a.dumps, token, a.max_bytes, private=not a.public)
    folder = folder_for(src_owner, src_repo)
    when = stamp()
    path = f"{folder}/{when}/{src_repo}.tar.gz"

    put = call("PUT", f"/repos/{dump_owner}/{dumps}/contents/{path}", token, {
        "message": f"backup: {a.repo} @ {ref[:12]} ({when})",
        "content": base64.b64encode(blob).decode(),
    })
    if isinstance(put, dict) and put.get("_error"):
        raise SystemExit(f"could not write the dump: {put}")
    print(f"  wrote {dump_owner}/{dumps}/{path}")

    have = list_dump_folders(dump_owner, dumps, folder, token)
    doomed = dumps_to_delete(have, a.keep)
    for old in doomed:
        delete_folder(dump_owner, dumps, f"{folder}/{old}", token)
        print(f"  rotated out {old}")
    print(f"  kept {min(len(have), a.keep)} of {len(have)} (limit {a.keep})")
    return write_manifest(dump_owner, dumps, token)


if __name__ == "__main__":
    sys.exit(main())
