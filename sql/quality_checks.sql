-- =============================================================================
-- Sanity checks for the loaded tables
-- =============================================================================
-- A pipeline can be technically flawless and still deliver wrong data: the
-- code faithfully stores whatever the source returns. These checks do not
-- rely on the code being right. They test whether the numbers are physically
-- and logically possible. Run them whenever a source or its tagging changes.
--
-- Each query returns the offending rows; an empty result means the check
-- passed. Replace `my-gcp-project.web_analytics` with your dataset.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. More sessions than page views
-- -----------------------------------------------------------------------------
-- Every web session contains at least one page view, so sessions > page_views
-- is impossible. When it happens, visitor identification is broken at
-- collection time (each hit becomes a new session). Data broken at collection
-- cannot be repaired by re-fetching.
SELECT
  date,
  SUM(sessions)   AS sessions,
  SUM(page_views) AS page_views
FROM `my-gcp-project.web_analytics.piwik_sessions`
GROUP BY date
HAVING SUM(sessions) > SUM(page_views)
ORDER BY date;


-- -----------------------------------------------------------------------------
-- 2. sessions = visitors on every row of a day
-- -----------------------------------------------------------------------------
-- A companion signature of the same failure: if no visitor ever returns
-- within a day, across every source/medium row, the visitor ID is most likely
-- being reset on each hit.
SELECT
  date,
  COUNT(*)              AS rows_total,
  COUNTIF(sessions = visitors) AS rows_sessions_eq_visitors
FROM `my-gcp-project.web_analytics.piwik_sessions`
GROUP BY date
HAVING COUNT(*) >= 5
   AND COUNTIF(sessions = visitors) = COUNT(*)
ORDER BY date;


-- -----------------------------------------------------------------------------
-- 3. Bot days: unusual direct volume with ~1 page per session
-- -----------------------------------------------------------------------------
-- Crawlers typically show up as direct traffic, no referrer, one page per
-- session. Flags days where direct sessions exceed `multiplier` x the
-- trailing 28-day average and pages/session is at or below
-- `max_pages_per_session`.
-- GA4 filters known bots by default; tools without bot filtering do not.
WITH params AS (
  SELECT 3.0 AS multiplier, 1.10 AS max_pages_per_session
),
direct_daily AS (
  SELECT
    date,
    SUM(sessions)   AS direct_sessions,
    SUM(page_views) AS direct_page_views
  FROM `my-gcp-project.web_analytics.piwik_sessions`
  WHERE LOWER(medium) IN ('direct', '(none)')
  GROUP BY date
),
with_baseline AS (
  SELECT
    *,
    AVG(direct_sessions) OVER (
      ORDER BY UNIX_DATE(date) RANGE BETWEEN 28 PRECEDING AND 1 PRECEDING
    ) AS baseline_avg
  FROM direct_daily
)
SELECT
  date,
  direct_sessions,
  ROUND(baseline_avg) AS baseline_avg,
  ROUND(SAFE_DIVIDE(direct_page_views, direct_sessions), 2) AS pages_per_session
FROM with_baseline, params
WHERE baseline_avg IS NOT NULL
  AND direct_sessions > multiplier * baseline_avg
  AND SAFE_DIVIDE(direct_page_views, direct_sessions) <= max_pages_per_session
ORDER BY date;


-- -----------------------------------------------------------------------------
-- 4. GA4: a single (not set) row dominating a day
-- -----------------------------------------------------------------------------
-- A tagging change can dump most of a day's traffic into one
-- (not set)/(not set) row. Flags days where that row holds > 50 % of sessions.
SELECT
  date,
  SUM(IF(source = '(not set)' AND medium = '(not set)', sessions, 0)) AS not_set_sessions,
  SUM(sessions) AS total_sessions,
  ROUND(SAFE_DIVIDE(
    SUM(IF(source = '(not set)' AND medium = '(not set)', sessions, 0)),
    SUM(sessions)), 3) AS not_set_share
FROM `my-gcp-project.web_analytics.ga4_sessions_daily`
GROUP BY date
HAVING not_set_share > 0.5
ORDER BY date;
