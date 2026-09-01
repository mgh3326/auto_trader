"""Create a minimal, read-only production snapshot for the portable dev kit.

The source connection is used exclusively for ``SELECT`` and PostgreSQL COPY
OUT operations.  The generated file is a plain SQL restore artifact consumed
by ``make dev-seed`` *after* the local database reaches Alembic head.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "trading_policy.yaml"
NULL_MARKER = r"\N"

COLUMN_QUERY = """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = $1
  AND table_name = $2
ORDER BY ordinal_position
"""

ALEMBIC_VERSION_QUERY = """
SELECT version_num
FROM alembic_version
LIMIT 1
"""

KR_UNIVERSE_QUERY = """
SELECT *
FROM public.kr_symbol_universe
WHERE is_active IS TRUE
ORDER BY shares_outstanding DESC NULLS LAST, symbol ASC
LIMIT $1
"""

US_UNIVERSE_QUERY = """
SELECT *
FROM public.us_symbol_universe
WHERE is_active IS TRUE
ORDER BY shares_outstanding DESC NULLS LAST, symbol ASC
LIMIT $1
"""

UPBIT_UNIVERSE_QUERY = """
SELECT *
FROM public.upbit_symbol_universe
WHERE is_active IS TRUE
ORDER BY market ASC
LIMIT $1
"""

SYMBOL_SECTORS_QUERY = """
SELECT *
FROM public.symbol_sectors
WHERE id IN (
    SELECT sector_id
    FROM public.kr_symbol_universe
    WHERE is_active IS TRUE AND sector_id IS NOT NULL
    ORDER BY shares_outstanding DESC NULLS LAST, symbol ASC
    LIMIT $1
)
OR id IN (
    SELECT sector_id
    FROM public.us_symbol_universe
    WHERE is_active IS TRUE AND sector_id IS NOT NULL
    ORDER BY shares_outstanding DESC NULLS LAST, symbol ASC
    LIMIT $1
)
ORDER BY id ASC
"""

KR_CANDLES_QUERY = """
SELECT candles.*
FROM public.kr_candles_1d AS candles
JOIN (
    SELECT symbol
    FROM public.kr_symbol_universe
    WHERE is_active IS TRUE
    ORDER BY shares_outstanding DESC NULLS LAST, symbol ASC
    LIMIT $1
) AS universe ON universe.symbol = candles.symbol
WHERE candles.time >= CURRENT_DATE - ($2::int * INTERVAL '1 day')
ORDER BY candles.time ASC, candles.symbol ASC, candles.venue ASC
"""

US_CANDLES_QUERY = """
SELECT candles.*
FROM public.us_candles_1d AS candles
JOIN (
    SELECT symbol
    FROM public.us_symbol_universe
    WHERE is_active IS TRUE
    ORDER BY shares_outstanding DESC NULLS LAST, symbol ASC
    LIMIT $1
) AS universe ON universe.symbol = candles.symbol
WHERE candles.time >= CURRENT_DATE - ($2::int * INTERVAL '1 day')
ORDER BY candles.time ASC, candles.symbol ASC, candles.exchange ASC
"""


@dataclass(frozen=True)
class ExportSpec:
    schema: str
    table: str
    query: str
    params: tuple[int, ...]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_source_dsn(raw_dsn: str) -> str:
    """Make a SQLAlchemy asyncpg URL acceptable to asyncpg without logging it."""

    parsed = urlsplit(raw_dsn)
    scheme = parsed.scheme.lower()
    if scheme == "postgresql+asyncpg":
        scheme = "postgresql"
    if scheme not in {"postgres", "postgresql"}:
        raise ValueError("source database URL must use postgres:// or postgresql://")
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _policy_sha256() -> str:
    return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()


def _build_specs(*, top_n: int, days: int) -> tuple[ExportSpec, ...]:
    # Parent rows are emitted before the universes that reference them.
    return (
        ExportSpec("public", "symbol_sectors", SYMBOL_SECTORS_QUERY, (top_n,)),
        ExportSpec("public", "kr_symbol_universe", KR_UNIVERSE_QUERY, (top_n,)),
        ExportSpec("public", "us_symbol_universe", US_UNIVERSE_QUERY, (top_n,)),
        ExportSpec("public", "upbit_symbol_universe", UPBIT_UNIVERSE_QUERY, (top_n,)),
        ExportSpec("public", "kr_candles_1d", KR_CANDLES_QUERY, (top_n, days)),
        ExportSpec("public", "us_candles_1d", US_CANDLES_QUERY, (top_n, days)),
    )


async def _columns(
    connection: asyncpg.Connection[asyncpg.Record], *, schema: str, table: str
) -> tuple[str, ...]:
    rows = await connection.fetch(COLUMN_QUERY, schema, table)
    columns = tuple(str(row["column_name"]) for row in rows)
    if not columns:
        raise RuntimeError(
            f"source table {schema}.{table} is missing or has no columns"
        )
    return columns


async def _copy_out(
    connection: asyncpg.Connection[asyncpg.Record], spec: ExportSpec
) -> tuple[tuple[str, ...], bytes]:
    columns = await _columns(connection, schema=spec.schema, table=spec.table)
    output = io.BytesIO()
    await connection.copy_from_query(
        spec.query,
        *spec.params,
        output=output,
        format="csv",
        header=False,
        null=NULL_MARKER,
    )
    return columns, output.getvalue()


def _restore_copy_header(spec: ExportSpec, columns: tuple[str, ...]) -> bytes:
    target = f"{_quote_identifier(spec.schema)}.{_quote_identifier(spec.table)}"
    column_list = ", ".join(_quote_identifier(column) for column in columns)
    return (
        f"COPY {target} ({column_list}) FROM STDIN WITH (FORMAT csv, NULL '{NULL_MARKER}');\n"
    ).encode()


async def _build_dump(
    *, source_dsn: str, top_n: int, days: int
) -> tuple[bytes, tuple[tuple[str, int], ...], str | None]:
    connection = await asyncpg.connect(
        dsn=source_dsn,
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "30000",
        },
    )
    try:
        # This is defense in depth for the operator-provided source role.  Every
        # query below is a SELECT; COPY is performed through copy_from_query,
        # which is PostgreSQL COPY ... TO STDOUT.
        async with connection.transaction(
            isolation="serializable", readonly=True, deferrable=True
        ):
            version_row = await connection.fetchrow(ALEMBIC_VERSION_QUERY)
            source_revision = (
                str(version_row["version_num"]) if version_row is not None else None
            )
            chunks = [
                b"-- portable dev seed; generated by scripts/make_dev_seed.py\n",
                b"-- source database was connected in a read-only transaction\n",
                f"-- repository trading policy sha256: {_policy_sha256()}\n".encode(),
                (
                    f"-- source alembic revision: {source_revision or 'unavailable'}\n"
                ).encode(),
                b"BEGIN;\n",
            ]
            counts: list[tuple[str, int]] = []
            for spec in _build_specs(top_n=top_n, days=days):
                columns, payload = await _copy_out(connection, spec)
                chunks.append(_restore_copy_header(spec, columns))
                chunks.append(payload)
                if not payload.endswith(b"\n"):
                    chunks.append(b"\n")
                chunks.append(b"\\.\n")
                counts.append((f"{spec.schema}.{spec.table}", payload.count(b"\n")))
            # This applies only when the artifact is restored into the local
            # development database.  It keeps later lazy sector inserts from
            # colliding with the explicit IDs in the bounded parent slice.
            chunks.append(
                b"SELECT setval(\n"
                b"    pg_get_serial_sequence('public.symbol_sectors', 'id'),\n"
                b"    COALESCE((SELECT MAX(id) FROM public.symbol_sectors), 1),\n"
                b"    true\n"
                b");\n"
            )
            chunks.append(b"COMMIT;\n")
            return b"".join(chunks), tuple(counts), source_revision
    finally:
        await connection.close()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-database-url",
        required=True,
        help="operator-provided PostgreSQL URL for a read-only source role",
    )
    parser.add_argument("--output", default="dev-seed.dump", type=Path)
    parser.add_argument("--top-n", default=50, type=_positive_int)
    parser.add_argument("--days", default=60, type=_positive_int)
    parser.add_argument(
        "--force", action="store_true", help="replace an existing output artifact"
    )
    return parser


def _write_output(*, output: Path, data: bytes, force: bool) -> None:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise FileExistsError(
            f"{output.name} already exists; rerun with --force to replace it"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(output)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_n > 1_000:
        raise SystemExit("--top-n must not exceed 1000")
    if args.days > 365:
        raise SystemExit("--days must not exceed 365")

    try:
        source_dsn = _normalize_source_dsn(args.source_database_url)
        dump, counts, source_revision = asyncio.run(
            _build_dump(source_dsn=source_dsn, top_n=args.top_n, days=args.days)
        )
        _write_output(output=args.output, data=dump, force=args.force)
    except (OSError, ValueError, asyncpg.PostgresError) as exc:
        # Do not interpolate exception strings: drivers can include connection
        # details in them, and the source URL is never useful in an operator log.
        print(f"dev seed export failed: {type(exc).__name__}")
        return 2

    rendered_counts = ", ".join(f"{table}={count}" for table, count in counts)
    revision = source_revision or "unavailable"
    print(
        f"dev seed export wrote {args.output} (source revision {revision}; {rendered_counts})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
