# Original self-check artifacts

`selfcheck-20261016.zip` preserves the original contents of the
`selfcheck-20261016/` evidence directory, including command logs, the legacy
schema snapshot, worker digests, and the pre-existing worker-file backup.

The archive retains whitespace in raw SQL dumps, terminal output and patch
snapshots without changing historical evidence to satisfy source whitespace checks.
The acceptance decision and review reports are checked in as readable Markdown.

To restore the raw files at the paths referenced by those reports:

```sh
rtk proxy python -m zipfile -e .omo/evidence/G1/selfcheck-20261016.zip .omo/evidence/G1
```
