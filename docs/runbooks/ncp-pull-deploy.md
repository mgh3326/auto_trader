# NCP GHCR pull deployment

The NCP host deploys a previously built image; it does not build from a source
checkout. `scripts/deploy-ncp-pull.sh` pulls one GHCR tag. API is blue/green:
`at-api-blue` on loopback `:8001` and `at-api-green` on loopback `:8002`; private
HAProxy owns stable `127.0.0.1:8000` and `100.122.100.56:8000`. It health-checks
the inactive color before an in-place HAProxy HUP, records the active color only
after that switch, and drains the previous API for `API_DRAIN_SECONDS` (120 by
default). The same promotion also advances the private MCP fleet:
an inactive blue/green default MCP color, three fixed profiles
(kiwoom, h3-crypto-paper, h3-us-paper; ROB-1436 retired the others, see mcp-unit-retirement-rob1436.md), and its loopback/tailnet-only
HAProxy front end.

A forward deploy exits 78 before any mutation while a ROB-1436 retired unit is
running or can restart (`RETIRED_CONTAINERS`). Stop and disable those units
first: [mcp-unit-retirement-rob1436.md](mcp-unit-retirement-rob1436.md) §3.

## Exact-SHA full-suite gate (ROB-1456)

Every forward deploy, including `--dry-run`, checks GitHub **before the first
image pull, file write or container change**. `scripts/deploy_ci_gate.py` reads
the remote tag using `docker buildx imagetools inspect`, pins its registry
digest, and reads `org.opencontainers.image.revision` from that immutable
image's platform configs. Every runnable platform must name the same full
40-character commit SHA. It never substitutes the current `main` tip, a
short tag suffix, or a locally cached image's revision.

For that exact SHA, the newest `Test` workflow push/dispatch run and its latest
attempt must contain all six completed, successful jobs:

- `lint`
- `taskiq-smoke`
- `test (3.13, 1)` through `test (3.13, 4)`

These names come from `.github/workflows/test.yml` (`lint`, `taskiq-smoke`,
the Python `3.13` × groups `[1, 2, 3, 4]` matrix). ROB-1454's fast PR gate
does not replace this evidence: neither `PR gate`, `ci-required`, a PR run,
nor a skipped/neutral shard is accepted. Results cannot be combined across
runs, attempts or commits. Missing, red, running, cancelled or unreadable
checks print their names and refuse with **exit 78**. API/authentication
failures also fail closed. There is no implicit wait/retry; rerun after CI
finishes. A manual full-suite run uses `gh workflow run test.yml --ref <branch>`
(ROB-1454); confirm its `head_sha` is the intended commit.

On success the script pulls and promotes the **already verified digest**,
not the tag again. A tag moving between inspection and pull cannot change
the deployed commit. Both automatic failure recovery and explicit `--rollback`
remain **unchecked emergency paths**, using their existing immutable targets.

Break-glass forward deploy (operator decision; keep the reason in the incident
record and do not put secrets in it):

```bash
/root/at-run/deploy-ncp-pull.sh sha-abcdef0 --dry-run \
  --allow-unverified 'INC-123: operator accepted missing full-suite evidence'
/root/at-run/deploy-ncp-pull.sh sha-abcdef0 \
  --allow-unverified 'INC-123: operator accepted missing full-suite evidence'
```

`--allow-unverified REASON` requires a non-blank reason and logs `OVERRIDE`,
the exact SHA and JSON-escaped reason, along with the missing/red checks.
It waives only CI evidence, not image identity, retired-unit checks or other
deployment preconditions. A missing digest/revision or mixed platform SHAs
must be corrected, even when overriding CI.

