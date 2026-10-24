<div align="center">

# backup

**Snapshot a repository before it changes — so a merge can never lose the code that was there.**

A reusable GitHub workflow + a zero-dependency script. Compressed, timestamped, rotated, and never full.

</div>

---

## What it does

Before a merge replaces a repository's code, `backup` takes the **state that is about to be
replaced** and stores it, compressed, in a **dumps** repository:

```
dumps/
  drsamonline__ml-kit/
    2026-10-24T103000Z/
      ml-kit.tar.gz
    2026-10-24T110000Z/
      ml-kit.tar.gz
  drsamonline__case-kit/
    2026-10-24T103000Z/
      case-kit.tar.gz
```

One folder per repository (`owner__repo`), one folder per backup named with the **date and
time**, and inside it the repository as a `.tar.gz`.

## The two rules that keep it safe

1. **It never fills up.** Each repository keeps the newest **100** backups (configurable).
   Past that, the **oldest is deleted** to make room for the new one — so the most recent
   work is always present and the store stays bounded.
2. **It never stops.** When a dumps repository passes its **size cap** (800 MB by default),
   the next one is **created automatically** — `dumps`, `dumps-2`, `dumps-3`, … — and the
   backup continues there. A full repo can never block a backup.

## How to wire it in — before every merge

Add this to any repository's workflow. It runs on a pull request, so it snapshots the
**base commit** — the code as it is *before* the merge:

```yaml
name: backup
on:
  pull_request:

jobs:
  backup:
    uses: drsamonline/backup/.github/workflows/backup.yml@main
    with:
      keep: 100          # optional
      max_mb: 800        # optional
    secrets:
      BACKUP_TOKEN: ${{ secrets.BACKUP_TOKEN }}
```

One secret, **`BACKUP_TOKEN`**, is required: a token with `contents: write` on the dumps
repositories and `contents: read` on the source repository. (`GITHUB_TOKEN` is scoped to a
single repository, so it cannot write to the dumps repo.)

Run it by hand too:

```bash
GITHUB_TOKEN=... python3 backup.py --repo drsamonline/ml-kit --ref main
GITHUB_TOKEN=... python3 backup.py --repo drsamonline/forge  --ref main --dumps dumps --keep 100
```

## Read this before you make the dumps repo public

**A dumps repository holds your source code.** If *any* repository you back up is private,
its dumps must be private too — a public dumps repo would publish that code. `backup.py`
therefore creates new dumps repos **private by default**; pass `--public` only when every
source repository is already public.

## The options

| flag | default | what it does |
|---|---|---|
| `--repo` | — | the source repo, `owner/name` |
| `--ref` | the default branch | the ref to snapshot — pass the **PR base** to capture the pre-merge state |
| `--dumps` | `dumps` | the dumps repo name |
| `--owner` | the source owner | who owns the dumps repo |
| `--keep` | `100` | backups kept per repository; the oldest beyond it are deleted |
| `--max-bytes` | `800 MB` | the dumps repo size cap before rolling to a new repo |
| `--public` | off | create new dumps repos public (read the warning above) |

## Verified

```
python3 tests/test_backup.py
17 passed, 0 failed
```

The tests cover the decisions that must be right — rotation keeps the newest N and deletes
the oldest, the timestamp sorts chronologically, and a **full dumps repo rolls over to a new
one**. Disabling rotation makes **4** of them fail.

## Licence

MIT. (c) 2026 Dr Sohil Momin. Part of the Forge system.
