# NCP GHCR pull deployment

The NCP host deploys a previously built image; it does not build from a source
checkout. `scripts/deploy-ncp-pull.sh` pulls one GHCR tag. API is blue/green:
`at-api-blue` on loopback `:8001` and `at-api-green` on loopback `:8002`; private
HAProxy owns stable `127.0.0.1:8000` and `100.122.100.56:8000`. It health-checks
the inactive color before an in-place HAProxy HUP, records the active color only
after that switch, and drains the previous API for `API_DRAIN_SECONDS` (120 by
default). The same promotion also advances the private MCP fleet:
an inactive blue/green default MCP color, five fixed profiles, and its
loopback/tailnet-only HAProxy front end.

## Tag policy

- `main` is the latest image built from a verified `main` push. It is suitable
  for the normal promotion path, but is intentionally movable.
- `sha-<short7>` identifies the exact `main` commit image and is the rollback
  or incident-pinning choice.
- `production` remains a manual release tag. This workflow does not advance it
  on a `main` push.

The GitHub Actions image workflow still supports `workflow_dispatch` and
published releases. It also ignores a `main` push when every changed path is
under `docs/` or is a Markdown file.

## One-time host preparation

The host's existing GHCR personal access token must have `read:packages` for
`mgh3326/auto_trader`. Authenticate it on the NCP host; do not add it to this
repository:

```bash
printf '%s' "$GHCR_PAT" | docker login ghcr.io -u mgh3326 --password-stdin
```

Keep the two existing runtime env files outside the repository. By default the
script uses `/root/at-run/.env.runtime` and `/root/at-run/.env.secrets`; hosts
using different established filenames can set `AT_RUNTIME_ENV_FILE` and
`AT_SECRETS_ENV_FILE`. No env contents are copied into the image or script.

Install the versioned operator script with restricted permissions:

```bash
install -m 0750 scripts/deploy-ncp-pull.sh /root/at-run/deploy-ncp-pull.sh
```

Before pulling, the script requires an existing active API and checks every
present app container is running and has its own resolvable immutable GHCR
repository digest. It reads the container's configured digest, then image
metadata for a mutable tag. If neither identifies that unit's own digest, it
exits before replacing anything. A local-only tagged container must be
replaced by the operator with an image that has a GHCR repository digest
before this deployment can proceed. Absent optional units remain absent on
rollback if the run created them before failing.

## Promote an image

Run only on the NCP host:

```bash
/root/at-run/deploy-ncp-pull.sh
/root/at-run/deploy-ncp-pull.sh sha-abcdef0
```

All units use `--network host`, the two existing `--env-file` arguments,
and `--restart unless-stopped`. The image now copies `research_contracts/` and
`config/`, so this deployment deliberately has no bind mounts for either path.
`at-scheduler` runs the image's TaskIQ scheduler command; each API color runs
Uvicorn with `--host 127.0.0.1 --port 8001|8002`; `at-worker` runs
`/app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1`.
`at-upbit-ws` and `at-kis-ws` respectively run
`/app/.venv/bin/python websocket_monitor.py --mode upbit` and
`/app/.venv/bin/python websocket_monitor.py --mode kis`.

The MCP default profile is promoted blue/green behind private HAProxy while
the previous color drains. Fixed MCP profiles are recreated from the same
digest with profile-scoped environment policy (including the required
approval-hash modes for TradingCodex execution). HAProxy must remain bound only
to loopback and the configured tailnet address; it is never a public listener.

The script resolves the GHCR repo digest after pulling it. It retries the inactive
API color's loopback `/healthz` for up to 60 seconds by default. If the new API
does not return HTTP 200, the worker is not running and does not emit its
TaskIQ startup line, or either WebSocket is not running and does not emit a
`Unified WebSocket health ... connected=True` (or equivalent `connected=True`)
startup line before the bounded wait expires, it restores every unit this run
already replaced, in reverse replacement order, using each unit's prior
digest. Untouched units stay untouched. A later MCP failure uses the same
rollback path, then restores the prior HAProxy config and active colors. The
final table compares the expected digest with each container's running digest;
any mismatch leaves the command nonzero. A tag is used only to pull and resolve
the image; `docker run` always receives `repo@sha256:...`, so the next
deployment's `.Config.Image` is stable even after a later `:main` pull.
If container inspection fails during the preflight snapshot, the script checks
the container list and stops before mutation unless absence is confirmed.

The script maintains these operator-owned, mode-0600 digest files:

