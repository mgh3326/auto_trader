# Dagu DAG proposal — data plane (ROB-1443)

**Proposal only. Nothing here is registered.** The operator decides whether to
copy these files into the Dagu DAGs directory on the OCI scheduler.

| file | schedule (UTC) | local meaning | health grace |
|---|---|---|---|
| `data-plane-kr.yaml` | `0 9 * * 1-5` | 18:00 KST Mon–Fri | 1 h |
| `data-plane-us.yaml` | `0 23 * * 1-5` | 19:00 EDT / 18:00 EST Mon–Fri | 1 h |
| `data-plane-crypto.yaml` | `20 0 * * *` | 00:20 UTC daily | 6 min |

The grace is chosen so that the run day's partition is already due when the
DAG starts, in summer and winter time (a test pins this): if `collect` fails
fast, the health step right after it reports `stale` the same day instead of
24 hours later.

Each DAG runs `research.data_plane.cli collect` for its market, then one
`health` step per dataset (KR/US: universe, bars_1d, bars_1d_adjusted,
corporate_actions; crypto: no adjusted bars). `collect` continues on
failure so the health steps still run and ping `<url>/fail`. Exchange holidays
need no special handling: bars are skipped on non-session days and the health
check never expects them.

Validated locally with `dagu validate` (Dagu 2.21.0) and `dagu dry`; the OCI
Dagu version was not checked (UNVERIFIED). `worker_selector: data-plane: "true"`
assumes a worker label that does not exist yet.

KR/US bars (ROB-1448): `--bar-sources kis,toss,yahoo` serves each bar partition
from the first source that covers the floor, with the public Yahoo chart as the
fallback; the manifest records `served_by` and every attempt. Broker calls are
read-only daily charts through an allowlisted view of the app's KIS/Toss
clients and take `--broker-budget kis=2/1,toss=1/1` (at most 25% of each
broker's per-key limit, shared by every collector on the same
`DATA_PLANE_ROOT` through `$DATA_PLANE_ROOT/.budget/`). At that budget a KR run
takes ~70 min and a US run ~2 h; the US collect timeout is 6 h. Details:
`docs/research/data-plane-broker-sources-inventory-2026-11-01.md`.

Worker prerequisites (operator):

* an auto_trader checkout at a reviewed commit (`AUTO_TRADER_DIR`), `uv` installed;
* `DATA_PLANE_ROOT` outside git with enough disk (US bars ≈ 11k rows/day);
* for KR/US broker bars: the NCP host that has the app's KIS/Toss credentials
  and Redis (token sharing) in the env the checkout reads; without them the
  brokers are recorded as unavailable and Yahoo serves;
* optional ping URLs `DATA_PLANE_PING_<MARKET>_<UNIVERSE|BARS|BARS_ADJUSTED|ACTIONS>`;
* outbound HTTPS to the KIS/Toss API hosts (KR/US) and query2.finance.yahoo.com, data-api.binance.vision,
  api.upbit.com, new.real.download.dws.co.kr, www.nasdaqtrader.com.

The first run of each market only writes a universe baseline plus that day's
bars and actions; bars for earlier days need a universe dated on or before them.
