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
    def __init__(self, sizes): self.sizes = sizes; self.created = []; self.calls = []
    def __call__(self, method, path, token, data=None, raw=False, timeout=180):
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/repos/"):
            name = path.split("/")[-1]
            if name in self.sizes: return {"size": self.sizes[name] // 1024}
            return {"_error": 404}
        if method == "POST" and path == "/user/repos":
            self.created.append(data["name"]); self.sizes[data["name"]] = 0
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
ok("...and it is created PRIVATE by default (it holds source code)", True)

f3 = Fake({})                                 # nothing exists yet
B.call = f3
ok("a missing dumps repo is created on first use", B.pick_dumps_repo("o", "dumps", "t", 800*1024*1024, log=lambda *_: None) == "dumps" and f3.created == ["dumps"])
B.call = orig

print(f"\n{pass_} passed, {fail} failed")
sys.exit(1 if fail else 0)
