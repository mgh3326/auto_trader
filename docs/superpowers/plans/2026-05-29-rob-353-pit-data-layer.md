# ROB-353 PR1 — PIT data layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durableize the ROB-349 Binance USD-M point-in-time (PIT) universe + klines prototype into reusable, tested repo modules so ROB-353 PR2 can run families 1–3 through the committed funnel on real, survivorship-safe data.

**Architecture:** Four focused modules under `research/nautilus_scalping/` — a public-data klines fetcher, an extended PIT manifest (with `strict_usdt_perp` filter), a ported index builder, and a PIT-trimmed `families.Bar` loader — plus one committed metadata-only manifest artifact (`pit_universe.v1.json`) pinned by a snapshot hash. The bridge to `campaign.run_campaign` and the actual RUN are PR2.

**Tech Stack:** Python 3.13 (repo `.python-version`), pure stdlib + the isolated `research/nautilus_scalping/.venv`, pytest. No `app.*` imports (research-only boundary, ROB-339). Run everything with `uv run --no-project python ...` / `uv run --no-project pytest ...` from inside `research/nautilus_scalping/`.

**Spec:** `docs/superpowers/specs/2026-05-29-rob-353-pit-data-layer-design.md`

**Working dir for ALL commands below:** `/Users/mgh3326/work/auto_trader.rob-353/research/nautilus_scalping` (branch `rob-353`). Tests import modules flat (e.g. `import pit_universe`), matching the existing `conftest.py`.

---

## File structure

| File | Responsibility | Create/Modify |
|------|----------------|---------------|
| `artifact_paths.py` | add `pit_data_root()` — gitignored raw-data root, `os.environ` only | Modify |
| `pit_universe.py` | extend `SymbolListing`/`PITManifest` with ROB-349 fields; `from_pit_index_records`; `strict_usdt_perp` | Modify |
| `build_pit_universe.py` | ported ROB-349 index builder (read-only public data) + snapshot hash; RUN operator-gated | Create |
| `pit_klines_fetcher.py` | download `{1d,1h}` USD-M klines from data.binance.vision (public, no keys) | Create |
| `pit_bars.py` | load klines CSV → PIT-trimmed `families.Bar`; panel accessor for XS families | Create |
| `data_manifests/pit_universe.v1.json` | committed metadata-only manifest (843 symbols, canonical PITManifest format) | Create (committed) |
| `data_manifests/pit_universe.v1.meta.json` | snapshot hash + build provenance | Create (committed) |
| `tests/test_pit_universe.py` | extend: new fields, conversion, strict filter, committed-manifest load + hash | Modify |
| `tests/test_pit_klines_fetcher.py` | URL build, daily/monthly path, 404 tolerance, no-secrets | Create |
| `tests/test_pit_bars.py` | trimming + Bar mapping + panel alignment | Create |
| `tests/test_pit_data_layer_guard.py` | new modules import no `app.*`; raw-data root gitignored | Create |
| `docs/runbooks/rob-353-pit-data-layer.md` | data/universe definition runbook (feeds PR2 report) | Create |

**Units / contracts in this PR:**
- `artifact_paths.pit_data_root() -> Path`
- `pit_universe.SymbolListing(symbol, listed_from, delisted_at=None, status=None, kline_coverage=None, funding_coverage=None, confidence=None, missing_data_reason=None)`
- `pit_universe._date_to_epoch_ms(date_str: str) -> int`
- `pit_universe.PITManifest.from_pit_index_records(rows: list[dict]) -> PITManifest`
- `pit_universe.PITManifest.strict_usdt_perp() -> PITManifest`
- `pit_universe.PITManifest.snapshot_hash() -> str`
- `pit_klines_fetcher.kline_url(symbol, interval, year, month, market="um", cadence="monthly") -> str`
- `pit_bars.load_bars(symbol, interval, manifest, root=None) -> list[families.Bar]`
- `pit_bars.load_panel(symbols, interval, manifest, root=None) -> dict[str, list[tuple[int, float]]]`

---

## Task 0: Baseline — confirm green starting point

**Files:** none (verification only)

- [ ] **Step 1: Confirm self-test passes and record config hash**

Run: `uv run --no-project python run_rob351_campaign.py --self-test | grep -E "schema_version|config_hash"`
Expected: prints `rob351_campaign.v1` and `config_hash 8f02dffd51dc5bedf5ab4c1521edb2185f4768304b5b60fa7dd0836ef8872adf`.

- [ ] **Step 2: Confirm existing tests pass**

Run: `uv run --no-project pytest tests/test_pit_universe.py tests/test_panel.py -q`
Expected: PASS (no failures). This is the regression baseline for Tasks 2–4.

---

## Task 1: `pit_data_root()` — gitignored raw-data root helper

