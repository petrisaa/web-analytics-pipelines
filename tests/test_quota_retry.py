"""
GA4 quota handling: retry on ResourceExhausted, quota logging and the retry
settings from config. time.sleep is replaced, so the tests never wait, and the
GA4 client is a fake: no network calls.
"""

import pytest
from google.analytics.data_v1beta.types import (
    PropertyQuota,
    QuotaStatus,
    Row,
    RunReportRequest,
    RunReportResponse,
)
from google.api_core.exceptions import ResourceExhausted

from pipelines import common, ga4_to_bigquery

QUOTA_ERROR = ResourceExhausted("Exhausted property tokens for a project per hour")


def quota(consumed):
    return PropertyQuota(
        tokens_per_day=QuotaStatus(consumed=consumed, remaining=200_000 - consumed),
        tokens_per_hour=QuotaStatus(consumed=consumed, remaining=40_000 - consumed),
        tokens_per_project_per_hour=QuotaStatus(consumed=consumed, remaining=14_000 - consumed),
    )


class FakeClient:
    """Plays back a scripted sequence of responses and exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def run_report(self, request):
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(ga4_to_bigquery.time, "sleep", calls.append)
    return calls


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    for env_name in common.ENV_OVERRIDES:
        monkeypatch.delenv(env_name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "ga4: {property_id: '123', date_from: '2024-01-01', date_to: yesterday}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(path))
    return path


# --- run_report_with_retry ------------------------------------------------------

def test_succeeds_after_retries(sleeps, capsys):
    ok = RunReportResponse(row_count=0)
    client = FakeClient([QUOTA_ERROR, QUOTA_ERROR, ok])

    result = ga4_to_bigquery.run_report_with_retry(client, RunReportRequest(), attempts=4, wait_seconds=900)

    assert result is ok
    assert len(client.requests) == 3
    assert sleeps == [900, 900]
    out = capsys.readouterr().out
    assert "attempt 1/4" in out and "attempt 2/4" in out
    assert "Succeeded on attempt 3/4" in out


def test_gives_up_after_max_attempts(sleeps, capsys):
    client = FakeClient([QUOTA_ERROR] * 4)

    with pytest.raises(ResourceExhausted):
        ga4_to_bigquery.run_report_with_retry(client, RunReportRequest(), attempts=4, wait_seconds=900)

    assert len(client.requests) == 4
    assert sleeps == [900, 900, 900]  # no wait after the last attempt
    assert "attempt 4/4), giving up" in capsys.readouterr().out


def test_no_retry_on_success(sleeps):
    client = FakeClient([RunReportResponse(row_count=0)])
    ga4_to_bigquery.run_report_with_retry(client, RunReportRequest(), attempts=4, wait_seconds=900)
    assert len(client.requests) == 1
    assert sleeps == []


def test_other_errors_are_not_retried(sleeps):
    client = FakeClient([ValueError("boom")])
    with pytest.raises(ValueError):
        ga4_to_bigquery.run_report_with_retry(client, RunReportRequest(), attempts=4, wait_seconds=900)
    assert sleeps == []


# --- fetch_rows: quota request, logging and settings ------------------------------

def page(n_rows, consumed):
    return RunReportResponse(rows=[Row()] * n_rows, row_count=n_rows, property_quota=quota(consumed))


def install_fake(monkeypatch, script):
    client = FakeClient(script)
    monkeypatch.setattr(ga4_to_bigquery, "BetaAnalyticsDataClient", lambda credentials=None: client)
    monkeypatch.setattr(ga4_to_bigquery, "google_credentials", lambda scopes: None)
    return client


def test_fetch_requests_and_logs_quota(config_file, monkeypatch, sleeps, capsys):
    monkeypatch.setattr(ga4_to_bigquery, "PAGE_SIZE", 2)  # two pages: 2 rows + 1 row
    client = install_fake(monkeypatch, [page(2, 28), page(1, 30)])

    rows = ga4_to_bigquery.fetch_rows(common.Config(), ga4_to_bigquery.REPORTS["sessions"])

    assert len(rows) == 3
    assert all(r.return_property_quota for r in client.requests)
    out = capsys.readouterr().out
    assert "project-hour 28 / 13972" in out
    assert "Run quota usage: 58 tokens" in out


def test_fetch_uses_default_retry_settings(config_file, monkeypatch, sleeps):
    install_fake(monkeypatch, [QUOTA_ERROR, page(0, 10)])
    ga4_to_bigquery.fetch_rows(common.Config(), ga4_to_bigquery.REPORTS["campaigns"])
    assert sleeps == [ga4_to_bigquery.DEFAULT_RETRY_WAIT_SECONDS]


def test_fetch_retry_settings_from_env(config_file, monkeypatch, sleeps):
    monkeypatch.setenv("GA4_QUOTA_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("GA4_QUOTA_RETRY_WAIT_SECONDS", "5")
    install_fake(monkeypatch, [QUOTA_ERROR, QUOTA_ERROR])

    with pytest.raises(ResourceExhausted):
        ga4_to_bigquery.fetch_rows(common.Config(), ga4_to_bigquery.REPORTS["sessions"])

    assert sleeps == [5]
