# Dagu DAG proposal — data plane (ROB-1443)

**Proposal only. Nothing here is registered.** The operator decides whether to
copy these files into the Dagu DAGs directory on the OCI scheduler.

| file | schedule (UTC) | local meaning | health grace |
|---|---|---|---|
| `data-plane-kr.yaml` | `0 9 * * 1-5` | 18:00 KST Mon–Fri | 2 h |
| `data-plane-us.yaml` | `0 23 * * 1-5` | 19:00 EDT / 18:00 EST Mon–Fri | 2 h |
| `data-plane-crypto.yaml` | `20 0 * * *` | 00:20 UTC daily | 15 min |

Each DAG runs `research.data_plane.cli collect` for its market, then three
`health` steps (universe, bars_1d, corporate_actions). `collect` continues on
failure so the health steps still run and ping `<url>/fail`. Exchange holidays
need no special handling: bars are skipped on non-session days and the health
check never expects them.

Validated locally with `dagu validate` (Dagu 2.21.0) and `dagu dry`; the OCI
Dagu version was not checked (UNVERIFIED). `worker_selector: data-plane: "true"`
assumes a worker label that does not exist yet.

Worker prerequisites (operator):

* an auto_trader checkout at a reviewed commit (`AUTO_TRADER_DIR`), `uv` installed;
* `DATA_PLANE_ROOT` outside git with enough disk (US bars ≈ 11k rows/day);
* optional ping URLs `DATA_PLANE_PING_<MARKET>_<UNIVERSE|BARS|ACTIONS>`;
* outbound HTTPS to query2.finance.yahoo.com, data-api.binance.vision,
  api.upbit.com, new.real.download.dws.co.kr, www.nasdaqtrader.com.

The first run of each market only writes a universe baseline plus that day's
bars and actions; bars for earlier days need a universe dated on or before them.
