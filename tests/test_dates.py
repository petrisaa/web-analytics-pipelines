"""
Date logic tests: "yesterday" per source time zone, fixed offsets, loaded_date
in the reporting zone, and runs where the runner's local date differs from the
target zone's date.

The clock is frozen by replacing `pipelines.common.datetime` with a subclass
whose now() returns a fixed instant. API and BigQuery clients are mocked; no
network calls are made.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from google.analytics.data_v1beta.types import RunReportResponse

from pipelines import common, ga4_to_bigquery, piwik_to_bigquery
from pipelines.common import parse_timezone, today_in, yesterday_in

RUNNER_TZ = ZoneInfo("America/Los_Angeles")  # a runner far from the target zones


@pytest.fixture
def freeze(monkeypatch):
    """freeze(utc_datetime) pins the clock to that instant."""

    def _freeze(instant_utc):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    # Naive local time on the (simulated) runner.
                    return instant_utc.astimezone(RUNNER_TZ).replace(tzinfo=None)
                return instant_utc.astimezone(tz)

        monkeypatch.setattr(common, "datetime", FrozenDatetime)
        return FrozenDatetime

    return _freeze


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Isolated config.yaml; clears any env overrides from the outer shell."""
    for env_name in common.ENV_OVERRIDES:
        monkeypatch.delenv(env_name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "gcp: {project: test-project, location: EU}\n"
        "bigquery:\n"
        "  dataset: test_dataset\n"
        "  tables: {ga4_sessions: ga4_s, ga4_campaigns: ga4_c, piwik_sessions: piwik_s}\n"
        "reporting_timezone: Europe/Helsinki\n"
        "ga4: {property_id: '123', date_from: '2026-01-01', date_to: yesterday}\n"
        "piwik: {host: piwik.test, app_id: app-1, date_from: '2026-01-01', timezone: '+03:00'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(path))
    return path


# --- parse_timezone -----------------------------------------------------------

@pytest.mark.parametrize(
    "value, offset",
    [("+03:00", timedelta(hours=3)), ("+0300", timedelta(hours=3)),
     ("-05:30", -timedelta(hours=5, minutes=30)), ("+00:00", timedelta(0))],
)
def test_fixed_offsets(value, offset):
    assert parse_timezone(value).utcoffset(None) == offset


def test_iana_name():
    assert parse_timezone("Europe/Helsinki") == ZoneInfo("Europe/Helsinki")


# --- yesterday in the source zone ----------------------------------------------

def test_yesterday_in_helsinki(freeze):
    freeze(utc(2026, 6, 15, 12, 0))
    assert yesterday_in("Europe/Helsinki") == date(2026, 6, 14)


def test_runner_and_target_on_different_days(freeze):
    # 22:30 UTC on 29 Mar 2026 = 01:30 on 30 Mar in Helsinki (EEST, UTC+3),
    # while a runner in Los Angeles is still on 29 Mar (15:30 PDT).
    frozen = freeze(utc(2026, 3, 29, 22, 30))
    assert frozen.now().date() == date(2026, 3, 29)  # runner's local date
    assert today_in("Europe/Helsinki") == date(2026, 3, 30)
    assert yesterday_in("Europe/Helsinki") == date(2026, 3, 29)


def test_runner_ahead_of_target(freeze):
    # 06:00 UTC on 1 Jan: Helsinki is already 1 Jan, Los Angeles still 31 Dec.
    freeze(utc(2026, 1, 1, 6, 0))
    assert yesterday_in("Europe/Helsinki") == date(2025, 12, 31)
    assert yesterday_in("America/Los_Angeles") == date(2025, 12, 30)


def test_fixed_offset_ignores_dst(freeze):
    # Winter, 21:30 UTC: a fixed +03:00 app is already past midnight, while
    # Europe/Helsinki (UTC+2 in winter) is not. Using the DST-aware zone for a
    # fixed-offset app would request one day too few.
    freeze(utc(2026, 1, 15, 21, 30))
    assert yesterday_in("+03:00") == date(2026, 1, 15)
    assert yesterday_in("Europe/Helsinki") == date(2026, 1, 14)


def test_fixed_offset_equals_dst_zone_in_summer(freeze):
    # In summer Helsinki is UTC+3, so the two agree.
    freeze(utc(2026, 7, 1, 21, 30))
    assert yesterday_in("+03:00") == yesterday_in("Europe/Helsinki") == date(2026, 7, 1)


# --- Piwik loader: date_to and loaded_date --------------------------------------

def test_piwik_main_uses_piwik_zone_and_reporting_zone(freeze, config_file, monkeypatch):
    # 21:30 UTC on 15 Jan: Piwik app (+03:00) is on 16 Jan, Helsinki on 15 Jan.
    freeze(utc(2026, 1, 15, 21, 30))
    captured = {}

    monkeypatch.setattr(piwik_to_bigquery, "get_token", lambda host: "token")

    def fake_fetch(host, token, app_id, date_from, date_to):
        captured["date_to"] = date_to
        return [["2026-01-15", "google", "organic", 10, 9, 20, 0.5]]

    def fake_load(client, cfg, table, df, schema):
        captured["table"] = table
        captured["loaded_date"] = df["loaded_date"].iloc[0]

    monkeypatch.setattr(piwik_to_bigquery, "fetch_rows", fake_fetch)
    monkeypatch.setattr(piwik_to_bigquery, "bigquery_client", lambda cfg: object())
    monkeypatch.setattr(piwik_to_bigquery, "load_replace", fake_load)

    piwik_to_bigquery.main()

    assert captured["date_to"] == "2026-01-15"           # yesterday in +03:00
    assert captured["loaded_date"] == date(2026, 1, 15)  # today in Europe/Helsinki
    assert captured["table"] == "piwik_s"


def test_piwik_timezone_env_override(freeze, config_file, monkeypatch):
    freeze(utc(2026, 1, 15, 21, 30))
    monkeypatch.setenv("PIWIK_TIMEZONE", "Europe/Helsinki")
    captured = {}
    monkeypatch.setattr(piwik_to_bigquery, "get_token", lambda host: "token")

    def fake_fetch(host, token, app_id, date_from, date_to):
        captured["date_to"] = date_to
        return []  # empty result: nothing is loaded

    monkeypatch.setattr(piwik_to_bigquery, "fetch_rows", fake_fetch)
    piwik_to_bigquery.main()
    assert captured["date_to"] == "2026-01-14"


# --- GA4 loader: "yesterday" is resolved by GA4 in the property's zone ----------

class FakeGA4Client:
    requests = []

    def __init__(self, credentials=None):
        pass

    def run_report(self, request):
        FakeGA4Client.requests.append(request)
        return RunReportResponse(row_count=0)


def test_ga4_passes_relative_yesterday_through(config_file, monkeypatch):
    # The GA4 API evaluates "yesterday" in the property's own time zone, so the
    # loader must send the literal value instead of a date computed locally.
    FakeGA4Client.requests = []
    monkeypatch.setattr(ga4_to_bigquery, "BetaAnalyticsDataClient", FakeGA4Client)
    monkeypatch.setattr(ga4_to_bigquery, "google_credentials", lambda scopes: None)

    ga4_to_bigquery.fetch_rows(common.Config(), ga4_to_bigquery.REPORTS["sessions"])

    date_range = FakeGA4Client.requests[0].date_ranges[0]
    assert date_range.end_date == "yesterday"
    assert date_range.start_date == "2026-01-01"


def test_ga4_loaded_date_in_reporting_zone(freeze, config_file, monkeypatch):
    # 22:30 UTC on 29 Mar: Helsinki is on 30 Mar, the runner on 29 Mar.
    freeze(utc(2026, 3, 29, 22, 30))

    class Value:
        def __init__(self, value):
            self.value = value

    class Row:
        dimension_values = [Value(v) for v in ("20260329", "example.com", "/", "google", "organic")]
        metric_values = [Value(v) for v in ("10", "20", "0.5", "9", "4")]

    captured = {}
    monkeypatch.setattr("sys.argv", ["ga4_to_bigquery", "sessions"])
    monkeypatch.setattr(ga4_to_bigquery, "fetch_rows", lambda cfg, report: [Row()])
    monkeypatch.setattr(ga4_to_bigquery, "bigquery_client", lambda cfg: object())
    monkeypatch.setattr(
        ga4_to_bigquery, "load_replace",
        lambda client, cfg, table, df, schema: captured.update(
            loaded_date=df["loaded_date"].iloc[0], date=df["date"].iloc[0], table=table
        ),
    )

    ga4_to_bigquery.main()

    assert captured["loaded_date"] == date(2026, 3, 30)
    assert captured["date"] == date(2026, 3, 29)
    assert captured["table"] == "ga4_s"


def test_empty_result_does_not_load(config_file, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ga4_to_bigquery", "campaigns"])
    monkeypatch.setattr(ga4_to_bigquery, "fetch_rows", lambda cfg, report: [])

    def fail(*args):
        raise AssertionError("load_replace must not run on an empty result")

    monkeypatch.setattr(ga4_to_bigquery, "load_replace", fail)
    ga4_to_bigquery.main()
