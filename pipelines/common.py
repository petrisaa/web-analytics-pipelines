"""
Shared helpers for the GA4 and Piwik PRO loaders: configuration, Google
credentials, date handling and the idempotent BigQuery load.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import google.auth
import pandas as pd
import yaml
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

load_dotenv()

BQ_SCOPE = "https://www.googleapis.com/auth/bigquery"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

# Environment variable -> dotted path in config.yaml. Env vars take precedence.
ENV_OVERRIDES = {
    "GCP_PROJECT": "gcp.project",
    "BQ_LOCATION": "gcp.location",
    "BQ_DATASET": "bigquery.dataset",
    "GA4_SESSIONS_TABLE": "bigquery.tables.ga4_sessions",
    "GA4_CAMPAIGNS_TABLE": "bigquery.tables.ga4_campaigns",
    "PIWIK_TABLE": "bigquery.tables.piwik_sessions",
    "REPORTING_TIMEZONE": "reporting_timezone",
    "MAIN_HOST": "sites.main_host",
    "PORTAL_HOST": "sites.portal_host",
    "GA4_PROPERTY_ID": "ga4.property_id",
    "GA4_DATE_FROM": "ga4.date_from",
    "GA4_DATE_TO": "ga4.date_to",
    "GA4_QUOTA_RETRY_ATTEMPTS": "ga4.quota_retry_attempts",
    "GA4_QUOTA_RETRY_WAIT_SECONDS": "ga4.quota_retry_wait_seconds",
    "PIWIK_HOST": "piwik.host",
    "PIWIK_APP_ID": "piwik.app_id",
    "PIWIK_DATE_FROM": "piwik.date_from",
    "PIWIK_TIMEZONE": "piwik.timezone",
}


class Config:
    """config.yaml with environment variable overrides, read via dotted keys."""

    def __init__(self, path=None):
        path = Path(path or os.environ.get("CONFIG_PATH", "config.yaml"))
        self._data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        for env_name, key in ENV_OVERRIDES.items():
            value = os.environ.get(env_name)
            if value:
                self._set(key, value)

    def _set(self, key, value):
        node = self._data
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value

    def get(self, key, default=None):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, key):
        value = self.get(key)
        if value in (None, ""):
            env = next((e for e, k in ENV_OVERRIDES.items() if k == key), None)
            raise SystemExit(f"Missing config value '{key}' (config.yaml or env {env}).")
        return str(value)


def parse_timezone(value) -> tzinfo:
    """Accept an IANA name ("Europe/Helsinki") or a fixed offset ("+03:00")."""
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", str(value).strip())
    if match:
        sign = 1 if match.group(1) == "+" else -1
        offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
        return timezone(sign * offset)
    return ZoneInfo(str(value))


def today_in(tz_value):
    return datetime.now(parse_timezone(tz_value)).date()


def yesterday_in(tz_value):
    """Last complete day in the given zone, independent of the runner's clock zone."""
    return today_in(tz_value) - timedelta(days=1)


def google_credentials(scopes):
    """Service account JSON content (CI) > key file path (local) > ADC."""
    key_json = os.environ.get("GCP_SERVICE_ACCOUNT_KEY")
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    if key_json:
        print("Auth: GCP_SERVICE_ACCOUNT_KEY")
        return service_account.Credentials.from_service_account_info(
            json.loads(key_json), scopes=scopes
        )
    if key_file:
        print("Auth: GOOGLE_APPLICATION_CREDENTIALS")
        return service_account.Credentials.from_service_account_file(key_file, scopes=scopes)
    print("Auth: Application Default Credentials")
    credentials, _ = google.auth.default(scopes=scopes)
    return credentials


def bigquery_client(cfg: Config):
    return bigquery.Client(
        project=cfg.require("gcp.project"),
        credentials=google_credentials([BQ_SCOPE]),
    )


def ensure_dataset(client, cfg: Config):
    dataset = bigquery.Dataset(f"{cfg.require('gcp.project')}.{cfg.require('bigquery.dataset')}")
    dataset.location = cfg.get("gcp.location", "EU")
    client.create_dataset(dataset, exists_ok=True)


def load_replace(client, cfg: Config, table, df: pd.DataFrame, schema):
    """
    Idempotent load: WRITE_TRUNCATE replaces the whole table with the full
    date range fetched in this run. Re-running a job (or running it twice) can
    never create duplicates. Trade-off: loaded_date is the same on every row,
    so the table keeps no history of earlier loads.
    """
    ensure_dataset(client, cfg)
    table_ref = f"{cfg.require('gcp.project')}.{cfg.require('bigquery.dataset')}.{table}"
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    client.load_table_from_dataframe(df, table_ref, job_config=job_config).result()
    print(f"Loaded {len(df)} rows into {table_ref}.")
