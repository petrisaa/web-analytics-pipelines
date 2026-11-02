-- =============================================================================
-- Data freshness check (BigQuery scheduled query)
-- =============================================================================
-- Purpose
--   Detects both failed and silently skipped nightly loads, independently of
--   GitHub Actions. The script fails on purpose with ERROR() when any check
--   does not hold. A failed scheduled query is written to Cloud Logging, and a
--   log-based alert in Cloud Monitoring turns that into a notification.
--   A job that never ran produces no error of its own; this check is what
--   makes "nothing happened" visible.
--
-- Checks (dates in the reporting timezone, same as loaded_date)
--   1. For each loaded table:
--        MAX(loaded_date) = today         -> tonight's job ran and succeeded
--        MAX(date)        >= yesterday    -> the job fetched data up to yesterday
--      The second condition catches a job that succeeded but got a partial
--      response from the source API.
--   2. GA4 export: the daily table events_<day before yesterday> exists.
--      Daily export tables land with a delay, hence two days, not one.
--      Missing export days cannot be backfilled, so this one is urgent.
--
-- Run manually (prints 'OK' or fails with a list of what is missing):
--   bq query --use_legacy_sql=false --location=EU < sql/freshness_check.sql
--
-- Replace `my-gcp-project`, `web_analytics` and `analytics_123456789` with
-- your own names. A scheduled query stores the SQL at creation time, so
-- update the schedule whenever this file changes.
-- =============================================================================
DECLARE reporting_tz STRING DEFAULT 'Europe/Helsinki';
DECLARE today DATE DEFAULT CURRENT_DATE(reporting_tz);
DECLARE yesterday DATE DEFAULT DATE_SUB(today, INTERVAL 1 DAY);
DECLARE export_day DATE DEFAULT DATE_SUB(today, INTERVAL 2 DAY);
DECLARE failures ARRAY<STRING>;

SET failures = ARRAY(
  WITH loads AS (
    SELECT 'piwik_sessions' AS table_name, MAX(loaded_date) AS loaded, MAX(date) AS latest
    FROM `my-gcp-project.web_analytics.piwik_sessions`
    UNION ALL
    SELECT 'ga4_sessions_daily', MAX(loaded_date), MAX(date)
    FROM `my-gcp-project.web_analytics.ga4_sessions_daily`
    UNION ALL
    SELECT 'ga4_campaigns_daily', MAX(loaded_date), MAX(date)
    FROM `my-gcp-project.web_analytics.ga4_campaigns_daily`
  )
  SELECT FORMAT('%s: loaded_date %t (expected %t), latest date %t (expected >= %t)',
                table_name, loaded, today, latest, yesterday)
  FROM loads
  WHERE loaded IS NULL OR loaded < today OR latest IS NULL OR latest < yesterday

  UNION ALL

  SELECT FORMAT('GA4 export: table events_%s is missing', FORMAT_DATE('%Y%m%d', export_day))
  FROM UNNEST([1])
  WHERE NOT EXISTS (
    SELECT 1
    FROM `my-gcp-project.analytics_123456789.INFORMATION_SCHEMA.TABLES`
    WHERE table_name = CONCAT('events_', FORMAT_DATE('%Y%m%d', export_day))
  )
);

IF ARRAY_LENGTH(failures) > 0 THEN
  SELECT ERROR(FORMAT('Freshness check failed (%d): %s',
                      ARRAY_LENGTH(failures), ARRAY_TO_STRING(failures, ' | ')));
END IF;

SELECT 'OK' AS result, today AS checked_on;
