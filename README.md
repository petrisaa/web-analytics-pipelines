# Web analytics pipelines: GA4 + Piwik PRO → BigQuery

<!-- Badge URLs contain the repository name: update both links if the repo is renamed. -->
[![tests](https://github.com/petrisaa/web-analytics-pipelines/actions/workflows/tests.yml/badge.svg)](https://github.com/petrisaa/web-analytics-pipelines/actions/workflows/tests.yml)

Daily pipelines that pull traffic and campaign data from two parallel
measurement systems, Google Analytics 4 (consent-based) and Piwik PRO
(cookieless), into BigQuery, plus the SQL that turns the GA4 raw export into a
cross-host sign-up funnel and keeps the whole thing honest with freshness and
sanity checks.

This is a portfolio version of a production project. All identifiers
(projects, properties, hosts, event names) are placeholders, and the data in
[data/sample/](data/sample/) is synthetic.

---

## Problem

Measuring the return on a media budget requires three things in one place:
what was spent, what traffic it bought, and what that traffic did. Spend lived
in spreadsheets, outcomes in a CRM, and traffic in two analytics tools that
did not agree with each other.

Two specific questions drove the work:

1. **How much does consent-based measurement under-count traffic, and does the
   gap depend on the channel?** If it does, a single correction factor applied
   to GA4 totals is wrong, and channel-level budget decisions made on raw GA4
   numbers are biased.
2. **Where do people drop out of the sign-up flow?** The flow starts on the
   public site and continues on a separate portal host, so a standard GA4
   funnel report does not show it end to end.

## Architecture

```
GA4 Data API                 Piwik PRO Analytics API        GA4 BigQuery export
(sessions, campaigns)        (sessions by source/medium)    (raw events_* tables)
       │                              │                              │
       │ google-analytics-data        │ REST + OAuth2                │
       ▼                              ▼                              │
pipelines/ga4_to_bigquery.py   pipelines/piwik_to_bigquery.py        │
       │                              │                              │
       └──────────────┬───────────────┘                              │
                      ▼                                              ▼
              BigQuery dataset ◄──────────── sql/signup_funnel.sql (view)
                      │                      sql/channel_grouping.sql (view)
                      │
                      ├── sql/freshness_check.sql  (scheduled query, fails loudly)
                      ├── sql/quality_checks.sql   (sanity checks)
                      ▼
              BI tool (Power BI)
```

**Loaders.** Each loader fetches the full date range, paginates through the
API and replaces its table with `WRITE_TRUNCATE`. A run is therefore
idempotent: re-running a job, or running it twice, cannot create duplicates.
The trade-off is that `loaded_date` is identical on every row and no load
history is kept.

**GA4 quota.** The GA4 Data API charges tokens per request against daily,
hourly and per-project hourly limits. Every request asks for its quota status
(`return_property_quota`). The loader logs each page's cost and the remaining
tokens, and at the end the run's total ("Run quota usage"). When the quota
runs out (`429 ResourceExhausted`), only the failing page is retried, by
default up to 4 attempts 15 minutes apart (`ga4.quota_retry_*` in
[config.yaml](config.yaml)). Because each run re-fetches the whole window, the
start date (`ga4.date_from`) is configurable and should be no earlier than the
reports need.

**Time zones.** "Yesterday" is resolved in the source system's own time zone,
not the runner's. The GA4 API's relative `yesterday` is evaluated in the
property's zone. The Piwik PRO app may be configured with a fixed UTC offset
that ignores daylight saving time, so the loader computes yesterday in that
exact offset (`piwik.timezone`). `loaded_date` uses the reporting zone, the
same one the freshness check compares against.

**Sign-up funnel.** Built on the raw GA4 export rather than the Data API,
because it needs event parameters and per-session ordering. Four steps across
two hosts; submission is inferred from the shape of the form's own events.
Traffic source is assigned once per session (cross-channel last click first,
then manual UTM fields) so a session never splits across channels between
steps. Details are in the header of [sql/signup_funnel.sql](sql/signup_funnel.sql).

**Monitoring.** [sql/freshness_check.sql](sql/freshness_check.sql) runs as a
BigQuery scheduled query and calls `ERROR()` when a table was not reloaded
today, does not reach yesterday, or when the GA4 export's daily table is
missing. A log-based alert turns the failure into a notification. This catches
jobs that never ran, which no job-level error handling can.

| Path | What it is |
|---|---|
| [pipelines/ga4_to_bigquery.py](pipelines/ga4_to_bigquery.py) | GA4 Data API → BigQuery (`sessions` and `campaigns` reports) |
| [pipelines/piwik_to_bigquery.py](pipelines/piwik_to_bigquery.py) | Piwik PRO Analytics API → BigQuery |
| [pipelines/common.py](pipelines/common.py) | Config, credentials, time zones, idempotent load |
| [sql/signup_funnel.sql](sql/signup_funnel.sql) | Four-step cross-host funnel view on the GA4 export |
| [sql/channel_grouping.sql](sql/channel_grouping.sql) | Example source/medium → channel mapping for both systems |
| [sql/freshness_check.sql](sql/freshness_check.sql) | Scheduled freshness check that fails with `ERROR()` |
| [sql/quality_checks.sql](sql/quality_checks.sql) | Sanity checks: sessions > page views, bot days, `(not set)` spikes |
| [.github/workflows/](.github/workflows/) | One workflow per loader, manual trigger only |
| [scripts/generate_sample_data.py](scripts/generate_sample_data.py) | Synthetic data with the same columns as the tables |

## Key findings

### 1. The pipeline was measuring the wrong website

The first version of the analytics was built on a GA4 property that,
as it turned out, tracked a different part of the organisation's web
presence, not the main site. Nothing was technically broken: the
pipeline ran, the data was real and the numbers looked plausible. I
found it only by comparing volumes against what the site should
produce. Lesson: a pipeline can be technically flawless and still
answer the wrong question. Validate against expectations, not just
against error logs.

### 2. Consent-based analytics undercounts traffic unevenly by channel

I ran a cookieless analytics tool in parallel with GA4 and compared
them channel by channel. Coverage differed clearly between channels:
The two search channels converged independently on almost the same ratio, 
which supports the method, while paid social coverage was far lower and 
flagged as unverified until it could be checked against ad platform click data. 
Conclusion: a single correction factor applied to GA4 totals would be wrong, 
because the gap depends on how users arrive.

### 3. A tag change silently broke the second tool for months

In the cookieless tool, sessions were inflated several times over
for a long period. The signal was a physically impossible ratio: 
more sessions than page views. The cause was a tag configuration 
change made for the other analytics system, so changing one tool 
broke the other. Because the error happened at collection, 
the history could not be repaired. I documented a “reliable 
from” date and added sanity checks that would have caught it on day one.

### 4. Customer lifetime value was overestimated about 2×

The original customer lifetime value was based on the average churn 
of the whole customer base. Cohort analysis showed that new customers 
churn fastest in their first year and behave differently from the 
existing base, so the original estimate was clearly too optimistic. 
Recalculating from cohort retention lowered the return-on-acquisition 
estimate considerably.

### 5. Paid search was being reported as organic

The session source logic preferred the manual campaign field over the
cross-channel one, so ad clicks were logged as organic search. Fixing
the field priority moved that traffic to the correct channel.

### 6. Built so that someone else can run it

Before handover I mapped every dependency on personal accounts: cloud
project, billing, repository and the project that silently ran the BI
tool's queries. I also found that the date logic depended on the
machine's local time zone, so the same script fetched different days
depending on where it ran. The fetch window also ended today instead of
yesterday, so the last row in every table was an incomplete day. I
measured 145 scheduled runs (consistently
4–6 hours late) and set the monitoring schedule from that data instead
of guessing. Monitoring runs outside the CI system, so it also detects
runs that never started. The BI model's member table was cut from 96
columns to the 4 actually used, to keep unnecessary personal data out.

## My role and AI-assisted development

I built this solution together with Claude Code (Anthropic's AI coding
assistant). The division of work was clear:

**My part:** defining the problems and requirements, making the
architecture and data decisions, agreeing on business definitions with
the people who use the reports (for example, which membership changes
count as churn), and validating every result against the source
systems and business logic. Several of the findings above started from
me questioning a number that did not fit. I also caught the assistant's
own mistakes, including incorrect diagnoses and overstated causal
claims.

**Claude Code's part:** writing most of the Python and SQL, running
read-only investigations, testing changes and drafting documentation.

**How we worked:** small steps, read-only investigation before any
change, and a reviewed diff before every commit.

What I learned: AI makes implementation fast. The value I add is in
asking the right questions and verifying the answers.

## Lessons learned

**A technically flawless pipeline can deliver completely wrong data.** Two of
the most consequential errors in the original project were invisible in the
code. In one, the pipeline read a perfectly valid analytics property that
measured a different site; everything ran, the numbers were plausible, they
just described the wrong thing. In the other, the source system's visitor
identification broke at collection time and every hit became a new session.
The loader stored exactly what the API returned. The error surfaced only when
sessions exceeded page views, which is physically impossible. Since then,
every new source gets sanity checks that do not depend on the code being
correct ([sql/quality_checks.sql](sql/quality_checks.sql)).

**Two measurement systems are not independent.** A tagging change made for
one tool broke measurement in the other for weeks, and caused double counting
in the first for two days. A change in one system can silently break another,
and it shows only when someone puts both sets of numbers in the same table.

**A tool's own diagnostics can be wrong.** A tag coverage report claimed the
sign-up pages were untagged. A browser DevTools check showed the tag firing;
the crawler behind the report simply could not see a tag injected by a
JavaScript application. Trusting the report would have led to the wrong
explanation for the funnel's low conversion.

**Never normalise analytics literals away.** `(direct)`, `(none)`,
`(not set)` and `(data not available)` carry meaning. Filtering or
"cleaning" them causes silent misattribution.

**Attribution fields are not interchangeable.** In the GA4 export,
`manual_campaign` is filled only from UTM tags. Ads auto-tagging leaves it
empty, so paid search quietly showed up as organic until the funnel switched
to `cross_channel_campaign` first.

**Not every metric can be summed.** Users and visitors are not additive across
source/medium rows, and bounce rate is defined differently in GA4 and Piwik
PRO, so it cannot be compared between them.

**Silence is a failure mode.** Scheduled CI jobs can start hours late or not at
all. Monitoring that lives outside the job, checks the data itself, and fails
loudly is what makes "nothing happened" visible.

## Tech stack

| Technology | Used for |
|---|---|
| Python 3.11+ | Extraction and transformation |
| GA4 Data API, GA4 BigQuery export | Consent-based analytics source |
| Piwik PRO Analytics API (OAuth2 client credentials) | Cookieless parallel measurement |
| Google BigQuery | Warehouse, views, scheduled queries |
| Cloud Logging / Cloud Monitoring | Alerting on failed freshness checks |
| GitHub Actions | Running the loaders |
| Power BI | Reporting (not included in this repository) |
| Claude Code | AI-assisted development |

## Setup

> **About the sample data.** Everything in [data/sample/](data/sample/) is
> synthetic, generated by
> [scripts/generate_sample_data.py](scripts/generate_sample_data.py). All
> figures in it are invented, including the GA4-vs-Piwik coverage factors per
> channel. They show the shape of the tables, not real results.

### Requirements

- Python 3.11+
- A GCP project with BigQuery enabled, and a service account with
  *BigQuery Data Editor* and *BigQuery Job User*
- For GA4: the service account added as a *Viewer* on the GA4 property; for
  the funnel, the GA4 BigQuery export linked to the project
- For Piwik PRO: API client credentials

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

Edit [config.yaml](config.yaml). Every value can also be set as an environment
variable (the variable name is in the comment next to each key; environment
variables win). Secrets come from the environment only:

```bash
cp .env.example .env           # then fill in PIWIK_CLIENT_ID / PIWIK_CLIENT_SECRET
gcloud auth application-default login
```

Google authentication is tried in this order: `GCP_SERVICE_ACCOUNT_KEY`
(JSON content, for CI), `GOOGLE_APPLICATION_CREDENTIALS` (key file path),
Application Default Credentials.

### 3. Run the loaders

```bash
python -m pipelines.ga4_to_bigquery sessions
python -m pipelines.ga4_to_bigquery campaigns
python -m pipelines.piwik_to_bigquery
```

Each run replaces its target table. For a test run against a separate table
and a short range, override the table and dates:

```bash
GA4_SESSIONS_TABLE=ga4_sessions_test GA4_DATE_FROM=2026-01-01 GA4_DATE_TO=2026-01-07 \
  python -m pipelines.ga4_to_bigquery sessions
```

### 4. Create the views and checks

Replace `my-gcp-project`, `web_analytics` and `analytics_123456789` in the
files under [sql/](sql/), then:

```bash
bq query --use_legacy_sql=false --location=EU < sql/signup_funnel.sql
bq query --use_legacy_sql=false --location=EU < sql/channel_grouping.sql
bq query --use_legacy_sql=false --location=EU < sql/freshness_check.sql   # prints OK or fails
```

Schedule `freshness_check.sql` as a BigQuery scheduled query a few hours after
the loaders, and create a Cloud Monitoring log-based alert on
`resource.type="bigquery_dts_config" AND severity>=ERROR`.

### 5. GitHub Actions

The workflows in [.github/workflows/](.github/workflows/) run on
`workflow_dispatch` only. Add a `schedule:` trigger for nightly runs.

| Kind | Name |
|---|---|
| Secret | `GCP_SERVICE_ACCOUNT_KEY`, `PIWIK_CLIENT_ID`, `PIWIK_CLIENT_SECRET` |
| Variable | `GCP_PROJECT`, `BQ_DATASET`, `GA4_PROPERTY_ID`, `PIWIK_HOST`, `PIWIK_APP_ID`, `PIWIK_TIMEZONE` |

Unset variables fall back to [config.yaml](config.yaml).

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests cover the date logic (yesterday per source time zone, fixed
offsets, `loaded_date` in the reporting zone, runner and target zone on
different calendar days) with a frozen clock and mocked API clients. They make
no network calls.

### Sample data

```bash
python scripts/generate_sample_data.py        # writes data/sample/*.csv
```

Standard library only, deterministic. The CSVs have the same columns as the
BigQuery tables and include a crawler day and a `(not set)` spike for the
quality checks to catch. Load them into BigQuery with `bq load --autodetect`
to try the SQL without access to real sources.
