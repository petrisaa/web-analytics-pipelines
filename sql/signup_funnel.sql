-- =============================================================================
-- signup_funnel_daily
-- =============================================================================
-- Purpose
--   Four-step sign-up funnel, counted in sessions per day, built directly on
--   the GA4 BigQuery export. The funnel crosses hosts: visitors land on the
--   public site (example.com) and fill in the form on a separate portal
--   (app.example.com), both tracked in the same GA4 property.
--
-- Steps
--   1 Landing page    page_view on a sign-up landing URL on any host other
--                     than the portal. News articles that merely mention
--                     "sign up" are excluded by URL pattern.
--   2 Form opened     The same page_view condition, but on the portal host.
--   3 Form started    Custom form event where the button text is a "next"
--                     button, i.e. the visitor moved past the first form page.
--                     "Back" buttons do not count.
--   4 Form submitted  The same custom form event where the destination
--                     parameter is set but the button text is MISSING.
--
-- Why step 4 is inferred from a missing parameter
--   The form fires one custom event for every button. Navigation buttons send
--   their label (Next / Back). The final submit button sends a destination URL
--   but no label. So "destination present AND label absent" = submission.
--   This was verified against per-session event sequences: completed sessions
--   end in exactly this event and abandoned sessions never contain it. It is
--   still an inference, not a named conversion event. If the portal starts
--   sending a dedicated success event, rebuild step 4 on that.
--
-- Attribution: one source/medium per SESSION, not per event
--   Each session gets the first non-empty traffic source in event-time order,
--   so a session stays in the same channel through every step and the
--   source/medium dimensions never change SUM(sessions). Priority:
--     1. session_traffic_source_last_click.cross_channel_campaign
--     2. session_traffic_source_last_click.manual_campaign
--     3. collected_traffic_source.manual_*
--   cross_channel_campaign comes first because manual_campaign is only filled
--   from UTM tags. Google Ads auto-tagging (gclid) leaves it empty, and paid
--   search clicks were previously misattributed to google / organic.
--   Values are raw GA4 values; channel grouping happens downstream
--   (see channel_grouping.sql).
--
-- Known limitations
--   1. Steps are counted independently, not by following one session through
--      all four. Step-to-step rates are indicative only; some sessions enter
--      the form directly without a landing page view.
--   2. Form events without ga_session_id cannot be tied to a session. They
--      are counted in `events` but not in `sessions`.
--   3. Without cross-domain measurement, the hop from the main site to the
--      portal starts a new session and loses the original source. Only the
--      step 1 channel split can be read as attribution.
--
-- Configuration
--   Edit the `params` CTE below. Table names are literal because BigQuery
--   does not allow parameterised table references in a view; replace
--   `my-gcp-project`, `analytics_123456789` and `web_analytics` with yours.
--   The _TABLE_SUFFIX lower bound is also literal so partition pruning works.
-- =============================================================================
CREATE OR REPLACE VIEW `my-gcp-project.web_analytics.signup_funnel_daily` AS
WITH params AS (
  SELECT
    'app.example.com'                                AS portal_host,
    ['%/signup%', '%/get-started%']                  AS landing_url_patterns,
    ['%/news/%', '%/blog/%']                         AS excluded_url_patterns,
    'signup_form_step'                               AS form_event_name,
    'form_submit_text'                               AS button_param,
    'form_destination'                               AS destination_param,
    ['Next', 'Continue']                             AS next_button_texts,
    -- Form type from the destination URL. First matching pattern wins.
    [
      STRUCT('%individual%' AS pattern, 'Individual' AS label),
      STRUCT('%student%',               'Student'),
      STRUCT('%business%',              'Business')
    ]                                                AS form_types
),

