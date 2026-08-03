"""Durable local checkpoint state for a resumable corpus build.

These SQLite files are private working indexes under the approved artifact and
holdout roots.  They are not the operating database and are never imported by
the application.  Their job is only to make a stopped, single-process
collection resume without refetching completed source calls.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoverageRow:
    market: str
    year: int
    membership_rows: int
    bar_rows: int

    @property
    def ratio(self) -> float | None:
        if self.membership_rows == 0:
            return None
        return self.bar_rows / self.membership_rows


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    scope: str
    sha256: str
    byte_size: int


class StateStore:
    """SQLite-backed membership, coverage, checkpoint, and file metadata."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS membership (
                session TEXT NOT NULL,
                market TEXT NOT NULL,
                ticker TEXT NOT NULL,
                PRIMARY KEY (session, market, ticker)
            );
            CREATE INDEX IF NOT EXISTS membership_ticker_idx
                ON membership (ticker, session, market);

            CREATE TABLE IF NOT EXISTS completed_membership (
                session TEXT NOT NULL,
                market TEXT NOT NULL,
                PRIMARY KEY (session, market)
            );

            CREATE TABLE IF NOT EXISTS completed_ticker (
                ticker TEXT NOT NULL PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS bar_presence (
                session TEXT NOT NULL,
                market TEXT NOT NULL,
                ticker TEXT NOT NULL,
                PRIMARY KEY (session, market, ticker)
            );

            CREATE TABLE IF NOT EXISTS gap_reason (
                session TEXT NOT NULL,
                market TEXT NOT NULL,
                ticker TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL,
                PRIMARY KEY (session, market, ticker)
            );

            CREATE TABLE IF NOT EXISTS source_anomaly (
                kind TEXT NOT NULL,
                session TEXT NOT NULL,
                market TEXT NOT NULL,
                ticker TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                PRIMARY KEY (kind, session, market, ticker, detail_json)
            );

            CREATE TABLE IF NOT EXISTS error_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phase TEXT NOT NULL,
                item_key TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS file_record (
                relative_path TEXT NOT NULL PRIMARY KEY,
                scope TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def add_membership(self, session: str, market: str, tickers: Iterable[str]) -> None:
        rows = [(session, market, ticker) for ticker in tickers]
        self.connection.executemany(
            "INSERT INTO membership(session, market, ticker) VALUES (?, ?, ?)", rows
        )
        self.connection.execute(
            "INSERT INTO completed_membership(session, market) VALUES (?, ?)",
            (session, market),
        )
        self.connection.commit()

    def membership_completed(self, session: str, market: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM completed_membership WHERE session = ? AND market = ?",
            (session, market),
        ).fetchone()
        return row is not None

    def completed_membership_count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM completed_membership"
            ).fetchone()[0]
        )

    def completed_ticker_count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM completed_ticker").fetchone()[
                0
            ]
        )

    def all_tickers(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT DISTINCT ticker FROM membership ORDER BY ticker"
        ).fetchall()
        return tuple(row[0] for row in rows)

    def ticker_membership(self, ticker: str) -> tuple[tuple[str, str], ...]:
        rows = self.connection.execute(
            """
            SELECT session, market
            FROM membership
            WHERE ticker = ?
            ORDER BY session, market
            """,
            (ticker,),
        ).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    def ticker_completed(self, ticker: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM completed_ticker WHERE ticker = ?", (ticker,)
        ).fetchone()
        return row is not None

    def mark_ticker_completed(self, ticker: str) -> None:
        self.connection.execute(
            "INSERT INTO completed_ticker(ticker) VALUES (?)", (ticker,)
        )
        self.connection.commit()

    def add_bar_presence(self, session: str, market: str, ticker: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO bar_presence(session, market, ticker) VALUES (?, ?, ?)",
            (session, market, ticker),
        )

    def commit(self) -> None:
        self.connection.commit()

    def mark_gap_for_membership(self, ticker: str, reason: str, detail: str) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO gap_reason(session, market, ticker, reason, detail)
            SELECT session, market, ticker, ?, ?
            FROM membership
            WHERE ticker = ?
            """,
            (reason, detail, ticker),
        )
        self.connection.commit()

    def mark_gap(
        self, session: str, market: str, ticker: str, reason: str, detail: str
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO gap_reason(session, market, ticker, reason, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session, market, ticker, reason, detail),
        )

    def record_source_anomaly(
        self,
        kind: str,
        session: str,
        market: str,
        ticker: str,
        detail: dict[str, object],
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO source_anomaly(kind, session, market, ticker, detail_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                kind,
                session,
                market,
                ticker,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
            ),
        )

    def record_error(self, phase: str, item_key: str, reason: str, detail: str) -> None:
        self.connection.execute(
            """
            INSERT INTO error_record(phase, item_key, reason, detail)
            VALUES (?, ?, ?, ?)
            """,
            (phase, item_key, reason, detail),
        )
        self.connection.commit()

    def errors(self) -> Iterator[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT id, phase, item_key, reason, detail
            FROM error_record
            ORDER BY id
            """
        )
        for row in rows:
            yield {
                "id": row[0],
                "phase": row[1],
                "item_key": row[2],
                "reason": row[3],
                "detail": row[4],
            }

    def source_anomalies(self) -> Iterator[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT kind, session, market, ticker, detail_json
            FROM source_anomaly
            ORDER BY kind, session, market, ticker, detail_json
            """
        )
        for row in rows:
            yield {
                "kind": row[0],
                "session": row[1],
                "market": row[2],
                "ticker": row[3],
                "detail": json.loads(row[4]),
            }

    def missing_rows(self, market: str, year: int) -> Iterator[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT
                m.session,
                m.market,
                m.ticker,
                COALESCE(g.reason, 'bar_not_returned') AS reason,
                COALESCE(g.detail, 'source returned no matching bar') AS detail
            FROM membership AS m
            LEFT JOIN bar_presence AS b
                ON b.session = m.session
                AND b.market = m.market
                AND b.ticker = m.ticker
            LEFT JOIN gap_reason AS g
                ON g.session = m.session
                AND g.market = m.market
                AND g.ticker = m.ticker
            WHERE m.market = ?
                AND substr(m.session, 1, 4) = ?
                AND b.session IS NULL
            ORDER BY m.session, m.ticker
            """,
            (market, str(year)),
        )
        for row in rows:
            yield {
                "session": row[0],
                "market": row[1],
                "ticker": row[2],
                "reason": row[3],
                "detail": row[4],
            }

    def coverage(
        self, markets: tuple[str, ...], years: Iterable[int]
    ) -> tuple[CoverageRow, ...]:
        result: list[CoverageRow] = []
        for market in markets:
            for year in years:
                row = self.connection.execute(
                    """
                    SELECT COUNT(*), COUNT(b.session)
                    FROM membership AS m
                    LEFT JOIN bar_presence AS b
                        ON b.session = m.session
                        AND b.market = m.market
                        AND b.ticker = m.ticker
                    WHERE m.market = ? AND substr(m.session, 1, 4) = ?
                    """,
                    (market, str(year)),
                ).fetchone()
                result.append(
                    CoverageRow(
                        market=market,
                        year=year,
                        membership_rows=int(row[0]),
                        bar_rows=int(row[1]),
                    )
                )
        return tuple(result)

    def gap_reason_counts(self) -> tuple[tuple[str, int], ...]:
        rows = self.connection.execute(
            """
            SELECT reason, COUNT(*)
            FROM (
                SELECT COALESCE(g.reason, 'bar_not_returned') AS reason
                FROM membership AS m
                LEFT JOIN bar_presence AS b
                    ON b.session = m.session
                    AND b.market = m.market
                    AND b.ticker = m.ticker
                LEFT JOIN gap_reason AS g
                    ON g.session = m.session
                    AND g.market = m.market
                    AND g.ticker = m.ticker
                WHERE b.session IS NULL
            )
            GROUP BY reason
            ORDER BY reason
            """
        ).fetchall()
        return tuple((str(row[0]), int(row[1])) for row in rows)

    def explicit_gap_count(self) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM membership AS m
            LEFT JOIN bar_presence AS b
                ON b.session = m.session
                AND b.market = m.market
                AND b.ticker = m.ticker
            WHERE b.session IS NULL
            """
        ).fetchone()
        return int(row[0])

    def membership_count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM membership").fetchone()[0]
        )

    def bar_count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM bar_presence").fetchone()[0]
        )

    def count_tickers(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(DISTINCT ticker) FROM membership"
            ).fetchone()[0]
        )

    def ticker_bar_count(self, ticker: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM bar_presence WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
        )

    def has_membership(self, session: str, market: str, ticker: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM membership
            WHERE session = ? AND market = ? AND ticker = ?
            """,
            (session, market, ticker),
        ).fetchone()
        return row is not None

    def register_file(
        self, relative_path: str, scope: str, sha256: str, byte_size: int
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO file_record(relative_path, scope, sha256, byte_size)
            VALUES (?, ?, ?, ?)
            """,
            (relative_path, scope, sha256, byte_size),
        )
        self.connection.commit()

    def upsert_file(
        self, relative_path: str, scope: str, sha256: str, byte_size: int
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO file_record(relative_path, scope, sha256, byte_size)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                scope = excluded.scope,
                sha256 = excluded.sha256,
                byte_size = excluded.byte_size
            """,
            (relative_path, scope, sha256, byte_size),
        )
        self.connection.commit()

    def file_record(self, relative_path: str) -> FileRecord | None:
        row = self.connection.execute(
            """
            SELECT relative_path, scope, sha256, byte_size
            FROM file_record
            WHERE relative_path = ?
            """,
            (relative_path,),
        ).fetchone()
        return FileRecord(*row) if row is not None else None

    def files(self) -> tuple[FileRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT relative_path, scope, sha256, byte_size
            FROM file_record
            ORDER BY relative_path
            """
        ).fetchall()
        return tuple(FileRecord(*row) for row in rows)

    def set_metadata(self, key: str, value: object) -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, encoded),
        )
        self.connection.commit()

    def get_metadata(self, key: str) -> object | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
