# Production release workflow — accepted overlay, not Git HEAD

This host's Git HEAD is not the current deployed source/configuration. An
isolated Git checkout is an input, never an instruction to restore the live tree.

## Before any release

1. Read AGENTS.md, QUANT_BACKUP_POLICY.md, current Pin and the last accepted
   release receipt. Preserve UNKNOWN and original evidence. Do not copy a
   business database, run business jobs, or send a notification to test release.
2. Build and test in a separate directory. Declare the exact target files and
   their current SHA256, candidate SHA256, and test receipt bound to the same
   final bytes. A conflict resolved by whole-file copying invalidates the old
   test evidence: merge the live behavior into the candidate and test again.
3. For an authorized business-source publication, use
   `/home/ubuntu/bin/trading-radar-publish check ...` then `apply ...` with the
   explicit reviewed package manifest. Read its CLI help for argument syntax.
   It holds the canonical shared lock and the legacy repository lock, checks
   the independent accepted baseline and all target CAS values before writing,
   and replaces only the declared source files. No arbitrary shell/Git command,
   migration, database, container or sender is part of this publisher.
4. The independent baseline is
   `/var/lib/trading-radar-release-lock/accepted-files.json`. It is outside Git
   and root-owned. The canonical Compose release lock checks it before starting
   a mutating command. Old Pin + old Compose being mutually consistent is not
   sufficient. Missing, unreadable or changed baseline means STOP, not adopt.

## Deployment configuration / images

Business-source publication rejects Pin, Compose, guard, verifier and operational
policy paths. Deliberate image/configuration changes need their own explicitly
authorized release: freeze exact before/after hashes, test final Compose and
runtime identities, preserve config-only rollback preimages, hold both locks,
and atomically advance the independent baseline with the new accepted receipt.
The business publisher cannot regenerate or advance that baseline. Do not edit
the baseline merely to silence a refusal. Do not blindly roll back healthy images.

## Rollback and failure

Never use whole-tree `git checkout`, `git restore`, `git reset`, `git clean`,
`rsync --delete`, or an old release tree copied over the live working directory.
These are historical anti-patterns, not executable recovery instructions.
Use a separately reviewed exact reverse write-set and current SHA preconditions.
Retain all older releases, static resources, UNKNOWN and original evidence.
A partial replace or lost receipt is a reconciliation state; inspect actual
hashes. Do not call it rollback, clear UNKNOWN, or replay automatically.

## Limits

These are cooperative release controls, not a security sandbox: a direct
same-user/root file write, direct Docker command or bypass of the canonical
entrypoint is outside their guarantee. Do not use those bypasses for releases.
No periodic job, permission expansion or service restart is introduced here.
The October 19 natural acceptance schedule and its six repaired files remain
unchanged by this release-flow installation.
