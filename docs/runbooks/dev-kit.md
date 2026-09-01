# Portable development kit (D1)

This runbook starts only an isolated PostgreSQL/Timescale + Redis pair for a
throwaway local development database. It does not start a broker, worker,
scheduler, or any automatic collection job.

## 1. Scope and prerequisites

The only host installs required are Git, Docker with Compose v2, and UV. The
kit targets Docker; Podman compatibility is not part of this contract.

```bash
git clone <repository>
cd auto_trader
cp .env.dev.example .env.dev
make dev-up
make dev-seed
make dev-verify
```

`dev-up` has a 60-second default `pg_isready` deadline. `dev-seed` first runs
`alembic upgrade head`; when `dev-seed.dump` is absent it leaves a valid empty
database and exits successfully. `dev-verify` confirms the current Alembic
revision, runs only `tests/test_dev_kit.py`, starts `uvicorn app.main:app`,
waits for loopback `/healthz` to return 200, and always terminates that process.

To stop the kit without deleting its development data:

```bash
make dev-down
```

The local-only artifact `dev-seed.dump` and `.env.dev` are ignored by Git.

## 2. Namespace and port isolation

`.env.dev` must set a unique project value in this form:

```dotenv
COMPOSE_PROJECT_NAME=at-dev-alice
DEV_PG_PORT=55432
DEV_REDIS_PORT=56379
```

Do not use an `auto-trader`, production, or staging project name. The Compose
file deliberately has no `container_name` values, so Docker prefixes its
containers, volumes, and network with `at-dev-alice`. Both published ports bind
to `127.0.0.1`; they are never exposed on a host interface by this kit.

The Make targets explicitly pass the selected project and `DEV_*` values to
Compose, so a similarly named variable exported by an interactive shell cannot
silently replace this kit's namespace or port mapping.

For two checkouts on one host, give each a project name and a different port
pair. The Make targets derive their host-side database and Redis URLs from the
same `DEV_*` variables.

```bash
cp .env.dev.example .env.dev.alice
cp .env.dev.example .env.dev.bob
# Edit the two files, for example: at-dev-alice/55432/56379 and
# at-dev-bob/55433/56380.
make DEV_ENV_FILE=.env.dev.alice dev-up
make DEV_ENV_FILE=.env.dev.bob dev-up
```

Validate a rendered configuration without creating containers:

```bash
make dev-config
```

## 3. Image and architecture record

The database image is pinned to `timescale/timescaledb:2.22.1-pg17`, not the
production `timescaledb-ha` image. It is chosen for a plain PostgreSQL 17
Timescale development database with Linux `amd64` and `arm64` manifest entries.
The extension does not need to exactly match production's 2.26.3; the
acceptance criterion is that this repository's Alembic head completes against
the pulled image (the migrations require TimescaleDB 2.15.0 or newer).

Registry manifests are mutable external metadata, so the operator witnessing a
real startup must capture this check immediately before the pull and attach its
output to the handoff report:

```bash
docker buildx imagetools inspect timescale/timescaledb:2.22.1-pg17
```

The result must list both `linux/amd64` and `linux/arm64`. The Redis image is
also the official multi-architecture `redis:7.4-alpine` tag. Do not substitute
the production HA tag merely to make image versions match.

The Compose services have explicit memory ceilings: 768 MiB for Timescale and
192 MiB for Redis. These are development safety bounds, not production sizing.

## 4. Optional bounded seed artifact

An operator may prepare a local, Git-ignored `dev-seed.dump` from a
read-only production database role:

```bash
uv run python scripts/make_dev_seed.py \
  --source-database-url "$READ_ONLY_DATABASE_URL" \
  --top-n 50 \
  --days 60
```

The exporter refuses a non-PostgreSQL URL, opens a `readonly=True` transaction
with `default_transaction_read_only=on`, and makes only `SELECT` plus
PostgreSQL COPY OUT calls against the source. It emits a plain SQL restore
artifact (mode `0600`) containing, in dependency order:

