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
import json
import sys
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


# --------------------------------------------------------------------------- #
# The dumps repo: pick one under the cap, creating the next if it is full
# --------------------------------------------------------------------------- #
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
        size = (info.get("size") or 0) * 1024          # the API reports KB
        if size < cap_bytes:
            log(f"  using {owner}/{name} ({size/1e6:.1f} MB of {cap_bytes/1e6:.0f} MB)")
            return name
        log(f"  {owner}/{name} is full ({size/1e6:.1f} MB) - rolling over")
        n += 1


def list_dump_folders(owner, dumps, folder, token):
    """The timestamp folders already in <dumps>/<folder>/."""
    items = call("GET", f"/repos/{owner}/{dumps}/contents/{folder}", token)
    if isinstance(items, dict) and items.get("_error") == 404:
        return []
    if isinstance(items, dict) and items.get("_error"):
        raise SystemExit(f"could not list {folder}: {items}")
    return sorted(i["name"] for i in items if i.get("type") == "dir")


def delete_folder(owner, dumps, path, token):
    """Delete a folder (a dump) via the Git trees API: remove its files, then it."""
    items = call("GET", f"/repos/{owner}/{dumps}/contents/{path}", token)
    if not isinstance(items, list):
        return
    for it in items:
        call("DELETE", f"/repos/{owner}/{dumps}/contents/{it['path']}", token,
             {"message": f"rotate: drop old backup {path}", "sha": it["sha"]})


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Snapshot a repo before it changes.")
    ap.add_argument("--repo", required=True, help="owner/name of the SOURCE repo")
    ap.add_argument("--ref", default="", help="the ref about to be replaced (default: the default branch)")
    ap.add_argument("--dumps", default="dumps", help="the dumps repo name (default: dumps)")
    ap.add_argument("--owner", default="", help="the dumps repo owner (default: the source owner)")
    ap.add_argument("--keep", type=int, default=100, help="how many dumps to keep per repo (default 100)")
    ap.add_argument("--max-bytes", type=int, default=800 * 1024 * 1024,
                    help="the dumps repo size cap before rolling to a new repo (default 800 MB)")
    ap.add_argument("--public", action="store_true",
                    help="create new dumps repos PUBLIC (default: private, because they hold source code)")
    ap.add_argument("--token", default="", help="a token (default: $GITHUB_TOKEN / $GH_TOKEN)")
    a = ap.parse_args(argv)

    import os
    token = a.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("no token: set GITHUB_TOKEN (contents:write on the dumps repo, contents:read on the source)")

    if "/" not in a.repo:
        raise SystemExit("--repo must be owner/name")
    src_owner, src_repo = a.repo.split("/", 1)
    dump_owner = a.owner or src_owner
    ref = a.ref or call("GET", f"/repos/{src_owner}/{src_repo}", token).get("default_branch", "main")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