The GHCR application image workflow (`.github/workflows/deploy.yml`) uses the
same gate before QEMU/Buildx setup, registry login and build/push. Automatic
builds follow completed `Test` runs for this repository's `main` pushes, so
CI is not raced by an immediate build. Checkout, build arguments, OCI revision
and `sha-<short7>` tag use that run's `head_sha`, not the newer default-branch
tip. `workflow_dispatch` and published releases check their own event SHA;
manual dispatch offers `allow_unverified` for the same logged override.
Waiting via `workflow_run` means docs-only main pushes now also build after
CI (there is no `paths-ignore` on that event).

This gate covers the NCP pull path and its application image producer.
`scripts/deploy.sh` and `scripts/deploy-native.sh` remain retired and refuse
execution. `build-caddy.yml` publishes a separate legacy Caddy image, not the
NCP app image (NCP uses HAProxy); existing one-shot jobs consume
`deployed-digest` rather than promote a new image.


## Tag policy

- `main` is the latest image built from a verified `main` push. It is suitable
  for the normal promotion path, but is intentionally movable.
- `sha-<short7>` identifies the exact `main` commit image and is the rollback
  or incident-pinning choice.
- `production` remains a manual release tag. This workflow does not advance it
  on a `main` push.

The GitHub Actions image workflow also supports `workflow_dispatch` and
published releases; both require the same exact-SHA full-suite evidence.

## One-time host preparation

The host's existing GHCR personal access token must have `read:packages` for
`mgh3326/auto_trader`. Authenticate it on the NCP host; do not add it to this
repository:

```bash
printf '%s' "$GHCR_PAT" | docker login ghcr.io -u mgh3326 --password-stdin
```

The host also needs Python 3 (stdlib only), Docker's Buildx plugin, and GitHub
CLI `gh` with authenticated read access to this repository's Actions runs/jobs.
Use the operator's established `gh` authentication or inherited `GH_TOKEN`;
the gate never reads runtime env files for GitHub credentials. Verify access
without printing tokens: `gh api repos/mgh3326/auto_trader/actions/workflows/test.yml`.

Keep the two existing runtime env files outside the repository. By default the
script uses the NCP host's files `/root/at-secrets/.env.api` and
`/root/at-secrets/.env.scheduler`, i.e. `${AT_SECRETS_DIRECTORY}/.env.api` and
`${AT_SECRETS_DIRECTORY}/.env.scheduler` with `AT_SECRETS_DIRECTORY` defaulting
to `/root/at-secrets` (ROB-1380, measured by operator-desk on deploy #4,
2026-10-17; the earlier defaults `.env.runtime`/`.env.secrets` and then
`/root/at-run/.env.*` did not match the host). Hosts using other established
paths set `AT_SECRETS_DIRECTORY`, or name each file with `AT_RUNTIME_ENV_FILE`
and `AT_SECRETS_ENV_FILE`, which win over the directory. Every
unit receives both files in that order, so a key present in both takes the
value from the second file. A missing file stops the run before any pull or
container change with `required env file is unavailable: <path> (set <VAR> to
the host file)`. No env contents are copied into the image or script.

Install the versioned operator script, gate helper and HAProxy template from
the same merged commit, with restricted permissions. The NCP host keeps the
template in the run directory's ops tree:

```bash
install -m 0750 scripts/deploy-ncp-pull.sh /root/at-run/deploy-ncp-pull.sh
install -m 0750 scripts/deploy_ci_gate.py /root/at-run/deploy_ci_gate.py
install -D -m 0644 ops/ncp/haproxy/haproxy.cfg.tmpl /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl
```

The installed copy has no repository around it. The script picks the HAProxy
template in this order: `--haproxy-template PATH`, then `MCP_HAPROXY_TEMPLATE`,
then whichever one of these exists: `haproxy.cfg.tmpl` next to the script (an
installed sibling copy), `${AT_RUN_DIRECTORY}/ops/ncp/haproxy/haproxy.cfg.tmpl`
(`/root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl`, the NCP layout), or
`../ops/ncp/haproxy/haproxy.cfg.tmpl` (a repository checkout). Two of those
paths that reach the same file count once. If none exists, if more than one
exists, or if an explicit path is not a readable file, it exits 78 before any
pull or container change and names every path it looked at, so do not keep a
sibling `/root/at-run/haproxy.cfg.tmpl` next to the ops-tree copy. An
explicit path that is missing never falls back to a discovered one. (Deploy #1 on 2026-10-16 hit the old behaviour: the installed copy looked
for `/root/ops/...`, failed silently with 78 after the API color was replaced,
and rolled back.) Rendering still refuses any non-loopback, non-tailnet bind.

