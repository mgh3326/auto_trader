# Task 934: prune old auto_trader images after a successful NCP deploy

Frozen before implementation. Purpose: stop unused `ghcr.io/mgh3326/auto_trader`
images from filling the NCP disk (2026-09-29: 66 unused images, pull failed at
100% disk) without ever deleting the image a rollback or a live container needs,
and without ever touching another repository.

## Keep rules

Each keep rule below is one invariant sentence. The acceptance suite counts the
`KEEP-` lines in this file and requires exactly one assertion-RED mutant per rule
(`tests/scripts/test_deploy_ncp_pull_image_prune.py`).

- KEEP-1 current: the digest this run just promoted (equal to `deployed-digest`) is never removed.
- KEEP-2 previous: the digest recorded in `deployed-digest.previous` at prune time (the manual rollback target) is never removed.
- KEEP-3 in-use: an image whose ID equals the `.Image` of any container listed by `docker ps -a` (running or stopped, including a unit skipped by `--skip-kis-ws` or `MCP_UNITS_SKIP`) is never removed.
- KEEP-4 repository: an image is a removal candidate only when every one of its RepoTags and RepoDigests has a repository part exactly equal to `IMAGE_REPOSITORY`; any foreign, mixed or reference-less image is never removed.

## Invariant table

| Invariant | Input and transition | Enforcement point | Dependencies | Independent observation |
| --- | --- | --- | --- | --- |
| Prune runs only after a fully successful deploy. | `main` reaches the statement after `promote_digest` only when it returned 0 (errexit). | `main`, after `promote_digest`; `manual_rollback` and `dry_run` never call `prune_old_images`. | errexit in `main`; `promote_digest` return code. | Failure, rollback and dry-run runs issue no `image ls`/`image rm`. |
| Prune can never change the deploy result. | Any error, unset variable or `exit` inside prune. | `run_image_prune` runs prune in a subshell joined by `\|\|` to a warning. | Subshell isolation. | Injected `image rm`/`image ls`/`ps` failures keep rc 0, the MATCH table and `deployment completed`; stderr names what was not removed. |
| Keep set is exact. | Recorded digests (`is_digest`), `docker image inspect` IDs, container `.Image` IDs, image RepoTags/RepoDigests. | `plan_image_prune`: string equality only; IDs must match `sha256:<64 hex>`. | Docker inspect output. | Prefix-sharing repositories (`auto_trader-dev`, `auto_trader_backup`) and a digest differing in one hex digit are handled by equality, not prefix. |
| Unreadable state keeps everything. | `deployed-digest.previous` absent/invalid, `deployed-digest` not equal to the promoted digest, `docker ps -a`/`inspect`/`image ls` failure or unexpected output. | `prune_old_images` / `plan_image_prune` return before any removal. | `read_digest`, `is_image_id`. | Warning on stderr, zero `image rm` calls, rc 0. |
| Removal is by reference, never forced; success is judged by outcome. | `docker image rm <refs>` without `-f`; no `system prune`, `image prune`, volume, network or container removal. The daemon drops a repository's digest references with its last tag, so a later reference in the same call may error although the image is gone. | `prune_old_images`: an image counts as removed only when a fresh `docker image ls` no longer lists its ID. | Daemon in-use conflict check as a second guard. | Call log contains no `-f` on `image rm` and no prune/volume/network/system subcommand; a multi-tag image is reported removed without a warning; an `rm` that exits 0 but leaves the image is a warning. |
| Operator sees what happened. | Each removed image ID and its references; total count and summed image size. | `prune_old_images` stdout. | `docker image inspect --format {{.Size}}`. | Output lists removed IDs, `removed N image(s)` and bytes, or `size unavailable`. |
| Operator can disable it. | `AT_IMAGE_PRUNE_ENABLED=0` disables; any value other than 0/1 skips with a warning; default 1. | `prune_old_images`, `dry_run`. | Env var. | No `image ls`/`image rm` with 0 or an invalid value. |

## AC review (builder, before implementation)

- Purpose: reclaim disk from unused auto_trader images at the only moment the
  deploy state is known good, so the next pull has room.
- Each AC against the purpose: AC1/AC4 bound what can be deleted (the only
  irreversible part); AC2 keeps a housekeeping failure from turning a good
  deploy into a reported failure (which would invite a needless rollback); AC3
  proves the rollback target survives the prune in a two-run stateful test; AC5
  makes the reclaim observable; AC6 turns each keep rule into a mutant-killing
  test; AC7 gives the operator an off switch.
- Counterexamples that pass every AC yet hurt the purpose, recorded as risks:
  (a) layers shared between a removed and a kept image are not freed, so the
  summed size overstates the reclaim (the log says so); (b) repeated failed
  deploys never prune, so disk can still fill between successes (bounded by
  the number of failed pulls); (c) a container created by hand concurrently
  with the prune is not in the snapshot, but `docker image rm` without `-f`
  still refuses an image used by any container; (d) reference-less images and
  build cache are never touched, so a host that builds locally still needs
  manual cleanup (NCP pulls only).

## Acceptance commands

Run only against fake docker and curl shims: `bash -n scripts/deploy-ncp-pull.sh`;
`shellcheck scripts/deploy-ncp-pull.sh` (no findings beyond the pre-existing
SC2016 info in `schedule_drain`); `uv run pytest tests/scripts/test_deploy_ncp_pull_image_prune.py tests/scripts/test_deploy_ncp_pull_rollback.py tests/scripts/test_deploy_ncp_pull_digest_pin.py -q`.
No production host, app, MCP server, broker, database or real env file participates.
