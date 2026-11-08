-- plantbot analysis on Snowflake (Snowsight worksheet; context PLANTBOT.FIELD, warehouse PLANTBOT_WH).
USE WAREHOUSE PLANTBOT_WH;
USE SCHEMA PLANTBOT.FIELD;

-- A1. Field leaderboard: which plants are getting worse fastest.
SELECT PLANT, ROVER_ID, COUNT(*) AS SCANS,
       ROUND(MAX_BY(HEALTH, TS), 1)          AS LATEST_HEALTH,
       ROUND(ANY_VALUE(HEALTH_PER_HOUR), 2)  AS HEALTH_PER_HOUR,
       MAX_BY(STATUS, TS)                    AS LATEST_STATUS
FROM PLANT_TRENDS
GROUP BY PLANT, ROVER_ID
ORDER BY HEALTH_PER_HOUR ASC NULLS LAST, LATEST_HEALTH ASC
LIMIT 20;

-- A2. Hourly field health with the share of flagged plants.
SELECT DATE_TRUNC('hour', TS) AS HOUR, COUNT(*) AS SCANS,
       ROUND(AVG(HEALTH), 1) AS AVG_HEALTH,
       ROUND(AVG(IFF(STATUS <> 'healthy', 1, 0)) * 100, 0) AS PCT_FLAGGED,
       ROUND(CORR(HEALTH, VOC_STRESS), 2) AS HEALTH_VOC_CORR
FROM SCANS GROUP BY 1 ORDER BY 1 DESC;

-- A3. Cortex AI: per-scan triage note (one LLM call per row: keep the LIMIT small on trial credits).
--     If AI_COMPLETE is unknown on your account, use SNOWFLAKE.CORTEX.COMPLETE with the same args.
--     If the model isn't in your region: ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';
SELECT SCAN_ID, STATUS, ROUND(HEALTH) AS HEALTH,
       AI_COMPLETE('llama3.1-70b',
         'In one sentence, tell a farmer what to check on this plant. Leaf model says "' || LABEL
         || '" (p_healthy ' || ROUND(P_HEALTHY, 2) || '), fused health ' || ROUND(HEALTH)
         || '/100, VOC stress ' || ROUND(COALESCE(VOC_STRESS, 0), 2)
         || ', temperature ' || ROUND(TEMP_C, 1) || ' C, humidity ' || ROUND(RH) || '%.') AS TRIAGE
FROM SCANS WHERE STATUS <> 'healthy'
ORDER BY TS DESC LIMIT 5;

-- A4. Cortex AI: whole-field report (same query the rover runs: snowflake_sink --report).
WITH recent AS (
  SELECT COALESCE(PLANT_ID, SCAN_ID) AS PLANT, TS, LABEL, STATUS, HEALTH, VOC_STRESS
  FROM SCANS WHERE TS > DATEADD(hour, -24, CURRENT_TIMESTAMP()) ORDER BY TS DESC LIMIT 60
)
SELECT AI_COMPLETE('llama3.1-70b',
  'You are an agronomist reviewing a field robot''s plant scans. Each line: time, plant, label, status, '
  || 'health 0-100, VOC stress 0-1. Give 4 bullets: overall health, plants to check first, '
  || 'VOC-vs-image disagreements, next action.\n'
  || LISTAGG(TO_VARCHAR(TS, 'HH24:MI') || ' ' || PLANT || ' ' || LABEL || ' ' || STATUS || ' '
             || ROUND(HEALTH, 0) || ' ' || ROUND(COALESCE(VOC_STRESS, 0), 2), '\n') WITHIN GROUP (ORDER BY TS)
) AS FIELD_REPORT
FROM recent;
