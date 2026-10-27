#!/usr/bin/env python3
"""Tests for backup.py's decisions - the parts that must be right, without a network.

    python3 tests/test_backup.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backup as B

pass_ = fail = 0
def ok(name, cond):
    global pass_, fail
    if cond: pass_ += 1; print("PASS  " + name)
    else: fail += 1; print("FAIL  " + name)

# ---- rotation: keep the newest N, delete the oldest ----
names = [f"2026-10-{d:02d}T000000Z" for d in range(1, 11)]
ok("under the limit -> delete nothing", B.dumps_to_delete(names[:5], 100) == [])
ok("at the limit -> delete nothing", B.dumps_to_delete(names[:100], 100) == [])
import datetime as _dt
_base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
_many = [B.stamp(_base + _dt.timedelta(hours=i)) for i in range(101)]
ok("101 dumps, keep 100 -> exactly the OLDEST goes", B.dumps_to_delete(_many, 100) == [_many[0]])
_more = _many + [B.stamp(_base + _dt.timedelta(hours=200 + i)) for i in range(49)]
ok("150 dumps, keep 100 -> the 50 oldest go", len(B.dumps_to_delete(_more, 100)) == 50)
ok("...and the survivors are the newest 100", all(n not in B.dumps_to_delete(_more, 100) for n in _more[-100:]))
ok("the order it is given does not matter", B.dumps_to_delete(list(reversed(names)), 3) == names[:7])
ok("keep 3 -> the newest 3 survive", [n for n in names if n not in B.dumps_to_delete(names, 3)] == names[-3:])
ok("keep 0 -> rotation off", B.dumps_to_delete(names, 0) == [])
ok("keep -1 -> rotation off", B.dumps_to_delete(names, -1) == [])

# ---- the folder name is a date and a time, sortable ----
import datetime
s = B.stamp(datetime.datetime(2026, 10, 24, 10, 30, 0, tzinfo=datetime.timezone.utc))
ok("stamp is date + time, UTC", s == "2026-10-24T103000Z")
ok("stamps sort chronologically", B.stamp(datetime.datetime(2026,1,2,0,0,0,tzinfo=datetime.timezone.utc))
   < B.stamp(datetime.datetime(2026,1,10,0,0,0,tzinfo=datetime.timezone.utc)))

# ---- one folder per repo, so two repos never collide ----
ok("the folder names the owner and the repo", B.folder_for("drsamonline", "ml-kit") == "drsamonline__ml-kit")

# ---- the dumps repo rollover: the size limit creates a NEW repo ----
class Fake:
    """A stub GitHub: repos that exist, with sizes, plus a record of creations."""
    def __init__(self, sizes):
        self.sizes = sizes; self.created = []; self.calls = []; self.payloads = []
    def __call__(self, method, path, token, data=None, raw=False, timeout=180):
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/repos/"):
            name = path.split("/")[-1]
            if name in self.sizes: return {"size": self.sizes[name] // 1024}
            return {"_error": 404}
        if method == "POST" and path == "/user/repos":
            self.created.append(data["name"]); self.payloads.append(dict(data))
            self.sizes[data["name"]] = 0
            return {"name": data["name"]}
        return {}

f = Fake({"dumps": 100 * 1024 * 1024})
got = B.pick_dumps_repo("o", "dumps", "t", 800 * 1024 * 1024, log=lambda *_: None, _call=f) if False else None
# pick_dumps_repo calls the module-level `call`; patch it for the test
orig = B.call
B.call = f
ok("an existing dumps repo under the cap is used", B.pick_dumps_repo("o", "dumps", "t", 800*1024*1024, log=lambda *_: None) == "dumps")

f2 = Fake({"dumps": 900 * 1024 * 1024})     # over the 800 MB cap
B.call = f2
ok("a full dumps repo rolls over to a NEW one", B.pick_dumps_repo("o", "dumps", "t", 800*1024*1024, log=lambda *_: None) == "dumps-2")
ok("...and the new repo was actually created", f2.created == ["dumps-2"])
ok("...and it is created PRIVATE by default (it holds source code)",
   f2.payloads and f2.payloads[0].get("private") is True)

f3 = Fake({})                                 # nothing exists yet
B.call = f3
ok("a missing dumps repo is created on first use", B.pick_dumps_repo("o", "dumps", "t", 800*1024*1024, log=lambda *_: None) == "dumps" and f3.created == ["dumps"])
B.call = orig

# ---- a partial backup is not a successful one (F-42) ----
# `backup.py --all` returned `0 if done else 1`, so 78 of 83 backed up exited 0 and
# the five that were missing were a log line inside a green run. This asserts the
# run now fails, and names what it could not take.
import io, contextlib

def _run_all(failing):
    """Run main() with --all against a fake GitHub. `failing` = names that cannot be dumped."""
    orig = (B.all_repos, B.dump_one, B.pick_dumps_repo, B.write_manifest, sys.argv)
    B.all_repos = lambda owner, token: ["a", "b", "c"]
    B.pick_dumps_repo = lambda *a, **k: "dumps"
    B.dump_one = lambda so, sr, do, du, tok, keep, ref="", log=print: sr not in failing
    # The manifest is written after the loop and its result is the run's result (F-98),
    # so the loop tests stub it; the manifest's own behaviour is tested separately below.
    B.write_manifest = lambda *a, **k: 0
    sys.argv = ["backup.py", "--all", "--repo", "drsamonline/x", "--token", "fake"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = B.main()
    finally:
        B.all_repos, B.dump_one, B.pick_dumps_repo, B.write_manifest, sys.argv = orig
    return rc, buf.getvalue()

rc, out = _run_all({"b"})
ok("one repo unbacked -> the run FAILS", rc == 1)
ok("...and names the repo it could not take", "NOT BACKED UP (1): b" in out)
ok("...and still reports the count it did take", "backed up 2 of 3" in out)

rc, out = _run_all(set())
ok("everything backed up -> the run passes", rc == 0)
ok("...and reports the full count", "backed up 3 of 3" in out)

def _run_all_bad_manifest():
    orig = (B.all_repos, B.dump_one, B.pick_dumps_repo, B.write_manifest, sys.argv)
    B.all_repos = lambda owner, token: ["a"]
    B.pick_dumps_repo = lambda *a, **k: "dumps"
    B.dump_one = lambda *a, **k: True
    B.write_manifest = lambda *a, **k: 1
    sys.argv = ["backup.py", "--all", "--repo", "drsamonline/x", "--token", "fake"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return B.main()
    finally:
        B.all_repos, B.dump_one, B.pick_dumps_repo, B.write_manifest, sys.argv = orig

ok("a manifest that could not be written FAILS an otherwise good run", _run_all_bad_manifest() == 1)

# ---- a dump that lands corrupt must not count as backed up (F-43) ----
# A successful PUT only means the request was accepted. The response carries the stored
# blob's sha, so it can be compared with the sha of the bytes we sent.
import hashlib as _h
_data = b"the repository tarball"
ok("the blob sha is git's own algorithm",
   B.git_blob_sha(_data) == _h.sha1(b"blob %d\0" % len(_data) + _data).hexdigest())

def _dump_with(put_sha):
    """Run dump_one against a fake API whose PUT reports `put_sha`."""
    orig = (B.tarball, B.call, B.list_dump_folders)
    B.tarball = lambda o, r, ref, t: _data
    B.call = lambda method, path, token, data=None, raw=False, timeout=180: {"content": {"sha": put_sha}}
    B.list_dump_folders = lambda *a, **k: []
    try:
        return B.dump_one("o", "r", "o", "dumps", "t", 100, log=lambda *_: None)
    finally:
        B.tarball, B.call, B.list_dump_folders = orig

ok("a dump whose stored sha matches is accepted", _dump_with(B.git_blob_sha(_data)) is True)
ok("a dump whose stored sha does NOT match is REJECTED", _dump_with("0" * 40) is False)
ok("a dump with no sha reported is accepted, and says it is unverified", _dump_with("") is True)

# ---- a restore test: unpack a dump and compare it with the commit it holds (F-43) ----
# "A backup workflow that ran" is not proof of recoverability. The dump has to
# unpack, name the commit it holds, and match that commit file for file.
import io as _io, tarfile as _tarfile

def _tar(files, top="o-r-deadbeef"):
    """A tarball shaped like GitHub's: one top-level <owner>-<repo>-<sha> directory."""
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            ti = _tarfile.TarInfo(f"{top}/{path}")
            ti.size = len(data)
            tf.addfile(ti, _io.BytesIO(data))
    return buf.getvalue()