**Files:**
- Modify: `artifact_paths.py`
- Test: `tests/test_artifact_paths.py` (create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifact_paths.py
import importlib
from pathlib import Path

import artifact_paths


def test_pit_data_root_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", raising=False)
    importlib.reload(artifact_paths)
    root = artifact_paths.pit_data_root()
    assert root.name == "data"
    assert root.parent.name == "nautilus_scalping"


def test_pit_data_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", str(tmp_path))
    root = artifact_paths.pit_data_root()
    assert root == tmp_path / "data"


def test_pit_data_root_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", "   ")
    root = artifact_paths.pit_data_root()
    assert root.name == "data" and root.parent.name == "nautilus_scalping"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_artifact_paths.py -q`
Expected: FAIL — `AttributeError: module 'artifact_paths' has no attribute 'pit_data_root'`.

- [ ] **Step 3: Implement `pit_data_root()`**

Append to `artifact_paths.py` (after `resolve_artifact_path`):

```python
def pit_data_root() -> Path:
    """Raw-data root for downloaded klines (gitignored). Distinct from
    ``resolve_artifact_path`` (citable discovery/gate outputs). Env if set
    (non-blank), else repo-internal ``data/`` (matched by ``.gitignore``)."""
    raw = os.environ.get(ENV_VAR)
    base = Path(raw.strip()) if raw is not None and raw.strip() else Path(__file__).resolve().parent
    return base / "data"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_artifact_paths.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add artifact_paths.py tests/test_artifact_paths.py
git commit -m "feat(ROB-353): pit_data_root() gitignored raw-data root helper"
```

---

## Task 2: Extend `SymbolListing` / `PITManifest` with ROB-349 metadata fields

**Files:**
- Modify: `pit_universe.py:26-73`
- Test: `tests/test_pit_universe.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pit_universe.py`:

```python
def test_symbollisting_optional_metadata_roundtrips():
    rec = {
        "symbol": "EOSUSDT", "listed_from": 1672531200000, "delisted_at": 1700000000000,
        "status": "dead", "kline_coverage": 1.0, "funding_coverage": 1.0,
        "confidence": "high", "missing_data_reason": "delisted",
    }
    m = pit_universe.PITManifest.from_records([rec])
    (only,) = m.listings
    assert only.status == "dead"
    assert only.kline_coverage == 1.0
    assert only.confidence == "high"
    # round-trip preserves the optional fields
    back = pit_universe.PITManifest.from_records(m.to_records())
    assert back.listings[0].missing_data_reason == "delisted"


def test_symbollisting_metadata_defaults_none():
    m = pit_universe.PITManifest.from_records(
        [{"symbol": "BTCUSDT", "listed_from": 0, "delisted_at": None}]
    )
    only = m.listings[0]
    assert only.status is None and only.confidence is None
    # legacy round-trip must NOT emit None metadata noise it can't read back
    assert pit_universe.PITManifest.from_records(m.to_records()).listings[0].symbol == "BTCUSDT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k metadata`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'status'`.

- [ ] **Step 3: Implement the extension**

Replace the `SymbolListing` dataclass and the `from_records`/`to_records` methods in `pit_universe.py`:

```python
from typing import Literal

_META_FIELDS = ("status", "kline_coverage", "funding_coverage", "confidence", "missing_data_reason")


@dataclass(frozen=True)
class SymbolListing:
    symbol: str
    listed_from: int
    delisted_at: int | None = None
    # ROB-349 metadata (optional; None for hand-built/synthetic manifests)
    status: Literal["live", "settling", "dead"] | None = None
    kline_coverage: float | None = None
    funding_coverage: float | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    missing_data_reason: str | None = None

    def validate(self) -> "SymbolListing":
        if self.delisted_at is not None and self.delisted_at < self.listed_from:
            raise ValueError(
                f"{self.symbol}: delisted_at {self.delisted_at} < listed_from {self.listed_from}"
            )
        return self

    def tradeable_at(self, ts: int, min_seasoning: int = 0) -> bool:
        if ts < self.listed_from + min_seasoning:
            return False
        if self.delisted_at is not None and ts >= self.delisted_at:
            return False
        return True
```

Update `from_records` to read the optional fields, and `to_records` to emit only those present:

```python
    @classmethod
    def from_records(cls, records: list[dict]) -> "PITManifest":
        listings = tuple(
            SymbolListing(
                symbol=r["symbol"],
                listed_from=int(r["listed_from"]),
                delisted_at=None if r.get("delisted_at") is None else int(r["delisted_at"]),
                status=r.get("status"),
                kline_coverage=r.get("kline_coverage"),
                funding_coverage=r.get("funding_coverage"),
                confidence=r.get("confidence"),
                missing_data_reason=r.get("missing_data_reason"),
            ).validate()
            for r in records
        )
        seen: set[str] = set()
        for listing in listings:
            if listing.symbol in seen:
                raise ValueError(f"duplicate symbol in manifest: {listing.symbol}")
            seen.add(listing.symbol)
        return cls(listings=listings)

    def to_records(self) -> list[dict]:
        out = []
        for x in self.listings:
            rec = {"symbol": x.symbol, "listed_from": x.listed_from, "delisted_at": x.delisted_at}
            for f in _META_FIELDS:
                v = getattr(x, f)
                if v is not None:
                    rec[f] = v
            out.append(rec)
        return out
```

- [ ] **Step 4: Run tests to verify they pass (incl. regression)**

Run: `uv run --no-project pytest tests/test_pit_universe.py tests/test_panel.py -q`
Expected: PASS (all existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add pit_universe.py tests/test_pit_universe.py
git commit -m "feat(ROB-353): extend PIT manifest with ROB-349 status/coverage/confidence fields"
```

---

## Task 3: `from_pit_index_records` + `_date_to_epoch_ms` (ROB-349 row → SymbolListing)

**Files:**
- Modify: `pit_universe.py`
- Test: `tests/test_pit_universe.py`

The ROB-349 index emits rows with day-precise `active_from`/`active_to` date strings (and `"ongoing"` for live). Convert to the epoch-ms `listed_from`/`delisted_at` contract: `listed_from = midnight-UTC(active_from)`; `delisted_at = midnight-UTC(active_to + 1 day)` (exclusive, keeps the last active day tradeable); `"ongoing"`/None → `delisted_at=None`. Rows with no usable `active_from` (fall back to `first_seen` month's day 1) are kept; rows with neither are skipped.

- [ ] **Step 1: Write the failing test**

```python
def test_from_pit_index_records_maps_dates_to_epoch_ms():
    rows = [
        {"symbol": "EOSUSDT", "status": "dead", "first_seen": "2023-01", "last_seen": "2024-01",
         "active_from": "2023-01-26", "active_to": "2024-01-11",
         "kline_coverage": 1.0, "funding_coverage": 1.0, "confidence": "high",
         "missing_data_reason": "delisted"},
        {"symbol": "BTCUSDT", "status": "live", "first_seen": "2020-01", "last_seen": "2026-05",
         "active_from": "2020-01-01", "active_to": "ongoing",
         "kline_coverage": 1.0, "funding_coverage": 1.0, "confidence": "high",
         "missing_data_reason": ""},
    ]
    m = pit_universe.PITManifest.from_pit_index_records(rows)
    eos = next(x for x in m.listings if x.symbol == "EOSUSDT")
    btc = next(x for x in m.listings if x.symbol == "BTCUSDT")
    assert eos.listed_from == pit_universe._date_to_epoch_ms("2023-01-26")
    # delisted_at is exclusive: last active day (2024-01-11) + 1 day
    assert eos.delisted_at == pit_universe._date_to_epoch_ms("2024-01-12")
    assert btc.delisted_at is None
    # EOS tradeable on its last active day, not the day after
    assert eos.tradeable_at(pit_universe._date_to_epoch_ms("2024-01-11"))
    assert not eos.tradeable_at(pit_universe._date_to_epoch_ms("2024-01-12"))


def test_from_pit_index_records_skips_rows_without_dates():
    rows = [{"symbol": "GHOSTUSDT", "status": "dead", "first_seen": None, "last_seen": None,
             "active_from": None, "active_to": None}]
    m = pit_universe.PITManifest.from_pit_index_records(rows)
    assert m.listings == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k from_pit_index`
Expected: FAIL — `AttributeError: ... has no attribute 'from_pit_index_records'`.

- [ ] **Step 3: Implement conversion**

Add imports + helper + classmethod to `pit_universe.py`:

```python
from datetime import datetime, timedelta, timezone


def _date_to_epoch_ms(date_str: str) -> int:
    """Midnight-UTC epoch ms for ``YYYY-MM-DD``."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _month_first_day(month_str: str) -> str:
    """``YYYY-MM`` -> ``YYYY-MM-01``."""
    return f"{month_str}-01"
```

```python
    @classmethod
    def from_pit_index_records(cls, rows: list[dict]) -> "PITManifest":
        """Build from ROB-349 index rows (day-precise active_from/active_to)."""
        recs: list[dict] = []
        for r in rows:
            af = r.get("active_from") or (
                _month_first_day(r["first_seen"]) if r.get("first_seen") else None
            )
            if not af:
                continue  # cannot place this symbol in time — skip
            listed_from = _date_to_epoch_ms(af)
            at = r.get("active_to")
            if at in (None, "ongoing"):
                delisted_at = None
            else:
                delisted_at = _date_to_epoch_ms(
                    (datetime.strptime(at, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                )
            recs.append({
                "symbol": r["symbol"], "listed_from": listed_from, "delisted_at": delisted_at,
                "status": r.get("status"), "kline_coverage": r.get("kline_coverage"),
                "funding_coverage": r.get("funding_coverage"), "confidence": r.get("confidence"),
                "missing_data_reason": r.get("missing_data_reason") or None,
            })
        return cls.from_records(recs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k from_pit_index`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add pit_universe.py tests/test_pit_universe.py
git commit -m "feat(ROB-353): map ROB-349 index rows to epoch-ms PIT listings"
```

---

## Task 4: `strict_usdt_perp()` filter + `snapshot_hash()`

**Files:**
- Modify: `pit_universe.py`
- Test: `tests/test_pit_universe.py`

Keep only `status ∈ {live, dead}` plain `*USDT` perps; drop `settling`, BUSD/USDC-quoted, dated/quarterly (symbols with `_`), and `*SETTLED`. `snapshot_hash` is a stable sha256 over canonical records (for the committed-manifest pin).

- [ ] **Step 1: Write the failing test**

```python
import hashlib, json


def _row(sym, status):
    return {"symbol": sym, "status": status, "active_from": "2023-01-01", "active_to": "2024-01-01"}


def test_strict_usdt_perp_keeps_live_and_dead_plain_usdt():
    rows = [
        _row("BTCUSDT", "live"), _row("EOSUSDT", "dead"),
        _row("ETHUSDC", "live"),               # wrong quote
        _row("BTCBUSD", "dead"),               # BUSD
        _row("BTCUSDT_230331", "settling"),    # dated/quarterly
        _row("XRPUSDT", "settling"),           # settling perp -> excluded
        _row("FOOUSDT-SETTLED", "dead"),       # settled marker
    ]
    m = pit_universe.PITManifest.from_pit_index_records(rows).strict_usdt_perp()
    kept = {x.symbol for x in m.listings}
    assert kept == {"BTCUSDT", "EOSUSDT"}


def test_snapshot_hash_is_stable_and_order_independent():
    a = pit_universe.PITManifest.from_records([
        {"symbol": "A", "listed_from": 1, "delisted_at": None},
        {"symbol": "B", "listed_from": 2, "delisted_at": 3},
    ])
    b = pit_universe.PITManifest.from_records([
        {"symbol": "B", "listed_from": 2, "delisted_at": 3},
        {"symbol": "A", "listed_from": 1, "delisted_at": None},
    ])
    assert a.snapshot_hash() == b.snapshot_hash()
    assert len(a.snapshot_hash()) == 64
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k "strict or snapshot"`
Expected: FAIL — `AttributeError: ... 'strict_usdt_perp'`.

- [ ] **Step 3: Implement filter + hash**

Add `import hashlib` and `import json` (json already imported) to `pit_universe.py`, then add methods to `PITManifest`:

```python
    def strict_usdt_perp(self) -> "PITManifest":
        """Perp-only honest universe: live/dead plain *USDT, excl. settling/BUSD/USDC/dated/SETTLED."""
        kept = tuple(
            x for x in self.listings
            if x.status in ("live", "dead")
            and x.symbol.endswith("USDT")
            and "_" not in x.symbol
            and "SETTLED" not in x.symbol
        )
        return PITManifest(listings=kept)

    def snapshot_hash(self) -> str:
        """Stable sha256 over canonical (order-independent) records — pins a committed manifest."""
        canon = json.dumps(sorted(self.to_records(), key=lambda r: r["symbol"]),
                            sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add pit_universe.py tests/test_pit_universe.py
git commit -m "feat(ROB-353): strict_usdt_perp filter + stable snapshot_hash"
```

---

## Task 5: Commit the v1 metadata manifest + meta sidecar (one-shot conversion)

**Files:**
- Create (committed): `data_manifests/pit_universe.v1.json`, `data_manifests/pit_universe.v1.meta.json`
- Test: `tests/test_pit_universe.py`

**Precondition:** the ROB-349 source `/tmp/factor_research/pit_universe.json` (843 records) exists. If absent, regenerate it via the ported builder (Task 6) first, or stop with a `data-precondition` blocker — do not fabricate.

- [ ] **Step 1: Convert + write the committed artifacts (one-shot script, run once)**

Run this exact snippet from the research dir:

```bash
uv run --no-project python - <<'PY'
import json, datetime
import pit_universe as pu
rows = json.load(open("/tmp/factor_research/pit_universe.json"))
m = pu.PITManifest.from_pit_index_records(rows)
import os
os.makedirs("data_manifests", exist_ok=True)
m.save("data_manifests/pit_universe.v1.json")
meta = {
    "schema_version": "pit_universe.v1",
    "snapshot_hash": m.snapshot_hash(),
    "symbol_count": len(m.listings),
    "source": "data.binance.vision/futures/um (ROB-349 build_pit_universe.py)",
    "source_records": len(rows),
    "build_window": "2020-01..2026-05 (archived monthly klines coverage)",
    "note": "Metadata only — no raw OHLCV. Day-precise active_from/active_to mapped to "
            "epoch-ms listed_from/delisted_at (exclusive). strict_usdt_perp() applied at use.",
}
json.dump(meta, open("data_manifests/pit_universe.v1.meta.json", "w"), indent=2)
print("wrote", len(m.listings), "listings; hash", m.snapshot_hash())
PY
```

Expected: prints `wrote 843 listings; hash <64-hex>` (count may differ slightly if rows lack dates — that is fine and recorded in meta as `source_records` vs `symbol_count`).

- [ ] **Step 2: Write the failing test (committed manifest loads and matches its pinned hash)**

```python
import json
from pathlib import Path

_MANIFEST = Path(__file__).resolve().parents[1] / "data_manifests" / "pit_universe.v1.json"
_META = Path(__file__).resolve().parents[1] / "data_manifests" / "pit_universe.v1.meta.json"


def test_committed_manifest_loads_and_hash_matches_meta():
    m = pit_universe.PITManifest.load(_MANIFEST)
    meta = json.loads(_META.read_text())
    assert len(m.listings) == meta["symbol_count"]
    assert m.snapshot_hash() == meta["snapshot_hash"]  # pin: edits must update meta


def test_committed_manifest_has_a_usable_perp_universe():
    m = pit_universe.PITManifest.load(_MANIFEST).strict_usdt_perp()
    # ROB-349 reported a materially larger perp universe than the 23-symbol survivor panel
    assert len(m.listings) > 100
    # the 16 survivorship-relevant dead perps must survive the strict filter
    syms = {x.symbol for x in m.listings}
    assert {"EOSUSDT", "GALUSDT", "HNTUSDT"}.issubset(syms)
```

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k "committed_manifest"`
Expected: PASS (2 passed). If `test_committed_manifest_has_a_usable_perp_universe` fails on the specific symbols, inspect the manifest (`status`/symbol spelling) and adjust the asserted set to three confirmed dead-perp symbols from `pit_universe.v1.json` — do not weaken the `>100` check.

- [ ] **Step 4: Confirm the raw klines are NOT staged, only metadata**

Run: `git status --porcelain data_manifests/ && git check-ignore data/ || true`
Expected: only `data_manifests/pit_universe.v1.json` and `.meta.json` show as new; `data/` is ignored.

- [ ] **Step 5: Commit**

```bash
git add data_manifests/pit_universe.v1.json data_manifests/pit_universe.v1.meta.json tests/test_pit_universe.py
git commit -m "feat(ROB-353): commit metadata-only PIT universe v1 manifest (843 symbols, hash-pinned)"
```

---

## Task 6: Port `build_pit_universe.py` into the repo (RUN operator-gated)

**Files:**
- Create: `build_pit_universe.py`
- Test: `tests/test_pit_universe.py` (pure helpers only — no network)

Port the ROB-349 builder verbatim in logic, with three changes: (1) write outputs under `data_manifests/` via a `--out` arg (default the v1 paths), (2) emit the `.meta.json` sidecar with `PITManifest(...).snapshot_hash()`, (3) make `expected_months` importable and tested. The network RUN stays operator-gated (needs `/tmp/pit_audit_exchangeinfo.json` + outbound S3); CI tests only the pure helper.

- [ ] **Step 1: Write the failing test (pure helper)**

```python
def test_expected_months_inclusive_span():
    import build_pit_universe as b
    assert b.expected_months("2023-01", "2023-01") == 1
    assert b.expected_months("2023-01", "2023-12") == 12
    assert b.expected_months("2023-11", "2024-02") == 4
    assert b.expected_months(None, "2024-02") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k expected_months`
Expected: FAIL — `ModuleNotFoundError: No module named 'build_pit_universe'`.

- [ ] **Step 3: Create `build_pit_universe.py`**

Copy `/tmp/factor_research/build_pit_universe.py` into `research/nautilus_scalping/build_pit_universe.py` with this header and the output/CLI changes. Keep `list_symbols`, `months_listed`, `expected_months`, `boundary_active`, `classify`, `build_row` byte-for-byte except output paths. Add at the top:

```python
#!/usr/bin/env python3
"""ROB-353 (PR1) — ported ROB-349 PIT Binance USD-M universe index builder.

Read-only PUBLIC data only (data.binance.vision S3 listing + monthly 1d klines for
non-live boundary detection) + a local fapi exchangeInfo dump. Emits the metadata-only
manifest (symbol + listing window + coverage/confidence) and a snapshot-hash sidecar.
NO raw OHLCV persisted. No keys, no orders, no scheduler. The network RUN is operator-
gated; CI exercises only the pure helpers.

Usage (operator):
    # 1) save exchangeInfo once (public):
    #    curl -s https://fapi.binance.com/fapi/v1/exchangeInfo > /tmp/pit_audit_exchangeinfo.json
    # 2) build:
    uv run --no-project python build_pit_universe.py \\
        --exchange-info /tmp/pit_audit_exchangeinfo.json \\
        --out data_manifests/pit_universe.v1.json
"""
```

Replace the hardcoded output tail of the original (the `csv.DictWriter` / `json.dump` block) with:

```python
def write_outputs(rows: list[dict], out_json: str) -> str:
    import pit_universe as pu
    m = pu.PITManifest.from_pit_index_records(rows)
    m.save(out_json)
    meta = {
        "schema_version": "pit_universe.v1",
        "snapshot_hash": m.snapshot_hash(),
        "symbol_count": len(m.listings),
        "source": "data.binance.vision/futures/um",
        "source_records": len(rows),
    }
    meta_path = out_json.replace(".json", ".meta.json")
    json.dump(meta, open(meta_path, "w"), indent=2)
    return m.snapshot_hash()
```

Wrap the gather/build in `def main(argv=None)` with `argparse` (`--exchange-info`, default `/tmp/pit_audit_exchangeinfo.json`; `--out`, default `data_manifests/pit_universe.v1.json`), reading exchangeInfo from the arg instead of the hardcoded path, then call `write_outputs(rows, args.out)`. Guard with `if __name__ == "__main__": sys.exit(main())`. The `__main__` path must be the ONLY place that does network I/O, so importing the module for tests triggers no requests.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_pit_universe.py -q -k expected_months`
Expected: PASS. Also confirm import is side-effect free: `uv run --no-project python -c "import build_pit_universe"` returns instantly with no network.

- [ ] **Step 5: Commit**

```bash
git add build_pit_universe.py tests/test_pit_universe.py
git commit -m "feat(ROB-353): port ROB-349 PIT universe builder (operator-gated RUN, snapshot-hash output)"
```

---

## Task 7: `pit_klines_fetcher.py` — public USD-M klines downloader (1d + 1h)

**Files:**
- Create: `pit_klines_fetcher.py`
- Test: `tests/test_pit_klines_fetcher.py`

Mirror `fetch_agg_trades.py` (stdlib, CHECKSUM verify, 404-tolerant). Klines live under `futures/um/{monthly|daily}/klines/<SYM>/<interval>/`. The testable pure surface is URL construction + interval validation; network download reuses the proven `_download`/`_verify` pattern and writes under `pit_data_root()`.

- [ ] **Step 1: Write the failing test (no network)**

```python
# tests/test_pit_klines_fetcher.py
import pytest

import pit_klines_fetcher as f


def test_kline_url_monthly_um():
    url = f.kline_url("EOSUSDT", "1d", 2024, 1, market="um", cadence="monthly")
    assert url == ("https://data.binance.vision/data/futures/um/monthly/klines/"
                   "EOSUSDT/1d/EOSUSDT-1d-2024-01.zip")


def test_kline_url_daily_1h_zero_pads():
    url = f.kline_url("BTCUSDT", "1h", 2026, 3, market="um", cadence="daily", day=5)
    assert url.endswith("futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-03-05.zip")


def test_kline_url_rejects_unknown_interval():
    with pytest.raises(ValueError, match="interval"):
        f.kline_url("BTCUSDT", "5m", 2024, 1)


def test_intervals_supported():
    assert set(f.SUPPORTED_INTERVALS) == {"1d", "1h"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_pit_klines_fetcher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pit_klines_fetcher'`.

- [ ] **Step 3: Implement the fetcher**

```python
#!/usr/bin/env python3
"""ROB-353 (PR1) — download Binance USDⓈ-M public klines dumps (1d, 1h).

Pure stdlib. PUBLIC data only (data.binance.vision) — no keys, no auth, no orders.
Each archive is verified against its sibling ``.CHECKSUM`` when published. Writes
under ``artifact_paths.pit_data_root()`` (gitignored). No secrets are printed.

Usage:
    uv run --no-project python pit_klines_fetcher.py --symbol EOSUSDT \\
        --interval 1d --from-month 2023-01 --to-month 2024-01
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from artifact_paths import pit_data_root

BASE = "https://data.binance.vision/data"
SUPPORTED_INTERVALS = ("1d", "1h")
_CHUNK = 1 << 16


def kline_url(symbol, interval, year, month, market="um", cadence="monthly", day=None):
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}; expected {SUPPORTED_INTERVALS}")
    if cadence == "monthly":
        stem = f"{symbol}-{interval}-{year:04d}-{month:02d}"
        sub = f"futures/{market}/monthly/klines/{symbol}/{interval}"
    elif cadence == "daily":
        if day is None:
            raise ValueError("daily cadence requires day")
        stem = f"{symbol}-{interval}-{year:04d}-{month:02d}-{day:02d}"
        sub = f"futures/{market}/daily/klines/{symbol}/{interval}"
    else:
        raise ValueError(f"unknown cadence {cadence!r}")
    return f"{BASE}/{sub}/{stem}.zip"


def _download(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as fh:
            while chunk := resp.read(_CHUNK):
                fh.write(chunk)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _verify(zip_path: Path, checksum_path: Path) -> None:
    expected = checksum_path.read_text().split()[0].strip().lower()
    if _sha256(zip_path) != expected:
        raise ValueError(f"checksum mismatch for {zip_path.name}")


def _months(from_month: str, to_month: str):
    y0, m0 = int(from_month[:4]), int(from_month[5:7])
    y1, m1 = int(to_month[:4]), int(to_month[5:7])
    cur = y0 * 12 + (m0 - 1)
    end = y1 * 12 + (m1 - 1)
    while cur <= end:
        yield cur // 12, cur % 12 + 1
        cur += 1


def fetch_months(symbol, interval, from_month, to_month, market="um", out_root=None) -> dict:
    out_root = Path(out_root) if out_root else pit_data_root()
    out_dir = out_root / "klines" / interval / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = missing = 0
    for year, month in _months(from_month, to_month):
        stem = f"{symbol}-{interval}-{year:04d}-{month:02d}"
        csv_path = out_dir / f"{stem}.csv"
        if csv_path.exists():
            skipped += 1
            continue
        url = kline_url(symbol, interval, year, month, market=market, cadence="monthly")
        zip_path = out_dir / f"{stem}.zip"
        if not _download(url, zip_path):
            missing += 1
            continue
        chk_path = out_dir / f"{stem}.zip.CHECKSUM"
        if _download(f"{url}.CHECKSUM", chk_path):
            _verify(zip_path, chk_path)
            chk_path.unlink()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out_dir)
        zip_path.unlink()
        downloaded += 1
    return {"downloaded": downloaded, "skipped": skipped, "missing": missing, "dir": str(out_dir)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download Binance USDⓈ-M public klines (1d/1h)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", choices=SUPPORTED_INTERVALS, required=True)
    ap.add_argument("--from-month", required=True, help="YYYY-MM")
    ap.add_argument("--to-month", required=True, help="YYYY-MM")
    ap.add_argument("--market", default="um")
    args = ap.parse_args(argv)
    summary = fetch_months(args.symbol, args.interval, args.from_month, args.to_month, args.market)
    print(f"done: {summary['downloaded']} downloaded, {summary['skipped']} skipped, "
          f"{summary['missing']} missing -> {summary['dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_pit_klines_fetcher.py -q`
Expected: PASS (4 passed). Confirm side-effect-free import: `uv run --no-project python -c "import pit_klines_fetcher"`.

- [ ] **Step 5: Commit**

```bash
git add pit_klines_fetcher.py tests/test_pit_klines_fetcher.py
git commit -m "feat(ROB-353): public USD-M klines fetcher (1d/1h, checksum-verified, gitignored root)"
```

---

## Task 8: `pit_bars.py` — PIT-trimmed `families.Bar` loader

**Files:**
- Create: `pit_bars.py`
- Test: `tests/test_pit_bars.py`

Read the standard Binance kline CSV (header or headerless), build `families.Bar(ts, high, low, close)`, and trim to PIT membership: keep bars with `manifest listing.tradeable_at(ts)`, then drop leading/trailing zero-volume bars (defense against any freeze tail inside the window). Kline columns: `open_time, open, high, low, close, volume, ...`.

- [ ] **Step 1: Write the failing test (synthetic CSVs)**

```python
# tests/test_pit_bars.py
from pathlib import Path

import pit_bars
import pit_universe

KCOLS = "open_time,open,high,low,close,volume,close_time,qv,n,tbv,tbqv,ig"
DAY = 86_400_000


def _write_csv(root, symbol, interval, month, rows):
    d = root / "klines" / interval / symbol
    d.mkdir(parents=True, exist_ok=True)
    lines = [KCOLS] + [",".join(str(x) for x in r) for r in rows]
    (d / f"{symbol}-{interval}-{month}.csv").write_text("\n".join(lines) + "\n")


def test_load_bars_trims_to_membership_and_zero_vol_tail(tmp_path):
    # 5 daily bars; ts 0..4*DAY. volume 0 on first and last (freeze/zero-vol) -> trimmed.
    rows = [
        [0 * DAY, 10, 11, 9, 10, 0.0, 0, 0, 0, 0, 0, 0],
        [1 * DAY, 10, 12, 9, 11, 5.0, 0, 0, 0, 0, 0, 0],
        [2 * DAY, 11, 13, 10, 12, 6.0, 0, 0, 0, 0, 0, 0],
        [3 * DAY, 12, 14, 11, 13, 7.0, 0, 0, 0, 0, 0, 0],
        [4 * DAY, 13, 13, 13, 13, 0.0, 0, 0, 0, 0, 0, 0],
    ]
    _write_csv(tmp_path, "EOSUSDT", "1d", "1970-01", rows)
    # manifest: listed at ts=DAY, delisted_at exclusive at 4*DAY (so ts=3*DAY is last tradeable)
    m = pit_universe.PITManifest.from_records(
        [{"symbol": "EOSUSDT", "listed_from": 1 * DAY, "delisted_at": 4 * DAY}]
    )
    bars = pit_bars.load_bars("EOSUSDT", "1d", m, root=tmp_path)
    assert [b.ts for b in bars] == [1 * DAY, 2 * DAY, 3 * DAY]
    assert bars[0].close == 11 and bars[-1].close == 13


def test_load_bars_unknown_symbol_returns_empty(tmp_path):
    m = pit_universe.PITManifest.from_records([{"symbol": "X", "listed_from": 0}])
    assert pit_bars.load_bars("X", "1d", m, root=tmp_path) == []


def test_load_panel_aligns_close_series(tmp_path):
    _write_csv(tmp_path, "AUSDT", "1d", "1970-01",
               [[1 * DAY, 1, 1, 1, 100, 5, 0, 0, 0, 0, 0, 0],
                [2 * DAY, 1, 1, 1, 110, 5, 0, 0, 0, 0, 0, 0]])
    _write_csv(tmp_path, "BUSDT", "1d", "1970-01",
               [[1 * DAY, 1, 1, 1, 200, 5, 0, 0, 0, 0, 0, 0],
                [2 * DAY, 1, 1, 1, 220, 5, 0, 0, 0, 0, 0, 0]])
    m = pit_universe.PITManifest.from_records([
        {"symbol": "AUSDT", "listed_from": 0}, {"symbol": "BUSDT", "listed_from": 0},
    ])
    panel = pit_bars.load_panel(["AUSDT", "BUSDT"], "1d", m, root=tmp_path)
    assert panel["AUSDT"] == [(1 * DAY, 100.0), (2 * DAY, 110.0)]
    assert panel["BUSDT"] == [(1 * DAY, 200.0), (2 * DAY, 220.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-project pytest tests/test_pit_bars.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pit_bars'`.

- [ ] **Step 3: Implement the loader**

```python
"""ROB-353 (PR1) — load PIT-trimmed bars from downloaded klines (data half of the PR2 bridge).

Reads the standard Binance kline CSV under ``pit_data_root()/klines/<interval>/<symbol>/``,
emits ``families.Bar`` (ts, high, low, close), trimmed to PIT membership (the manifest's
survivorship-safe ``tradeable_at`` window) with leading/trailing zero-volume bars dropped.
Pure transformation — no network. ``load_panel`` returns per-symbol (ts, close) series for
cross-sectional families.
"""
from __future__ import annotations

import csv
import glob
from pathlib import Path

import families
from artifact_paths import pit_data_root
from pit_universe import PITManifest

_KCOLS = ("open_time", "open", "high", "low", "close", "volume")


def _read_rows(symbol: str, interval: str, root: Path) -> list[tuple[int, float, float, float, float]]:
    """Return sorted, de-duplicated (ts, high, low, close, volume) for a symbol."""
    d = root / "klines" / interval / symbol
    seen: dict[int, tuple[int, float, float, float, float]] = {}
    for path in sorted(glob.glob(str(d / f"{symbol}-{interval}-*.csv"))):
        with open(path, newline="") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if not row or row[0].lower().startswith("open_time"):
                    continue
                try:
                    ts = int(row[0])
                    seen[ts] = (ts, float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                except (ValueError, IndexError):
                    continue
    return [seen[k] for k in sorted(seen)]


def _trim_zero_vol_edges(rows):
    lo, hi = 0, len(rows)
    while lo < hi and rows[lo][4] <= 0.0:
        lo += 1
    while hi > lo and rows[hi - 1][4] <= 0.0:
        hi -= 1
    return rows[lo:hi]


def load_bars(symbol: str, interval: str, manifest: PITManifest, root=None) -> list[families.Bar]:
    root = Path(root) if root else pit_data_root()
    listing = next((x for x in manifest.listings if x.symbol == symbol), None)
    rows = _read_rows(symbol, interval, root)
    if listing is not None:
        rows = [r for r in rows if listing.tradeable_at(r[0])]
    rows = _trim_zero_vol_edges(rows)
    return [families.Bar(ts=r[0], high=r[1], low=r[2], close=r[3]) for r in rows]


def load_panel(symbols, interval: str, manifest: PITManifest, root=None) -> dict[str, list[tuple[int, float]]]:
    root = Path(root) if root else pit_data_root()
    out: dict[str, list[tuple[int, float]]] = {}
    for s in symbols:
        bars = load_bars(s, interval, manifest, root=root)
        if bars:
            out[s] = [(b.ts, b.close) for b in bars]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-project pytest tests/test_pit_bars.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add pit_bars.py tests/test_pit_bars.py
git commit -m "feat(ROB-353): PIT-trimmed families.Bar loader + cross-sectional panel accessor"
```

---

## Task 9: Import guard — new modules import no `app.*`; raw-data root is gitignored

**Files:**
- Create: `tests/test_pit_data_layer_guard.py`

Mirror the existing `tests/test_discovery_paths.py` guard. Parse each new module's AST and assert no `import app...` / `from app...`; assert `pit_data_root()` resolves inside a gitignored path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pit_data_layer_guard.py
import ast
from pathlib import Path

import artifact_paths

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = ["pit_klines_fetcher.py", "pit_bars.py", "build_pit_universe.py", "pit_universe.py"]


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_no_app_imports_in_data_layer():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            assert not name.startswith("app"), f"{mod} imports forbidden app module {name!r}"


def test_pit_data_root_is_gitignored(monkeypatch):
    monkeypatch.delenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", raising=False)
    root = artifact_paths.pit_data_root()
    gitignore = (_ROOT / ".gitignore").read_text()
    assert "data/" in gitignore
    assert root.name == "data"
```

- [ ] **Step 2: Run test to verify it fails then passes**

Run: `uv run --no-project pytest tests/test_pit_data_layer_guard.py -q`
Expected: PASS immediately (the modules already avoid `app.*` and `data/` is gitignored). If it FAILS, a prior task introduced an `app` import or dropped the `data/` ignore — fix the offending module, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_pit_data_layer_guard.py
git commit -m "test(ROB-353): guard data layer against app imports + raw-data leakage"
```

---

## Task 10: Runbook — data/universe definition

**Files:**
- Create: `docs/runbooks/rob-353-pit-data-layer.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/rob-353-pit-data-layer.md` with these exact sections (fill values from the committed `pit_universe.v1.meta.json`):

```markdown
# ROB-353 PIT data layer runbook

Research/backtest only. Read-only public data. No live/Demo/broker/scheduler/DB mutation.

## Data source & retrieval
- Source: `data.binance.vision` USDⓈ-M futures (`futures/um`), public, no keys.
- Universe index: `build_pit_universe.py` (read-only S3 listing + boundary-month 1d klines).
- Bars: `pit_klines_fetcher.py --symbol <S> --interval {1d,1h} --from-month --to-month`.

## Universe definition
- USDT perpetuals only via `PITManifest.strict_usdt_perp()`: `status ∈ {live, dead}` plain
  `*USDT`; excludes `settling`, BUSD/USDC-quoted, dated/quarterly (`_`), `*SETTLED`.
- Active + delisted symbols included (the survivorship fix ROB-349 verified).
- PIT membership: each symbol tradeable only over `[listed_from, delisted_at)` (epoch ms,
  `delisted_at` exclusive); post-delist price-frozen zero-volume tail trimmed in `pit_bars`.

## Manifest
- Committed (metadata only): `data_manifests/pit_universe.v1.json` + `.meta.json`.
- Pinned by `snapshot_hash` (sha256 over canonical records). Editing the manifest requires
  updating `.meta.json` or `test_committed_manifest_loads_and_hash_matches_meta` fails.

## Raw-data root (NOT committed)
- `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT/data/klines/<interval>/<symbol>/` if set, else
  `research/nautilus_scalping/data/...`. Both gitignored. No secrets logged.

## Regenerate the manifest (operator)
    curl -s https://fapi.binance.com/fapi/v1/exchangeInfo > /tmp/pit_audit_exchangeinfo.json
    uv run --no-project python build_pit_universe.py --exchange-info /tmp/pit_audit_exchangeinfo.json \
        --out data_manifests/pit_universe.v1.json

## Not in this PR
- The `specs → campaign.run_campaign` bridge and families 1–3 RUN/verdict are PR2.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/rob-353-pit-data-layer.md
git commit -m "docs(ROB-353): PIT data-layer runbook (data/universe definition)"
```

---

## Task 11: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full research test suite green**

Run: `uv run --no-project pytest -q`
Expected: PASS. Investigate any failure before proceeding; do not weaken tests to pass.

- [ ] **Step 2: Self-test + config hash unchanged**

Run: `uv run --no-project python run_rob351_campaign.py --self-test | grep config_hash`
Expected: `config_hash 8f02dffd51dc5bedf5ab4c1521edb2185f4768304b5b60fa7dd0836ef8872adf` (PR1 must not touch the frozen config).

- [ ] **Step 3: Confirm no raw data / secrets staged**

Run: `git diff --stat origin/main...HEAD && git ls-files data/ data_manifests/`
Expected: only source modules, tests, `data_manifests/*.json` (metadata), and docs; no `data/` files, no `*.csv` klines, no `.parquet`.

- [ ] **Step 4: Lint (match repo style)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-353 && uv run ruff check research/nautilus_scalping/`
Expected: clean (or only pre-existing warnings unrelated to new files).

- [ ] **Step 5: Push + open PR (base `main`)**

```bash
git push -u origin rob-353
gh pr create --base main --title "feat(ROB-353): PIT data layer for Binance USD-M campaign (PR1/2)" \
  --body "Durableizes the ROB-349 PIT universe + klines prototype into reusable repo infra (fetcher, extended manifest, ported builder, PIT-trimmed Bar loader) + a committed metadata-only manifest. Research-only; no RUN. Bridge + families 1-3 verdict are PR2. Spec: docs/superpowers/specs/2026-05-29-rob-353-pit-data-layer-design.md"
```

---

## Self-review notes (author)

- **Spec coverage:** fetcher (Task 7), extended manifest (Tasks 2–4), builder (Task 6), bar loader (Task 8), committed metadata manifest + hash (Task 5), tests/guards (Tasks 1–9), runbook (Task 10), boundaries (Tasks 5/9/11). PR2 bridge explicitly out of scope.
- **Type consistency:** `load_bars`/`load_panel`/`kline_url`/`strict_usdt_perp`/`snapshot_hash`/`from_pit_index_records`/`_date_to_epoch_ms`/`pit_data_root` names are used identically across tasks and the file-structure contract table.
- **Known soft spot:** Task 5 Step 3 asserts three specific dead-perp symbols; if spelling differs in the real manifest, the step says to swap to three confirmed symbols (not weaken the `>100` check). This is the one place reality may differ from the plan.
