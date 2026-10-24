# Creating `BACKUP_TOKEN`

The backup workflow needs **one secret** per repository: `BACKUP_TOKEN`. It is a
**fine-grained personal access token** — the narrow kind, limited to the repositories you
pick and to one permission.

Why not the built-in `GITHUB_TOKEN`? Because it is scoped to a single repository, and the
backup must **write into a different repository** (the dumps repo). Only a PAT (or a GitHub
App) can cross that line.

> **Never paste a token into a chat, an issue, a commit, or a file in a repo.** Set it as a
> secret and nothing else. If one is ever exposed, revoke it on the same page you created it.

---

## 1. Create it — the pre-filled way

GitHub can pre-fill the whole form from a link. **Open this one** and check the three fields
below before you press Generate:

**https://github.com/settings/personal-access-tokens/new?name=Forge+backup&description=Pre-merge+backups+for+drsamonline&target_name=drsamonline&expires_in=90&contents=write**

The link sets:

| field | value | why |
|---|---|---|
| **Token name** | `Forge backup` | so you can find it later |
| **Resource owner** | `drsamonline` | your account owns the repos |
| **Expiration** | 90 days | short is safer; you will rotate it |
| **Repository access** | *(you choose — see below)* | **not** "All repositories" |
| **Permissions → Repository → Contents** | **Read and write** | `write` includes `read` |

### Repository access — pick these

Choose **Only select repositories**, then select:

- **`dumps`** — the store (it needs write)
- **every repository you want backed up** (e.g. `forge`, `ml-kit`, `bmi-bmr-tdee-macros-fluid-calculator`, …)

A fine-grained token has **one** permission set across the repositories you select, so
`Contents: Read and write` applies to all of them. That is fine — it is still far narrower
than a classic `repo`-scoped token.

### Only if you want the automatic rollover

When a dumps repo fills up, `backup.py` **creates the next one** (`dumps-2`, …). Creating a
repository needs one more permission:

- **Permissions → Repository → Administration → Read and write**

If you would rather not grant that, **skip it** — the backup still works; when a dumps repo
fills up you create `dumps-2` by hand and `backup.py` will use it. `--check` tells you which
situation you are in.

Press **Generate token** and **copy it once** (GitHub shows it only once).

## 2. Store it as a secret

The secret must exist on **every repository that calls the backup workflow** — a
`drsamonline` account is a *user*, not an organization, so there are no org-wide secrets to
share.

**In the browser:** the repository → **Settings** → **Secrets and variables** → **Actions**
→ **New repository secret** → Name `BACKUP_TOKEN`, Secret *paste* → **Add secret**.

**With the GitHub CLI:**

```bash
gh secret set BACKUP_TOKEN --repo drsamonline/forge
# it prompts; paste the token
```

Repeat per repository (or loop over them):

```bash
for r in forge ml-kit case-kit bmi-bmr-tdee-macros-fluid-calculator; do
  gh secret set BACKUP_TOKEN --repo drsamonline/$r
done
```

## 3. Verify it — do not assume

Run the check. It **reads the source repo, writes a probe into the dumps repo, deletes the
probe**, and reports whether the rollover would work:

```bash
GITHUB_TOKEN=<paste-here> python3 backup.py --check --repo drsamonline/forge
```

You want to see:

```
  ok    can READ the source repo drsamonline/forge
  ok    can WRITE the dumps repo drsamonline/dumps
  ok    cleaned up the probe
  ok    can CREATE a repo (rollover will work)
```

If the last line says *cannot CREATE a repo*, that is **not** a failure — it only means you
create the next dumps repo by hand when the time comes.

## 4. Rotate it before it expires

Set a reminder for the expiry date. When it passes, the workflow stops being able to write
and the backup job will **skip with a warning** (it will not fail your PRs, and it will not
pretend the backup ran). Create a new token, `gh secret set BACKUP_TOKEN` again, done.
