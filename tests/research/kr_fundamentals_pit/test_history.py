import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.kr_fundamentals_pit import cli, collector
from research.kr_fundamentals_pit.build import build_rows
from research.kr_fundamentals_pit.collector import Collector, SourceError
from research.kr_fundamentals_pit.store import as_of, publish

KEY = "synthetic-secret-sentinel"


def archive():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr(
            "CORPCODE.xml",
            "<result><list><corp_code>99999999</corp_code><stock_code>999999</stock_code></list></result>",
        )
    return output.getvalue()


def transport(amendment=False):
    calls = []

    def get(endpoint, params, key):
        calls.append((endpoint, params))
        if endpoint == "corpCode.xml":
            return archive()
        if endpoint == "list.json":
            rows = [
                {
                    "corp_code": "99999999",
                    "rcept_no": "20200102000001",
                    "rcept_dt": "20200102",
                    "report_nm": "사업보고서 (2019.12)",
                }
            ]
            if amendment:
                rows.append(
                    {
                        "corp_code": "99999999",
                        "rcept_no": "20200103000002",
                        "rcept_dt": "20200103",
                        "report_nm": "[기재정정]사업보고서 (2019.12)",
                    }
                )
            return json.dumps({"status": "000", "total_page": 1, "list": rows}).encode()
        if params["fs_div"] == "CFS":
            return b'{"status":"013"}'
        receipt = "20200103000002" if amendment else "20200102000001"
        rows = [
            {
                "rcept_no": receipt,
                "account_id": "ifrs-full_Revenue",
                "sj_div": "IS",
                "thstrm_amount": "1,234",
                "currency": "KRW",
            }
        ]
        return json.dumps({"status": "000", "list": rows}).encode()

    return get, calls


