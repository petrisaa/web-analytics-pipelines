"""
GA4 Data API -> BigQuery.

Two daily reports from the same property:
  sessions   date x host x landing page x session source/medium
  campaigns  date x host x session source/medium/campaign

Attribution is GA4's session-scoped last click (sessionSource / sessionMedium).
hostName is kept as a dimension so the main site and the portal can be
separated downstream. landingPage is used instead of
landingPagePlusQueryString: tracking parameters in the query string multiply
the row count by an order of magnitude without adding analytical value.

Usage:
    python -m pipelines.ga4_to_bigquery sessions
    python -m pipelines.ga4_to_bigquery campaigns
"""

import argparse
import time
from datetime import datetime

import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.api_core.exceptions import ResourceExhausted
from google.cloud import bigquery

from pipelines.common import (
    GA4_SCOPE,
    Config,
    bigquery_client,
    google_credentials,
    load_replace,
    today_in,
)

PAGE_SIZE = 10000
DEFAULT_RETRY_ATTEMPTS = 4
DEFAULT_RETRY_WAIT_SECONDS = 15 * 60

# Each report: GA4 API name -> (BigQuery column, BigQuery type).
REPORTS = {
    "sessions": {
        "table_key": "bigquery.tables.ga4_sessions",
        "dimensions": {
            "date": ("date", "DATE"),
            "hostName": ("host_name", "STRING"),
            "landingPage": ("landing_page", "STRING"),
            "sessionSource": ("source", "STRING"),
            "sessionMedium": ("medium", "STRING"),
        },
        "metrics": {
            "sessions": ("sessions", "INTEGER"),
            "screenPageViews": ("screen_page_views", "INTEGER"),
            "bounceRate": ("bounce_rate", "FLOAT"),
            "totalUsers": ("total_users", "INTEGER"),
            "newUsers": ("new_users", "INTEGER"),
        },
    },
    "campaigns": {
        "table_key": "bigquery.tables.ga4_campaigns",
        "dimensions": {
            "date": ("date", "DATE"),
            "hostName": ("host_name", "STRING"),
            "sessionSource": ("source", "STRING"),
            "sessionMedium": ("medium", "STRING"),
            "sessionCampaignName": ("campaign", "STRING"),
        },
        "metrics": {
            "sessions": ("sessions", "INTEGER"),
            "screenPageViews": ("screen_page_views", "INTEGER"),
            "bounceRate": ("bounce_rate", "FLOAT"),
            "totalUsers": ("total_users", "INTEGER"),
        },
    },
}

PANDAS_DTYPES = {"STRING": "string", "INTEGER": "Int64", "FLOAT": "float64"}


def run_report_with_retry(client, request, attempts, wait_seconds):
    """
    run_report that waits and retries when the token quota is exhausted.
    Only the failing page is retried; pages already fetched are kept.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = client.run_report(request)
        except ResourceExhausted as exc:
            if attempt == attempts:
                print(f"  Quota exhausted (attempt {attempt}/{attempts}), giving up: {exc.message}")
                raise
            print(f"  Quota exhausted (attempt {attempt}/{attempts}), "
                  f"retrying in {wait_seconds} s: {exc.message}")
            time.sleep(wait_seconds)
            continue
        if attempt > 1:
            print(f"  Succeeded on attempt {attempt}/{attempts}")
        return response


def log_quota(quota):
    # consumed = this request, remaining = left after this request
    print(
        "    quota (request / remaining): "
        f"day {quota.tokens_per_day.consumed} / {quota.tokens_per_day.remaining}, "
        f"hour {quota.tokens_per_hour.consumed} / {quota.tokens_per_hour.remaining}, "
        f"project-hour {quota.tokens_per_project_per_hour.consumed} / "
        f"{quota.tokens_per_project_per_hour.remaining}"
    )


def fetch_rows(cfg, report):
    client = BetaAnalyticsDataClient(credentials=google_credentials([GA4_SCOPE]))
    attempts = int(cfg.get("ga4.quota_retry_attempts", DEFAULT_RETRY_ATTEMPTS))
    wait_seconds = int(cfg.get("ga4.quota_retry_wait_seconds", DEFAULT_RETRY_WAIT_SECONDS))
    rows, offset, tokens = [], 0, 0

    while True:
        request = RunReportRequest(
            property=f"properties/{cfg.require('ga4.property_id')}",
            date_ranges=[
                DateRange(start_date=cfg.require("ga4.date_from"), end_date=cfg.require("ga4.date_to"))
            ],
            dimensions=[Dimension(name=n) for n in report["dimensions"]],
            metrics=[Metric(name=n) for n in report["metrics"]],
            offset=offset,
            limit=PAGE_SIZE,
            return_property_quota=True,
        )
        response = run_report_with_retry(client, request, attempts, wait_seconds)
        rows.extend(response.rows)
        print(f"  offset={offset}: {len(response.rows)} rows ({len(rows)} / {response.row_count})")
        log_quota(response.property_quota)
        tokens += response.property_quota.tokens_per_day.consumed
        if len(response.rows) < PAGE_SIZE:
            print(f"  Run quota usage: {tokens} tokens")
            return rows
        offset += PAGE_SIZE


def to_dataframe(rows, report, loaded_date):
    columns = {"loaded_date": pd.Series([loaded_date] * len(rows), dtype="object")}

    for i, (col, bq_type) in enumerate(report["dimensions"].values()):
        values = [r.dimension_values[i].value for r in rows]
        if bq_type == "DATE":
            columns[col] = pd.Series(
                [datetime.strptime(v, "%Y%m%d").date() for v in values], dtype="object"
            )
        else:
            # GA4 literals such as (direct), (none), (not set) are kept as-is.
            columns[col] = pd.array(values, dtype="string")

    for i, (col, bq_type) in enumerate(report["metrics"].values()):
        cast = float if bq_type == "FLOAT" else int
        columns[col] = pd.array(
            [cast(r.metric_values[i].value) for r in rows], dtype=PANDAS_DTYPES[bq_type]
        )

    return pd.DataFrame(columns)


def schema_for(report):
    fields = [("loaded_date", "DATE"), *report["dimensions"].values(), *report["metrics"].values()]
    return [bigquery.SchemaField(name, bq_type) for name, bq_type in fields]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", choices=sorted(REPORTS))
    args = parser.parse_args()

    cfg = Config()
    report = REPORTS[args.report]
    table = cfg.require(report["table_key"])

    print(
        f"GA4 {args.report}: property {cfg.require('ga4.property_id')}, "
        f"{cfg.require('ga4.date_from')} -> {cfg.require('ga4.date_to')}"
    )
    rows = fetch_rows(cfg, report)
    if not rows:
        # Never truncate the table with an empty result.
        print("No rows returned; table left unchanged.")
        return

    df = to_dataframe(rows, report, today_in(cfg.require("reporting_timezone")))
    load_replace(bigquery_client(cfg), cfg, table, df, schema_for(report))


if __name__ == "__main__":
    main()
