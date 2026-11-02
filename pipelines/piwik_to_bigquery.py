"""
Piwik PRO Analytics API -> BigQuery.

Fetches daily sessions, visitors, page views and bounce rate by source/medium
for one Piwik PRO app and replaces the BigQuery table with the full history.

Timezone note: the Piwik PRO API cuts days in the *app's* configured timezone,
which may be a fixed UTC offset rather than a DST-aware zone. "Yesterday" is
therefore computed in that same zone (piwik.timezone), so the request always
ends at the last complete Piwik day regardless of when or where the job runs.

Usage:
    python -m pipelines.piwik_to_bigquery
"""

import os
from datetime import date

import pandas as pd
import requests
from google.cloud import bigquery

from pipelines.common import Config, bigquery_client, load_replace, today_in, yesterday_in

PAGE_SIZE = 10000
TIMEOUT_S = 60

COLUMNS = ["timestamp", "source", "medium", "sessions", "visitors", "page_views", "bounce_rate"]

SCHEMA = [
    bigquery.SchemaField("loaded_date", "DATE"),
    bigquery.SchemaField("date", "DATE"),
    bigquery.SchemaField("source", "STRING"),
    bigquery.SchemaField("medium", "STRING"),
    bigquery.SchemaField("sessions", "INTEGER"),
    bigquery.SchemaField("visitors", "INTEGER"),
    bigquery.SchemaField("page_views", "INTEGER"),
    bigquery.SchemaField("bounce_rate", "FLOAT"),
]


def get_token(host):
    response = requests.post(
        f"https://{host}/auth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": os.environ["PIWIK_CLIENT_ID"],
            "client_secret": os.environ["PIWIK_CLIENT_SECRET"],
        },
        timeout=TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_rows(host, token, app_id, date_from, date_to):
    rows, offset = [], 0

    while True:
        response = requests.post(
            f"https://{host}/api/analytics/v1/query",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "website_id": app_id,
                "date_from": date_from,
                "date_to": date_to,
                "columns": [{"column_id": c} for c in COLUMNS],
                "order_by": [[0, "asc"]],
                "offset": offset,
                "limit": PAGE_SIZE,
            },
            timeout=TIMEOUT_S,
        )
        if not response.ok:
            print(f"Piwik API error {response.status_code}: {response.text}")
            response.raise_for_status()

        page = response.json()["data"]
        rows.extend(page)
        print(f"  offset={offset}: {len(page)} rows (total {len(rows)})")
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def to_dataframe(rows, loaded_date):
    # timestamp is "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"; the date part is enough.
    return pd.DataFrame(
        {
            "loaded_date": pd.Series([loaded_date] * len(rows), dtype="object"),
            "date": pd.Series([date.fromisoformat(str(r[0])[:10]) for r in rows], dtype="object"),
            "source": pd.array([r[1] for r in rows], dtype="string"),
            "medium": pd.array([r[2] for r in rows], dtype="string"),
            "sessions": pd.array([int(r[3]) for r in rows], dtype="Int64"),
            "visitors": pd.array([int(r[4]) for r in rows], dtype="Int64"),
            "page_views": pd.array([int(r[5]) for r in rows], dtype="Int64"),
            "bounce_rate": pd.array([float(r[6]) for r in rows], dtype="float64"),
        }
    )


def main():
    cfg = Config()
    host = cfg.require("piwik.host")
    app_id = cfg.require("piwik.app_id")
    date_from = cfg.require("piwik.date_from")
    date_to = yesterday_in(cfg.require("piwik.timezone")).isoformat()

    print(f"Piwik PRO: app {app_id}, {date_from} -> {date_to}")
    token = get_token(host)
    rows = fetch_rows(host, token, app_id, date_from, date_to)
    if not rows:
        print("No rows returned; table left unchanged.")
        return

    df = to_dataframe(rows, today_in(cfg.require("reporting_timezone")))
    load_replace(bigquery_client(cfg), cfg, cfg.require("bigquery.tables.piwik_sessions"), df, SCHEMA)


if __name__ == "__main__":
    main()