class HistoryTest(unittest.TestCase):
    def test_budget_resume_versions_asof_and_write_once(self):
        with tempfile.TemporaryDirectory() as d:
            get, calls = transport()
            c = Collector(d, "first", "2020-01-01", "2020-01-31", KEY, 2, get)
            self.assertFalse(c.collect()["complete"])
            self.assertEqual(len(calls), 2)
            c = Collector(d, "first", "2020-01-01", "2020-01-31", KEY, 2, get)
            first = c.collect()
            self.assertTrue(first["complete"])
            self.assertEqual(len(calls), 4)
            self.assertEqual(first["reports"][0]["statement_basis"], "OFS")
            facts, derived = build_rows(first["reports"])
            self.assertEqual(
                facts[0]["available_at"], "2020-01-02T23:59:59.999999+09:00"
            )
            self.assertEqual(
                facts[0]["available_at_basis"], "latest_amendment_rcept_date_eod"
            )
            self.assertEqual(derived[0]["metrics"]["revenue"]["value"], "1234")
            self.assertIsNone(derived[0]["metrics"]["equity"]["value"])
            self.assertEqual(
                derived[0]["metrics"]["equity"]["reason"], "account_not_found"
            )
            meta = {
                "coverage_start": "2020-01-01",
                "coverage_end": "2020-01-31T23:59:59.999999+09:00",
                "fetch_budget_used": 4,
                "missing_reports": [],
            }
            entry = publish(d, "first", facts, derived, meta)
            self.assertEqual(entry["partitions"]["facts"]["rows"], 1)
            self.assertEqual(len(entry["partitions"]["facts"]["sha256"]), 64)
            with self.assertRaises(FileExistsError):
                publish(d, "first", facts, derived, meta)
            get2, _ = transport(True)
            second = Collector(
                d, "second", "2020-01-01", "2020-01-31", KEY, 10, get2
            ).collect()
            facts2, derived2 = build_rows(second["reports"])
            publish(d, "second", facts2, derived2, meta)
            self.assertEqual(len(derived2[0]["history"]), 2)
            self.assertTrue(facts2[0]["amendment"])
            self.assertEqual(as_of(d, "999999", "2020-01-02T12:00:00+09:00"), [])
            before = as_of(d, "999999", "2020-01-03T12:00:00+09:00")
            after = as_of(d, "999999", "2020-01-04T00:00:00+09:00")
            self.assertEqual(before[0]["rcept_no"], "20200102000001")
            self.assertEqual(after[0]["rcept_no"], "20200103000002")
            with self.assertRaises(ValueError):
                as_of(d, "999999", "2020-02-01T00:00:00+09:00")
            self.assertEqual(
                len((Path(d) / "manifest.jsonl").read_text().splitlines()), 2
            )
            for p in Path(d).rglob("*"):
                if p.is_file():
                    self.assertNotIn(KEY.encode(), p.read_bytes())

    def test_later_receipt_mismatch_is_missing_not_backdated(self):
        with tempfile.TemporaryDirectory() as d:
            get, _ = transport(True)

            def stale(endpoint, params, key):
                body = get(endpoint, params, key)
                if endpoint == "fnlttSinglAcntAll.json" and params["fs_div"] == "OFS":
                    body = body.replace(b"20200103000002", b"20200102000001")
                return body

            result = Collector(
                d, "mismatch", "2020-01-01", "2020-01-31", KEY, 10, stale
            ).collect()
            self.assertEqual(result["reports"], [])
            self.assertEqual(
                result["missing"][0]["reason"], "receipt_mismatch_or_empty"
            )

    def test_missing_amendment_invalidates_earlier_value(self):
        with tempfile.TemporaryDirectory() as d:
            original, _ = transport()
            first = Collector(
                d, "first", "2020-01-01", "2020-01-31", KEY, 10, original
            ).collect()
            facts, derived = build_rows(first["reports"])
            meta = {
                "coverage_start": "2020-01-01",
                "coverage_end": "2020-01-31T23:59:59.999999+09:00",
                "fetch_budget_used": 4,
                "missing_reports": [],
            }
            publish(d, "first", facts, derived, meta)
            amended, _ = transport(True)

            def stale(endpoint, params, key):
                body = amended(endpoint, params, key)
                return (
                    body.replace(b"20200103000002", b"20200102000001")
                    if endpoint == "fnlttSinglAcntAll.json"
                    else body
                )

            later = Collector(
                d, "later", "2020-01-01", "2020-01-31", KEY, 10, stale
            ).collect()
            publish(d, "later", [], [], {**meta, "missing_reports": later["missing"]})
            self.assertEqual(len(as_of(d, "999999", "2020-01-03T12:00:00+09:00")), 1)
            self.assertEqual(as_of(d, "999999", "2020-01-04T00:00:00+09:00"), [])

    def test_current_kst_day_is_not_complete_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
            with self.assertRaises(ValueError):
                Collector(d, "today", today, today, KEY, 10)

    def test_secret_echo_and_transport_exception_are_sanitized(self):
        with tempfile.TemporaryDirectory() as d:

            def echo(*args):
                return KEY.encode()

            c = Collector(d, "one", "2020-01-01", "2020-01-31", KEY, 1, echo)
            with self.assertRaises(SourceError) as err:
                c.get("list.json", {})
            self.assertNotIn(KEY, str(err.exception))

            def fail(*args):
                raise RuntimeError(KEY)

            c.transport = fail
            c.budget += 1
            with self.assertRaises(SourceError) as err:
                c.get("list.json", {})
            self.assertNotIn(KEY, str(err.exception))
            for p in Path(d).rglob("*"):
                if p.is_file():
                    self.assertNotIn(KEY.encode(), p.read_bytes())

    def test_cli_fetch_and_build_with_injected_http(self):
        with tempfile.TemporaryDirectory() as d:
            get, _ = transport(True)
            args = [
                "--root",
                d,
                "--key-env",
                "TEST_DART_SYNTHETIC_KEY",
                "--start",
                "2020-01-01",
                "--end",
                "2020-01-31",
                "--run-id",
                "a",
            ]
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"TEST_DART_SYNTHETIC_KEY": KEY}),
                patch("research.kr_fundamentals_pit.collector.http_get", get),
                redirect_stdout(output),
            ):
                # Collector's default transport is bound at definition; inject at construction.
                original = Collector.__init__

                def init(self, *a, **kw):
                    return original(self, *a, **{**kw, "transport": get})

                with patch.object(Collector, "__init__", init):
                    self.assertEqual(cli.main(["fetch", *args, "--budget", "2"]), 0)
                    self.assertEqual(cli.main(["fetch", *args, "--budget", "2"]), 0)
                    self.assertEqual(cli.main(["build", *args]), 0)
            self.assertNotIn(KEY, output.getvalue())
            self.assertEqual(
                json.loads((Path(d) / "manifest.jsonl").read_text())[
                    "fetch_budget_used"
                ],
                4,
            )

    def test_publish_retry_after_mid_failure_with_same_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            meta = {
                "coverage_start": "2020-01-01",
                "coverage_end": "2020-01-31T23:59:59.999999+09:00",
                "fetch_budget_used": 1,
                "missing_reports": [],
            }
            calls = []
            real = os.fsync

            def flaky(fd):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError("simulated disk failure")
                return real(fd)

            with (
                patch("research.kr_fundamentals_pit.store.os.fsync", flaky),
                self.assertRaises(OSError),
            ):
                publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            staging = Path(d) / "partitions" / "run.staging"
            self.assertTrue(staging.exists())
            self.assertFalse((Path(d) / "partitions" / "run").exists())
            self.assertFalse((Path(d) / "manifest.jsonl").exists())
            entry = publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            self.assertFalse(staging.exists())
            self.assertEqual(entry["partitions"]["facts"]["rows"], 1)
            self.assertEqual(
                len((Path(d) / "manifest.jsonl").read_text().splitlines()), 1
            )
            with self.assertRaises(FileExistsError):
                publish(d, "run", [{"a": 1}], [{"b": 2}], meta)

    def test_publish_adopts_orphaned_partition_on_retry(self):
        with tempfile.TemporaryDirectory() as d:
            meta = {
                "coverage_start": "2020-01-01",
                "coverage_end": "2020-01-31T23:59:59.999999+09:00",
                "fetch_budget_used": 1,
                "missing_reports": [],
            }
            real_open = Path.open

            def flaky_open(self, mode="r", *args, **kwargs):
                if self.name == "manifest.jsonl" and "a" in mode:
                    raise OSError("simulated manifest failure")
                return real_open(self, mode, *args, **kwargs)

            # Crash after the atomic rename but before the manifest append.
            with patch.object(Path, "open", flaky_open), self.assertRaises(OSError):
                publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            self.assertTrue((Path(d) / "partitions" / "run").is_dir())
            self.assertFalse((Path(d) / "manifest.jsonl").exists())
            # Retry with the same run ID completes only the missing entry.
            entry = publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            self.assertEqual(entry["run_id"], "run")
            records = (Path(d) / "manifest.jsonl").read_text().splitlines()
            self.assertEqual(len(records), 1)
            # A fully recorded partition still refuses overwrite...
            with self.assertRaises(FileExistsError):
                publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            # ...and a partition whose content does not match stays refused.
            (Path(d) / "partitions" / "other").mkdir(parents=True)
            (Path(d) / "partitions" / "other" / "facts.jsonl").write_bytes(b"?")
            with self.assertRaises(FileExistsError):
                publish(d, "other", [{"a": 1}], [{"b": 2}], meta)

    def test_torn_final_manifest_line_does_not_brick_store(self):
        with tempfile.TemporaryDirectory() as d:
            meta = {
                "coverage_start": "2020-01-01",
                "coverage_end": "2020-01-31T23:59:59.999999+09:00",
                "fetch_budget_used": 1,
                "missing_reports": [],
            }
            facts, derived = build_rows(
                Collector(
                    d, "c1", "2020-01-01", "2020-01-31", KEY, 10, transport()[0]
                ).collect()["reports"]
            )
            publish(d, "c1", facts, derived, meta)
            manifest = Path(d) / "manifest.jsonl"
            with manifest.open("a") as stream:
                stream.write('{"run_id"')
            # Readers still work, and a new run can still publish.
            rows = as_of(d, "999999", "2020-01-03T12:00:00+09:00")
            self.assertEqual(len(rows), 1)
            publish(d, "c2", facts, derived, meta)
            self.assertEqual(len(manifest.read_text().splitlines()), 3)
            # A corrupt middle record is real damage and still raises.
            lines = manifest.read_text().splitlines()
            lines[0] = "{broken"
            manifest.write_text("\n".join(lines) + "\n")
            with self.assertRaises(json.JSONDecodeError):
                as_of(d, "999999", "2020-01-03T12:00:00+09:00")

    def test_lost_partition_dir_is_republished_without_duplicate_entry(self):
        with tempfile.TemporaryDirectory() as d:
            meta = {
                "coverage_start": "2020-01-01",
                "coverage_end": "2020-01-31T23:59:59.999999+09:00",
                "fetch_budget_used": 1,
                "missing_reports": [],
            }
            publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            # Simulate losing the directory entry (crash before parent fsync):
            # the manifest record survives but the partition is gone.
            shutil.rmtree(Path(d) / "partitions" / "run")
            entry = publish(d, "run", [{"a": 1}], [{"b": 2}], meta)
            self.assertEqual(entry["run_id"], "run")
            records = (Path(d) / "manifest.jsonl").read_text().splitlines()
            self.assertEqual(len(records), 1)
            self.assertTrue((Path(d) / "partitions" / "run").is_dir())

    def test_build_tolerates_torn_final_usage_line(self):
        with tempfile.TemporaryDirectory() as d:
            get, _ = transport()
            args = [
                "--root",
                d,
                "--key-env",
                "TEST_DART_SYNTHETIC_KEY",
                "--start",
                "2020-01-01",
                "--end",
                "2020-01-31",
                "--run-id",
                "b",
            ]
            original = Collector.__init__

            def init(self, *a, **kw):
                return original(self, *a, **{**kw, "transport": get})

            with (
                patch.dict("os.environ", {"TEST_DART_SYNTHETIC_KEY": KEY}),
                patch.object(Collector, "__init__", init),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["fetch", *args, "--budget", "9"]), 0)
                usage_path = Path(d) / "cache" / "b" / "usage.jsonl"
                with usage_path.open("a") as stream:
                    stream.write('{"budget_used"')
                self.assertEqual(cli.main(["build", *args]), 0)
            self.assertEqual(
                json.loads((Path(d) / "manifest.jsonl").read_text())[
                    "fetch_budget_used"
                ],
                4,
            )

    def test_source_error_chain_never_carries_key(self):
        def chain(err):
            seen, out = set(), []
            while err is not None and id(err) not in seen:
                seen.add(id(err))
                out.append(err)
                err = err.__context__ or err.__cause__
            return out

        with tempfile.TemporaryDirectory() as d:

            def fail(*args):
                raise RuntimeError(KEY)

            c = Collector(d, "one", "2020-01-01", "2020-01-31", KEY, 1, fail)
            with self.assertRaises(SourceError) as err:
                c.get("list.json", {})
            for exc in chain(err.exception):
                self.assertNotIn(KEY, repr(exc))

            def opener(*args, **kwargs):
                raise RuntimeError(KEY)

            with (
                patch.object(collector.urllib.request, "build_opener", opener),
                self.assertRaises(SourceError) as err,
            ):
                collector.http_get("list.json", {}, KEY)
            for exc in chain(err.exception):
                self.assertNotIn(KEY, repr(exc))

    def test_identical_is_cis_rows_dedupe_only_differing_is_ambiguous(self):
        base = {
            "symbol": "999999",
            "corp_code": "99999999",
            "business_year": "2020",
            "report_code": "11011",
            "period": "2020-12",
            "history": [],
            "rcept_no": "20200102000001",
            "rcept_date": "20200102",
            "amendment": False,
            "statement_basis": "CFS",
        }
        row = {"rcept_no": "20200102000001", "account_id": "ifrs-full_ProfitLoss"}
        same = {
            **base,
            "accounts": [
                {**row, "sj_div": "IS", "thstrm_amount": "1,000", "currency": "KRW"},
                {**row, "sj_div": "CIS", "thstrm_amount": "1,000", "currency": "KRW"},
            ],
        }
        metric = build_rows([same])[1][0]["metrics"]["net_income"]
        self.assertEqual(metric["value"], "1000")
        self.assertIsNone(metric["reason"])
        differ = {
            **base,
            "accounts": [
                {**row, "sj_div": "IS", "thstrm_amount": "1,000", "currency": "KRW"},
                {**row, "sj_div": "CIS", "thstrm_amount": "999", "currency": "KRW"},
            ],
        }
        metric = build_rows([differ])[1][0]["metrics"]["net_income"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["reason"], "ambiguous_account")

    def test_interim_flow_metrics_carry_period_basis_and_cumulative(self):
        base = {
            "symbol": "999999",
            "corp_code": "99999999",
            "business_year": "2020",
            "period": "2020-06",
            "history": [],
            "rcept_no": "20200814000001",
            "rcept_date": "20200814",
            "amendment": False,
            "statement_basis": "CFS",
            "accounts": [
                {
                    "rcept_no": "20200814000001",
                    "account_id": "ifrs-full_Revenue",
                    "sj_div": "IS",
                    "thstrm_amount": "300",
                    "thstrm_add_amount": "700",
                    "currency": "KRW",
                },
                {
                    "rcept_no": "20200814000001",
                    "account_id": "ifrs-full_Assets",
                    "sj_div": "BS",
                    "thstrm_amount": "5000",
                    "currency": "KRW",
                },
            ],
        }
        metrics = build_rows([{**base, "report_code": "11012"}])[1][0]["metrics"]
        self.assertEqual(metrics["revenue"]["period_basis"], "quarter")
        self.assertEqual(metrics["revenue"]["value"], "300")
        self.assertEqual(metrics["revenue"]["cumulative_value"], "700")
        self.assertEqual(metrics["total_assets"]["period_basis"], "instant")
        self.assertIsNone(metrics["total_assets"]["cumulative_value"])
        metrics = build_rows([{**base, "report_code": "11011"}])[1][0]["metrics"]
        self.assertEqual(metrics["revenue"]["period_basis"], "annual")

    def test_cache_files_do_not_count_until_renamed(self):
        with tempfile.TemporaryDirectory() as d:
            get, _ = transport()
            c = Collector(d, "run", "2020-01-01", "2020-01-31", KEY, 1, get)
            c.get("corpCode.xml", {}, binary=True)
            files = list((Path(d) / "cache" / "run").glob("*.cache"))
            self.assertEqual(len(files), 1)
            self.assertFalse(list((Path(d) / "cache" / "run").glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