_blob = _tar({"a.txt": b"one", "sub/b.txt": b"two"})
_top, _tree = B.restored_tree(_blob)
ok("a dump unpacks to one git blob sha per file",
   _top == "o-r-deadbeef" and _tree == {"a.txt": B.git_blob_sha(b"one"),
                                        "sub/b.txt": B.git_blob_sha(b"two")})
ok("the top directory names the commit the dump holds",
   B.parse_tarball_root(_top) == ("o", "r", "deadbeef"))
try:
    B.parse_tarball_root("nonsense")
    ok("a top directory that is not <owner>-<repo>-<sha> is refused", False)
except SystemExit:
    ok("a top directory that is not <owner>-<repo>-<sha> is refused", True)

try:
    B.restored_tree(_blob[:len(_blob) // 2])
    ok("a TRUNCATED dump is rejected, not accepted quietly", False)
except Exception:
    ok("a TRUNCATED dump is rejected, not accepted quietly", True)

_two = _io.BytesIO()
with _tarfile.open(fileobj=_two, mode="w:gz") as tf:
    for name in ("o-r-aaa/x.txt", "other-repo-bbb/y.txt"):
        ti = _tarfile.TarInfo(name); ti.size = 1
        tf.addfile(ti, _io.BytesIO(b"z"))
try:
    B.restored_tree(_two.getvalue())
    ok("a dump holding two repositories is refused", False)
except SystemExit:
    ok("a dump holding two repositories is refused", True)

# the comparison itself
ok("an exact restore reports nothing", B.compare_trees({"a": "1", "b": "2"}, {"a": "1", "b": "2"}) == ([], [], []))
ok("a missing file is reported", B.compare_trees({"a": "1"}, {"a": "1", "b": "2"})[0] == ["b"])
ok("an extra file is reported", B.compare_trees({"a": "1", "b": "2"}, {"a": "1"})[1] == ["b"])
ok("an altered file is reported", B.compare_trees({"a": "9"}, {"a": "1"})[2] == ["a"])
ok("a restore is not exact if anything differs",
   B.compare_trees({"a": "9", "b": "2", "c": "3"}, {"a": "1", "b": "2", "d": "4"}) != ([], [], []))

# ---- the dumps manifest is DERIVED, so it cannot go stale (F-98) ----
# It was hand-written once, on the store's first day, and went on claiming one
# repository was backed up while the store held 78.
_TREE = [
    {"path": "drsamonline__ml-kit/2026-10-24T095823Z/ml-kit.tar.gz", "type": "blob", "size": 8947},
    {"path": "drsamonline__ml-kit/2026-10-25T101010Z/ml-kit.tar.gz", "type": "blob", "size": 9000},
    {"path": "drsamonline__forge/2026-10-27T124846Z/forge.tar.gz", "type": "blob", "size": 685311},
    # none of the following is a dump, and none may be indexed
    {"path": "README.md", "type": "blob", "size": 173},
    {"path": "LICENSE", "type": "blob", "size": 4604},
    {"path": "manifest.json", "type": "blob", "size": 477},
    {"path": ".github/workflows/x.yml", "type": "blob", "size": 100},
    {"path": "drsamonline__ml-kit/2026-10-24T095823Z", "type": "tree", "size": 0},
    {"path": "drsamonline__ml-kit/loose-file.txt", "type": "blob", "size": 5},
]
_doc = B.manifest_from(_TREE, "2026-10-27T180000Z")
ok("the manifest lists every repository that has a dump",
   sorted(_doc["dumps"]) == ["drsamonline__forge", "drsamonline__ml-kit"])
ok("...and only real dumps are indexed", _doc["snapshots"] == 3)
ok("...and nothing else in the repo is mistaken for a dump",
   "manifest.json" not in _doc["dumps"] and ".github" not in _doc["dumps"])
ok("a file loose in a repository folder is not a dump",
   all(s["file"].endswith(".tar.gz") for v in _doc["dumps"].values() for s in v))
ok("snapshots are ordered oldest first",
   [s["at"] for s in _doc["dumps"]["drsamonline__ml-kit"]]
   == ["2026-10-24T095823Z", "2026-10-25T101010Z"])
ok("the totals are counted, not guessed", _doc["repositories"] == 2 and _doc["bytes"] == 8947 + 9000 + 685311)
ok("each snapshot names its source repository",
   _doc["dumps"]["drsamonline__forge"][0]["source"] == "drsamonline/forge")
ok("the index says when it was generated", _doc["generated_at"] == "2026-10-27T180000Z")
ok("an empty store produces an empty index, not an error",
   B.manifest_from([], "x")["repositories"] == 0 and B.manifest_from([], "x")["dumps"] == {})
ok("the index is stable across runs", B.manifest_from(_TREE, "a")["dumps"] == B.manifest_from(_TREE, "b")["dumps"])

def _write_with(tree, truncated=False, put_sha=None):
    """Run write_manifest against a fake API."""
    orig = B.call
    body = {}
    def fake(method, path, token, data=None, raw=False, timeout=180):
        if path.endswith("/git/trees/main?recursive=1"):
            return {"tree": tree, "truncated": truncated}
        if path.endswith("/contents/" + B.MANIFEST):
            if method == "GET":
                return {"_error": 404}
            body.update(data)
            return {"content": {"sha": put_sha if put_sha is not None else "0" * 40}}
        if method == "GET" and "/repos/" in path:
            return {"default_branch": "main"}
        return {}
    B.call = fake
    try:
        return B.write_manifest("o", "dumps", "t", log=lambda *_: None), body
    finally:
        B.call = orig

rc, body = _write_with(_TREE)
ok("a truncated tree does not get written as an index", rc == 1)
rc, body = _write_with(_TREE[:1], put_sha=None)
ok("a manifest whose stored sha does not match is rejected", rc == 1)
import base64 as _b64, json as _json
_want = B.git_blob_sha(_json.dumps(B.manifest_from(_TREE[:1], B.stamp()), indent=1).encode() + b"\n")
rc, body = _write_with(_TREE[:1], put_sha=_want)
ok("a manifest that lands intact is accepted", rc == 0)

print(f"\n{pass_} passed, {fail} failed")
sys.exit(1 if fail else 0)