- `/root/at-run/deployed-digest` is the last successful promotion target.
  A skipped KIS WebSocket can retain a different digest, as the table shows.
- `/root/at-run/deployed-digest.previous` is the prior healthy deployment;
  each successful deployment records the former current value here.

Automatic rollback uses the per-container preflight snapshot. A floating tag
is resolved through that container's image metadata; the API digest is never
substituted for another unit. If resolution fails, no replacement begins.
Automatic rollback does not rotate either digest file. A successful promotion
rotates the prior target into `deployed-digest.previous`.

## KIS WebSocket skip flag and dry-run plan

Both flags combine with a tag or with --rollback, in any order:

    /root/at-run/deploy-ncp-pull.sh sha-abcdef0 --skip-kis-ws
    /root/at-run/deploy-ncp-pull.sh --dry-run sha-abcdef0
    /root/at-run/deploy-ncp-pull.sh --rollback --skip-kis-ws
    /root/at-run/deploy-ncp-pull.sh --dry-run --skip-kis-ws

--dry-run prints a read-only plan and exits. It reports the intended digest
resolved from the locally inspectable image, the planned action for every
unit, and, when --skip-kis-ws is set, the skip reason and retained KIS
digest. When no local repo digest is inspectable it prints the literal word
unresolved instead of claiming a digest; a real run pulls first and resolves
there. Dry-run performs no docker pull, run, rm, stop, rename, or kill and
writes no HAProxy route or color files. A rollback dry-run instead reads
deployed-digest.previous and reports it (or unresolved) as the target.

--skip-kis-ws is an explicit operator decision. The script performs no
automatic host-local holder detection; reliable detection is not established
in this repository, so the flag is the only authority. It wins regardless of
any optional holder evidence. Pass it whenever fillwire holds the KIS fill
stream (the fillwire #180 observation window) and the existing at-kis-ws
must remain undisturbed. A skipped at-kis-ws is never stopped, removed,
renamed, or recreated — including during rollback after a later phase
failure.

The retained KIS digest can legitimately differ from the promoted digest.
After the run the operator must inspect the final per-container digest
table: every replaced unit must report the intended digest as running, while
at-kis-ws reports its own retained digest.

If --skip-kis-ws was supplied and at-kis-ws was already stopped, its digest
table row reports SKIPPED_STOPPED: expected is its last known immutable digest
when available (otherwise UNKNOWN), and running is STOPPED. This is an
intentional skip, not a digest mismatch; the container is left untouched.
Without the flag, a stopped at-kis-ws still fails capture. Any other stopped
unit still fails capture or digest verification and triggers the usual rollback.

## Operator rollback

To return to the prior healthy digest without selecting a tag, run only on the
NCP host:

```bash
/root/at-run/deploy-ncp-pull.sh --rollback
```

This pulls and runs the digest in `deployed-digest.previous`, checks all five
core units, and promotes the MCP fleet through its same private HAProxy flow.
It rotates the digest files so the prior current deployment remains available
for a later return. It fails explicitly if
`deployed-digest.previous` is absent or invalid. Do not edit either file to
bypass a failed rollback; investigate the image and container logs instead.

Do not run migrations or make a live deployment from CI as part of this flow.

## Expected interruption budget

| Role | Expected interruption |
| --- | --- |
| API | 0 after the one-time first cutover; first adoption has a documented ≤2s bind handoff while legacy `at-api` releases port 8000; HAProxy then polls both routes for at most `HAPROXY_READY_ATTEMPTS` × `HAPROXY_READY_INTERVAL` (default 20 × 0.5s = 10s) |
| Worker | 0; `at-worker-new` must report ready before `docker stop -t 60 at-worker` |
| Scheduler | a few seconds; exactly one instance is retained to prevent duplicate firing |
| WebSocket monitors | a few seconds; broker appkeys permit only one session |
| MCP | 0; existing blue/green HAProxy drain |

## Worker operation notes

`at-worker`, `at-upbit-ws`, and `at-kis-ws` are NCP-only trading roles. Their
Mac launchd plists are intentionally absent: re-enabling a Mac worker would
create competing TaskIQ consumers, and re-enabling either WebSocket would
duplicate its monitor. The KIS rate limiter remains process-local, so observe
KIS 429s while diagnosing any out-of-band process. For Toss, use
`TOSS_RATE_LIMITER_BACKEND=redis` on every intentionally active worker
environment as recommended by #2004.