base AS (
  SELECT
    PARSE_DATE('%Y%m%d', e.event_date) AS date,
    e.event_name,
    e.event_timestamp,
    e.device.web_info.hostname AS hostname,
    CONCAT(e.user_pseudo_id, '-',
      CAST((SELECT value.int_value FROM UNNEST(e.event_params)
            WHERE key = 'ga_session_id') AS STRING)) AS session_id,
    (SELECT value.string_value FROM UNNEST(e.event_params)
     WHERE key = 'page_location') AS page_location,
    (SELECT value.string_value FROM UNNEST(e.event_params)
     WHERE key = p.button_param) AS button_text,
    (SELECT value.string_value FROM UNNEST(e.event_params)
     WHERE key = p.destination_param) AS form_destination,

    -- Event-level source/medium by priority. NULLIF turns GA4's '(not set)'
    -- into NULL so the next candidate is tried.
    CASE
      WHEN NULLIF(e.session_traffic_source_last_click.cross_channel_campaign.source, '(not set)') IS NOT NULL
        THEN e.session_traffic_source_last_click.cross_channel_campaign.source
      WHEN NULLIF(e.session_traffic_source_last_click.manual_campaign.source, '(not set)') IS NOT NULL
        THEN e.session_traffic_source_last_click.manual_campaign.source
      WHEN NULLIF(e.collected_traffic_source.manual_source, '(not set)') IS NOT NULL
        THEN e.collected_traffic_source.manual_source
    END AS ev_source,
    CASE
      WHEN NULLIF(e.session_traffic_source_last_click.cross_channel_campaign.source, '(not set)') IS NOT NULL
        THEN e.session_traffic_source_last_click.cross_channel_campaign.medium
      WHEN NULLIF(e.session_traffic_source_last_click.manual_campaign.source, '(not set)') IS NOT NULL
        THEN e.session_traffic_source_last_click.manual_campaign.medium
      WHEN NULLIF(e.collected_traffic_source.manual_source, '(not set)') IS NOT NULL
        THEN e.collected_traffic_source.manual_medium
    END AS ev_medium,
    e.session_traffic_source_last_click.cross_channel_campaign.default_channel_group AS ev_ga4_channel
  FROM `my-gcp-project.analytics_123456789.events_*` AS e
  CROSS JOIN params AS p
  WHERE _TABLE_SUFFIX >= '20250101'
    AND e.event_name IN ('page_view', p.form_event_name)
),

-- Keep only funnel-relevant events.
funnel_events AS (
  SELECT b.*
  FROM base AS b
  CROSS JOIN params AS p
  WHERE b.event_name = p.form_event_name
     OR (
       b.event_name = 'page_view'
       AND EXISTS (SELECT 1 FROM UNNEST(p.landing_url_patterns) AS pat
                   WHERE b.page_location LIKE pat)
       AND NOT EXISTS (SELECT 1 FROM UNNEST(p.excluded_url_patterns) AS pat
                       WHERE b.page_location LIKE pat)
     )
),

-- One source per session: the first non-empty event in time order.
session_source AS (
  SELECT
    session_id,
    ev_source      AS source,
    ev_medium      AS medium,
    ev_ga4_channel AS ga4_channel
  FROM funnel_events
  WHERE ev_source IS NOT NULL
    AND session_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY event_timestamp) = 1
),

-- Steps 1 and 2: page views, split by host. The host check comes first so a
-- landing page on the main site with the same path as a portal page still
-- lands in step 1.
page_steps AS (
  SELECT
    f.date,
    f.session_id,
    IF(f.hostname = p.portal_host, '2 Form opened', '1 Landing page') AS step,
    CAST(NULL AS STRING) AS form_destination
  FROM funnel_events AS f
  CROSS JOIN params AS p
  WHERE f.event_name = 'page_view'
),

-- Step 3: forward navigation inside the form.
-- Step 4: submit button (destination present, no button label).
form_steps AS (
  SELECT
    f.date,
    f.session_id,
    IF(f.button_text IS NULL, '4 Form submitted', '3 Form started') AS step,
    f.form_destination
  FROM funnel_events AS f
  CROSS JOIN params AS p
  WHERE f.event_name = p.form_event_name
    AND f.form_destination IS NOT NULL
    AND (f.button_text IS NULL OR f.button_text IN UNNEST(p.next_button_texts))
),

all_steps AS (
  SELECT * FROM page_steps
  UNION ALL
  SELECT * FROM form_steps
)

SELECT
  s.date,
  s.step,
  CASE
    WHEN s.form_destination IS NULL THEN 'Unknown'
    ELSE COALESCE(
      (SELECT ft.label FROM params AS p, UNNEST(p.form_types) AS ft WITH OFFSET AS i
       WHERE s.form_destination LIKE ft.pattern
       ORDER BY i LIMIT 1),
      'Other')
  END AS form_type,
  COALESCE(src.source,      '(not set)') AS source,
  COALESCE(src.medium,      '(not set)') AS medium,
  COALESCE(src.ga4_channel, '(not set)') AS ga4_channel,
  COUNT(DISTINCT s.session_id) AS sessions,
  COUNT(*)                     AS events
FROM all_steps AS s
LEFT JOIN session_source AS src USING (session_id)
GROUP BY 1, 2, 3, 4, 5, 6