## Canonical NCP invocation

This is the invocation the desk runs on the NCP host, whose layout (measured on
deploy #4, 2026-10-17) is the script's default: env files in `/root/at-secrets`,
template at `/root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl`. No env override
and no template flag are needed. Run the dry-run first; it prints the resolved
env files and template (or `MISSING`) without changing anything, then the real
run prints the same three lines before it pulls.

```bash
/root/at-run/deploy-ncp-pull.sh sha-abcdef0 --dry-run
/root/at-run/deploy-ncp-pull.sh sha-abcdef0

# The same run with every input spelled out (equal to the defaults above):
AT_RUNTIME_ENV_FILE=/root/at-secrets/.env.api \
AT_SECRETS_ENV_FILE=/root/at-secrets/.env.scheduler \
  /root/at-run/deploy-ncp-pull.sh sha-abcdef0 \
  --haproxy-template /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl --dry-run
```

The dry-run should show:

```text
env file (AT_RUNTIME_ENV_FILE): /root/at-secrets/.env.api
env file (AT_SECRETS_ENV_FILE): /root/at-secrets/.env.scheduler
haproxy template (run-ops): /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl
```

| Input | Meaning |
| --- | --- |
| `AT_RUNTIME_ENV_FILE=/root/at-secrets/.env.api` | First `--env-file` for every unit. Equal to the default (`${AT_SECRETS_DIRECTORY:-/root/at-secrets}/.env.api`); an explicit value wins over the directory. |
| `AT_SECRETS_ENV_FILE=/root/at-secrets/.env.scheduler` | Second `--env-file` for every unit; its value wins for a key in both files. Equal to the default (`${AT_SECRETS_DIRECTORY:-/root/at-secrets}/.env.scheduler`). |
| `/root/at-run/deploy-ncp-pull.sh` | The installed copy from "One-time host preparation". |
| `sha-abcdef0` | Image tag to pull: the `sha-<short7>` of the merged `main` commit (or `main`). The run resolves and pins it to `repo@sha256:...`. Omit the tag and pass `--rollback` instead to return to `deployed-digest.previous`. |
| `--haproxy-template /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl` | The template in the run directory's ops tree; found without the flag (source `run-ops`) while it is the only candidate. Explicit, it can never be confused with a stray copy elsewhere. `MCP_HAPROXY_TEMPLATE=PATH` works the same, after the flag. `--haproxy-template=PATH` is accepted too. A missing value, or one starting with `-`, exits 64 instead of consuming the next option; write such a path as `./-name`. |
| `--dry-run` | Read-only plan; drop it for the real run. |

Optional additions, each described below: `--skip-kis-ws` while fillwire holds
the KIS fill stream, `MCP_UNITS_SKIP=<unit,...>` to hold fixed MCP units back,
and `AT_IMAGE_PRUNE_ENABLED=0` to skip the old-image prune for one run.

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

The script resolves and verifies the GHCR digest before pulling it. It retries the inactive
API color's loopback `/healthz` for up to 60 seconds by default. If the new API
does not return HTTP 200, the worker is not running and does not emit its
TaskIQ startup line, or either WebSocket is not running and does not emit a
`Unified WebSocket health ... connected=True` (or equivalent `connected=True`)
startup line before the bounded wait expires, it restores every unit this run
already replaced, in reverse replacement order, using each unit's prior
digest. Untouched units stay untouched. A later MCP failure uses the same
rollback path, then restores the prior HAProxy config and active colors. The
final table compares the expected digest with each container's running digest;
any mismatch leaves the command nonzero. The input tag is resolved once through
remote metadata before the gate; both `docker pull` and `docker run` receive
that `repo@sha256:...`, so a later `:main` update cannot change this deployment.
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

## Old image prune after a successful deploy

After a fully successful promotion (the digest table matched, the digest
files were rotated and the drains were armed) the script removes old
`ghcr.io/mgh3326/auto_trader` images so repeated pulls cannot fill the disk
(task 934: on 2026-09-29 a pull failed at 100% disk with 66 unused images).
It keeps:

- the digest just deployed (it must equal `deployed-digest`);
- the digest in `deployed-digest.previous` (the `--rollback` target), so a
  rollback works from the local image without the registry;
- every image used by any container in `docker ps -a`, running or stopped,
  including an `at-kis-ws` left alone by `--skip-kis-ws`, units in
  `MCP_UNITS_SKIP` and the previous API/MCP colors that are still draining.

Only images whose every tag and repo digest belongs exactly to
`ghcr.io/mgh3326/auto_trader` are candidates; postgres, redis, haproxy and any
image that also carries another repository's reference are never touched.
Removal is `docker image rm <references>` without `-f`; the script never runs
`docker system prune`, `docker image prune`, or removes volumes, networks or
containers. Build cache is not touched.

The deploy output lists each removed image ID with its references, then a
line `image prune: removed N image(s), reclaimed B bytes by image size`.
The byte figure sums image sizes; layers shared with a kept image are not
freed, so the actual disk gain can be smaller.

The prune never runs on a failed deploy, on `--rollback`, or with `--dry-run`.
A deploy dry-run instead prints `image prune: would remove ...` lines from
the current state; the real run re-evaluates after promotion. A rollback
dry-run prints `image prune: not run on rollback`.

Any prune problem is a warning on stderr only (`WARNING: image prune ...`)
and never changes the exit code or the digest table: an image that could not
be removed is named in the warning, and if `deployed-digest.previous` is
absent or invalid (it must be exactly one digest and a newline, as the
script writes it), `deployed-digest` does not record the promoted digest, or
docker's image or container listing cannot be read, the whole prune is
skipped and every image is kept.

To disable it for one run, set `AT_IMAGE_PRUNE_ENABLED=0`:

    AT_IMAGE_PRUNE_ENABLED=0 /root/at-run/deploy-ncp-pull.sh sha-abcdef0

When the variable is unset the prune runs. Any other value, including an
empty one, skips the prune with a warning. To free
space by hand while the prune is disabled, remove only unused
`ghcr.io/mgh3326/auto_trader` images that are neither of the two recorded
digests; the invariants are in `docs/contracts/task-934-image-prune.md`.

## KIS WebSocket skip flag and dry-run plan

Both flags combine with a tag or with --rollback, in any order:

    /root/at-run/deploy-ncp-pull.sh sha-abcdef0 --skip-kis-ws
    /root/at-run/deploy-ncp-pull.sh --dry-run sha-abcdef0
    /root/at-run/deploy-ncp-pull.sh --rollback --skip-kis-ws
    /root/at-run/deploy-ncp-pull.sh --dry-run --skip-kis-ws

--dry-run prints a read-only plan and enforces the exact-SHA gate above.
It reads the intended digest and revision from the registry even when no image
is cached locally, reports every unit's planned action and, when --skip-kis-ws
is set, the skip reason and retained KIS digest. Missing CI evidence exits 78
instead of printing a successful plan. Dry-run performs no docker pull, run,
rm, stop, rename, or kill and writes no HAProxy route or color files.
A rollback dry-run bypasses CI and registry inspection, reads
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
Without the flag, a stopped at-kis-ws still fails capture. Any other unit
stopped at capture also fails before mutation; if it stops after promotion,
digest verification fails and triggers the usual rollback.

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
