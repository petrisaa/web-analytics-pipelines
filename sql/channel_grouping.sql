-- =============================================================================
-- Example channel grouping
-- =============================================================================
-- A generic source/medium -> channel mapping, applied identically to GA4 and
-- Piwik PRO rows so the two sources can be compared per channel. This is an
-- illustration only; a real mapping must be built from the campaign tagging
-- conventions actually in use.
--
-- Rules are evaluated top to bottom; the first match wins. GA4 literals such
-- as (direct), (none) and (not set) are matched explicitly and never
-- normalised away: dropping them silently misattributes traffic.
-- =============================================================================
CREATE OR REPLACE VIEW `my-gcp-project.web_analytics.sessions_by_channel` AS
WITH unified AS (
  SELECT date, 'ga4' AS system, host_name, LOWER(source) AS source, LOWER(medium) AS medium,
         sessions, screen_page_views AS page_views
  FROM `my-gcp-project.web_analytics.ga4_sessions_daily`
  UNION ALL
  SELECT date, 'piwik', CAST(NULL AS STRING), LOWER(source), LOWER(medium),
         sessions, page_views
  FROM `my-gcp-project.web_analytics.piwik_sessions`
)
SELECT
  date,
  system,
  host_name,
  CASE
    WHEN source IN ('(direct)', 'direct') OR medium IN ('(none)', 'direct')
      THEN 'Direct'
    -- Paid social before paid search: social platforms also use medium=cpc.
    WHEN medium IN ('paid_social', 'paidsocial', 'social_paid')
      OR (REGEXP_CONTAINS(source, r'facebook|instagram|linkedin|tiktok|meta')
          AND medium IN ('cpc', 'cpm', 'paid'))
      THEN 'Paid Social'
    WHEN medium IN ('cpc', 'ppc', 'paid_search', 'paidsearch')
      THEN 'Paid Search'
    WHEN medium IN ('display', 'banner', 'cpm', 'programmatic')
      THEN 'Display'
    WHEN medium IN ('native', 'sponsored')
      THEN 'Native'
    WHEN medium = 'organic' OR medium = 'search'
      THEN 'Organic Search'
    WHEN medium IN ('social', 'social-network', 'sm')
      OR REGEXP_CONTAINS(source, r'facebook|instagram|linkedin|tiktok|t\.co|x\.com|youtube')
      THEN 'Organic Social'
    WHEN medium IN ('email', 'e-mail', 'newsletter')
      THEN 'Email'
    WHEN medium = 'referral'
      THEN 'Referral'
    WHEN source = '(not set)' OR medium = '(not set)'
      THEN 'Unassigned'
    ELSE 'Other'
  END AS channel,
  SUM(sessions)   AS sessions,
  SUM(page_views) AS page_views
FROM unified
GROUP BY 1, 2, 3, 4