- only the selected KR/US sector rows and the top-N active KR, US, and Upbit
  symbol universes;
- up to the requested recent-day window of KR and US daily candles for those
  selected symbols; and
- a SHA-256 stamp of the repository-owned `config/trading_policy.yaml`.

The policy document itself stays under Git as the authoritative source; no
account, credential, order ledger, fill evidence, Telegram, or user table is
exported. The generated artifact contains `COPY ... FROM STDIN` instructions
only for the *local development restore*. It never sends such a command to the
source database.

`make dev-seed` restores the artifact only after Alembic head. It is intended
for a newly created development namespace. If a stale namespace already has
conflicting seed rows, create a new unique `at-dev-<suffix>` namespace rather
than applying a production-derived artifact over it.

## 5. Safety defaults

`.env.dev.example` pins every broker credential field to a
`DEV_PLACEHOLDER_*` sentinel and sets every `*_ENABLED` value in the template
to `false`. The static guard in `tests/test_dev_kit.py` fails if a listed broker
credential stops using that sentinel, and its mutant test demonstrates that a
real-looking key pattern is rejected. It also verifies that there is no
`container_name`, that both host ports are parameterized, and that the seed
exporter has no source-side write path.

The Make targets start their `uv` commands with a minimal process environment
(`env -i` with only `PATH`, `TMPDIR`, the selected dev env file, and derived
local database/Redis URLs). A real credential or enabled gate exported by a
developer shell therefore cannot override `.env.dev` during `dev-seed` or
`dev-verify`.

Keep these defaults intact. This kit has no broker activation path, no scheduler
registration, and no external HTTP in its seed/verify workflow; the verify
probe is fixed to `127.0.0.1`.

## 6. Host preparation matrix

| Host | Minimum preparation | Development-kit rule | Notes |
| --- | --- | --- | --- |
| Personal Mac (arm64 or x86_64) | Git, UV, Docker Desktop with Compose v2 | Use a unique `at-dev-<suffix>` and loopback ports. | Confirm the manifest has the matching `linux/arm64` or `linux/amd64` entry before the first pull. |
| Company-managed Mac | Git, UV, company-approved Docker Desktop/Compose v2 | Keep `.env.dev` local; do not use corporate production/staging names or ports. | Request Docker Desktop entitlement/proxy access through the company process; no additional language runtime is needed. |
| Ubuntu x86 desktop | Git, UV, Docker Engine + Compose plugin | Use the Docker group/rootless setup approved for that host, then run the same Make targets. | Do not bind `55432`/`56379` beyond loopback. |
| Raspberry Pi arm64 | 64-bit Raspberry Pi OS/Ubuntu, Git, UV, Docker Engine + Compose plugin | Keep the default caps or lower them only after an operator review; use a dedicated suffix. | 2 GiB or more host RAM is recommended because Docker itself needs headroom beyond the 960 MiB service caps. |
| NCP / OCI host shared with staging or production | Git, UV, Docker Engine + Compose plugin | A unique project suffix and unique loopback port pair are mandatory; never attach this project to a production Compose network or volume. | Run `make dev-config` before `dev-up`, and document the exact project and port pair in the host change record. |

## 7. Witness checklist (fable)

The fable witness performs the real-runtime acceptance that cannot be performed
in a Docker-less coding environment:

1. Capture `docker buildx imagetools inspect` for the pinned Timescale image;
   verify both architectures.
2. On a Mac, run `make dev-up && make dev-seed && make dev-verify`; record the
   Compose project, ports, Timescale extension version, Alembic output, and
   `/healthz` result.
3. On a desktop Linux or company Mac, follow the matrix preparation path and
   run at least `make dev-config` before the scheduled full smoke.
4. For a coexistence host, render two unique projects and confirm their names,
   volumes, networks, and host-port mappings do not overlap.
