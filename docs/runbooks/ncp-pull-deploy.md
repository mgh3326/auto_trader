# NCP GHCR pull deployment

The NCP host deploys a previously built image; it does not build from a source
checkout. `scripts/deploy-ncp-pull.sh` pulls one GHCR tag, recreates `at-api`,
`at-scheduler`, `at-worker`, `at-upbit-ws`, and `at-kis-ws` from that same
repo-digest reference. It verifies the API's loopback `/healthz`, bounded
TaskIQ worker startup, and bounded WebSocket connection logs before recording
the deployed digest. The same promotion also advances the private MCP fleet:
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

The script requires existing `at-api` and `at-scheduler` containers before it
will replace any unit. This intentional preflight ensures a failed readiness
check has a concrete previous image to restore. The first promotion may lack
`at-worker` or `at-kis-ws`; their rollback references bootstrap from the API
image. A hand-started `at-upbit-ws` using a local/non-digest tag (for example
`auto_trader:ncp-main`) also bootstraps from the API image instead of retaining
that mutable tag.

## Promote an image

Run only on the NCP host:

```bash
/root/at-run/deploy-ncp-pull.sh
/root/at-run/deploy-ncp-pull.sh sha-abcdef0
```

All five units use `--network host`, the two existing `--env-file` arguments,
and `--restart unless-stopped`. The image now copies `research_contracts/` and
`config/`, so this deployment deliberately has no bind mounts for either path.
`at-scheduler` runs the image's TaskIQ scheduler command; `at-api` uses the
image's default API command; `at-worker` runs
`/app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1`.
`at-upbit-ws` and `at-kis-ws` respectively run
`/app/.venv/bin/python websocket_monitor.py --mode upbit` and
`/app/.venv/bin/python websocket_monitor.py --mode kis`.

The MCP default profile is promoted blue/green behind private HAProxy while
the previous color drains. Fixed MCP profiles are recreated from the same
digest with profile-scoped environment policy (including the required
approval-hash modes for TradingCodex execution). HAProxy must remain bound only
to loopback and the configured tailnet address; it is never a public listener.

The script prints the GHCR repo digest after pulling it. It retries
`http://127.0.0.1:8000/healthz` for up to 60 seconds by default. If the new API
does not return HTTP 200, the worker is not running and does not emit its
TaskIQ startup line, or either WebSocket is not running and does not emit a
`Unified WebSocket health ... connected=True` (or equivalent `connected=True`)
startup line before the bounded wait expires, it recreates all five units with
pinned rollback references and verifies the restored API, worker, and both
WebSockets. A failed MCP promotion restores the MCP fleet's captured image
state and active color before the same core rollback. A tag is used only to pull and
resolve the image; `docker run` always receives `repo@sha256:...`, so the next
deployment's `.Config.Image` is stable even after a later `:main` pull.

The script maintains these operator-owned, mode-0600 digest files:

- `/root/at-run/deployed-digest` is the currently healthy deployment.
- `/root/at-run/deployed-digest.previous` is the prior healthy deployment;
  each successful deployment atomically rotates the former current value here.

For an automatic readiness rollback, a container's existing digest reference
takes precedence. A legacy floating-tag API/scheduler container instead uses
`deployed-digest` for this first transition. A missing worker/KIS WebSocket or
local-tagged Upbit WebSocket uses the API image as its bootstrap reference;
that API reference is still validated as a repo digest (or resolved through
`deployed-digest`). If neither route provides a valid repo digest, the script
fails explicitly before replacing containers; it never silently restarts a
floating tag. A successful automatic rollback restores `deployed-digest` to
the recovered digest.

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

## Worker operation notes

`at-worker`, `at-upbit-ws`, and `at-kis-ws` are NCP-only trading roles. Their
Mac launchd plists are intentionally absent: re-enabling a Mac worker would
create competing TaskIQ consumers, and re-enabling either WebSocket would
duplicate its monitor. The KIS rate limiter remains process-local, so observe
KIS 429s while diagnosing any out-of-band process. For Toss, use
`TOSS_RATE_LIMITER_BACKEND=redis` on every intentionally active worker
environment as recommended by #2004.
