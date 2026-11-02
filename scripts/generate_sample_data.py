"""
Generate synthetic sample data with the same columns as the BigQuery tables.

Nothing here comes from a real site. Volumes, channel mix and the
GA4-vs-Piwik coverage gap are invented, but shaped so that the SQL checks in
sql/quality_checks.sql have something to find:

  * a weekly rhythm (weekday traffic higher than weekend traffic)
  * GA4 sees fewer sessions than Piwik PRO, by a channel-dependent factor
    (consent-based vs cookieless measurement)
  * one crawler day in Piwik (direct traffic spike, ~1 page per session)
  * one GA4 day where a tagging glitch dumps traffic into (not set)/(not set)

Standard library only, deterministic (fixed seed).

Usage:
    python scripts/generate_sample_data.py [--out data/sample] [--days 90]
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
START = date(2026, 1, 5)
MAIN_HOST = "example.com"
PORTAL_HOST = "app.example.com"

# source, medium, share of Piwik sessions, GA4 coverage (GA4 / Piwik)
CHANNELS = [
    ("google", "organic", 0.38, 0.60),
    ("google", "cpc", 0.14, 0.58),
    ("(direct)", "(none)", 0.20, 0.45),
    ("bing", "organic", 0.04, 0.62),
    ("facebook", "paid_social", 0.08, 0.15),
    ("linkedin", "social", 0.04, 0.40),
    ("newsletter", "email", 0.06, 0.55),
    ("partner-site.example.org", "referral", 0.03, 0.50),
    ("native-network", "native", 0.03, 0.35),
]
CAMPAIGNS = {
    "cpc": ["brand_search", "generic_search"],
    "paid_social": ["spring_awareness", "retargeting"],
    "email": ["monthly_newsletter"],
    "native": ["content_promo"],
}
LANDING_PAGES = ["/", "/pricing", "/signup", "/features", "/news/product-update", "/get-started"]
FORM_TYPES = ["individual", "student", "business"]

BASE_PIWIK_SESSIONS = 1800
BOT_DAY_OFFSET = 23
NOT_SET_DAY_OFFSET = 51


def weekday_factor(d):
    return 0.65 if d.weekday() >= 5 else 1.0


def noisy(value, rng, spread=0.12):
    return max(0, round(value * rng.uniform(1 - spread, 1 + spread)))


def piwik_rows(days, rng):
    rows = []
    for i in range(days):
        d = START + timedelta(days=i)
        total = BASE_PIWIK_SESSIONS * weekday_factor(d)
        for source, medium, share, _ in CHANNELS:
            sessions = noisy(total * share, rng)
            if i == BOT_DAY_OFFSET and medium == "(none)":
                sessions += 2700  # crawler: direct, no referrer
            if sessions == 0:
                continue
            bot = i == BOT_DAY_OFFSET and medium == "(none)"
            pages_per_session = 1.02 if bot else rng.uniform(1.6, 2.8)
            rows.append({
                "loaded_date": "",
                "date": d.isoformat(),
                "source": "direct" if source == "(direct)" else source,
                "medium": "direct" if medium == "(none)" else medium,
                "sessions": sessions,
                "visitors": max(1, round(sessions / rng.uniform(1.02, 1.08))),
                "page_views": round(sessions * pages_per_session),
                "bounce_rate": round(0.97 if bot else rng.uniform(0.35, 0.65), 4),
            })
    return rows


def ga4_session_rows(days, rng):
    rows = []
    for i in range(days):
        d = START + timedelta(days=i)
        total = BASE_PIWIK_SESSIONS * weekday_factor(d)
        for source, medium, share, coverage in CHANNELS:
            channel_sessions = total * share * coverage
            for page in LANDING_PAGES:
                weight = rng.uniform(0.05, 0.3)
                sessions = noisy(channel_sessions * weight, rng, 0.25)
                if sessions == 0:
                    continue
                host = PORTAL_HOST if page in ("/signup", "/get-started") and rng.random() < 0.3 else MAIN_HOST
                rows.append(_ga4_row(d, host, source, medium, sessions, rng, landing_page=page))
        if i == NOT_SET_DAY_OFFSET:
            rows.append(_ga4_row(d, MAIN_HOST, "(not set)", "(not set)", 2600, rng, landing_page="(not set)"))
    return rows


def ga4_campaign_rows(days, rng):
    rows = []
    for i in range(days):
        d = START + timedelta(days=i)
        total = BASE_PIWIK_SESSIONS * weekday_factor(d)
        for source, medium, share, coverage in CHANNELS:
            for campaign in CAMPAIGNS.get(medium, ["(organic)" if medium == "organic" else "(not set)"]):
                sessions = noisy(total * share * coverage / len(CAMPAIGNS.get(medium, [0])), rng)
                if sessions:
                    rows.append(_ga4_row(d, MAIN_HOST, source, medium, sessions, rng, campaign=campaign))
    return rows


def _ga4_row(d, host, source, medium, sessions, rng, landing_page=None, campaign=None):
    row = {"loaded_date": "", "date": d.isoformat(), "host_name": host}
    if landing_page is not None:
        row["landing_page"] = landing_page
    row.update({"source": source, "medium": medium})
    if campaign is not None:
        row["campaign"] = campaign
    row.update({
        "sessions": sessions,
        "screen_page_views": round(sessions * rng.uniform(1.5, 3.0)),
        "bounce_rate": round(rng.uniform(0.3, 0.6), 4),
        "total_users": max(1, round(sessions / rng.uniform(1.05, 1.25))),
    })
    if landing_page is not None:
        row["new_users"] = round(row["total_users"] * rng.uniform(0.6, 0.85))
    return row


def funnel_rows(days, rng):
    steps = [("1 Landing page", 1.0), ("2 Form opened", 0.45), ("3 Form started", 0.28), ("4 Form submitted", 0.16)]
    rows = []
    for i in range(days):
        d = START + timedelta(days=i)
        for source, medium, share, coverage in CHANNELS:
            landing = noisy(140 * weekday_factor(d) * share * coverage * 4, rng, 0.3)
            for step, rate in steps:
                forms = ["Unknown"] if step[0] in "12" else FORM_TYPES
                for form in forms:
                    sessions = noisy(landing * rate / len(forms), rng, 0.3)
                    if sessions == 0:
                        continue
                    rows.append({
                        "date": d.isoformat(),
                        "step": step,
                        "form_type": form.capitalize() if form != "Unknown" else form,
                        "source": source,
                        "medium": medium,
                        "ga4_channel": _ga4_channel(medium),
                        "sessions": sessions,
                        "events": sessions + rng.randint(0, max(1, sessions // 3)),
                    })
    return rows


def _ga4_channel(medium):
    return {
        "organic": "Organic Search", "cpc": "Paid Search", "(none)": "Direct",
        "paid_social": "Paid Social", "social": "Organic Social", "email": "Email",
        "referral": "Referral", "native": "Display",
    }.get(medium, "Unassigned")


def write_csv(path, rows, loaded_date):
    for row in rows:
        if "loaded_date" in row:
            row["loaded_date"] = loaded_date
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path}: {len(rows)} rows")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/sample")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    rng = random.Random(SEED)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    loaded_date = (START + timedelta(days=args.days)).isoformat()

    write_csv(out / "piwik_sessions.csv", piwik_rows(args.days, rng), loaded_date)
    write_csv(out / "ga4_sessions_daily.csv", ga4_session_rows(args.days, rng), loaded_date)
    write_csv(out / "ga4_campaigns_daily.csv", ga4_campaign_rows(args.days, rng), loaded_date)
    write_csv(out / "signup_funnel_daily.csv", funnel_rows(args.days, rng), loaded_date)


if __name__ == "__main__":
    main()
