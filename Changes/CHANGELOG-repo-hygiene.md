# Changelog: repo hygiene (no code changes)

Base commit expected: `dfd72aa` (Simulated attendance report parity +
click-through). If `git log --oneline -1` does not show this, an earlier
zip was skipped -- stop and tell Claude.

## What changed
- `.gitattributes` -- LF in the repo on every OS; binaries (.pt, .png,
  .jpg, .mp4, .docx, .zip) explicitly untouched. Removes the
  "LF will be replaced by CRLF" warnings on Windows.
- `.gitignore` -- ignores `logs/`, `__pycache__/`, `.pytest_cache/`,
  `.env`, `*.db`, `*.sqlite3` so test logs and local databases never get
  committed by `git add -A`.

No Python, JS or test files are touched, so no test run is needed.

## Verify on Windows (after xcopy)
    git status
Expect only `.gitattributes`, `.gitignore` and
`Changes/CHANGELOG-repo-hygiene.md` as new files, and no other file listed
as modified.

## Verify on Lightning (after pull)
    git log --oneline -1
    mkdir -p logs
    pytest tests/real-app-smoke -v 2>&1 | tee logs/repo-hygiene.txt
    git status
Expect the pytest result to match your last run, and `git status` to show
a clean tree (logs/ is now ignored).

## Log to send Claude
Send `logs/repo-hygiene.txt` and the output of `git log --oneline -1`.
