-- plantbot on Snowflake. Idempotent.
-- Apply:  python -m plantbot.integrations.snowflake_sink --init-schema
--    or:  paste into a Snowsight worksheet.
--
-- ONE-TIME ADMIN SETUP (Snowsight worksheet as ACCOUNTADMIN; NOT run by --init-schema):
--
--   -- Cortex models outside your region (needed for many models; trial users are ACCOUNTADMIN):
--   ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';
--
--   -- PATs need a network policy by default. The rover's IP changes on hackathon Wi-Fi, so
--   -- let PATs work without one (still enforced if you add one later):
--   CREATE AUTHENTICATION POLICY IF NOT EXISTS plantbot_pat_policy
--     PAT_POLICY = (NETWORK_POLICY_EVALUATION = ENFORCED_NOT_REQUIRED);
--   ALTER USER <your_user> SET AUTHENTICATION POLICY plantbot_pat_policy;
--
--   -- Token for the rover (copy token_secret from the output into SNOWFLAKE_PAT in ~/plantbot/.env):
--   ALTER USER <your_user> ADD PROGRAMMATIC ACCESS TOKEN plantbot_rover
--     ROLE_RESTRICTION = 'ACCOUNTADMIN' DAYS_TO_EXPIRY = 7;
--   (For anything beyond the hackathon: a dedicated role with only USAGE + INSERT on these objects.)

CREATE WAREHOUSE IF NOT EXISTS PLANTBOT_WH
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS PLANTBOT;
CREATE SCHEMA IF NOT EXISTS PLANTBOT.FIELD;

CREATE TABLE IF NOT EXISTS PLANTBOT.FIELD.SCANS (
  SCAN_ID     STRING PRIMARY KEY,
  TS          TIMESTAMP_TZ,
  ROVER_ID    STRING,
  PLANT_ID    STRING,
  MODE        STRING,
  LABEL       STRING,
  P_HEALTHY   FLOAT,
  HEALTH      FLOAT,          -- fused 0..100 (image + VOC)
  STATUS      STRING,         -- healthy | watch | stressed
  VOC_STRESS  FLOAT,
  MQ2_RATIO   FLOAT,
  GAS_RATIO   FLOAT,
  DIFF_RATIO  FLOAT,
  TEMP_C      FLOAT,
  RH          FLOAT,
  BACKEND     STRING,
  EXPLAIN     STRING,
  RECORD      VARIANT,        -- full scan.json (probs, sniff window, bbox, ...)
  LOADED_AT   TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
);

-- Per-plant health trend: rolling mean, change since previous scan, and a least-squares slope
-- (health points per hour) over everything we have on that plant.
CREATE OR REPLACE VIEW PLANTBOT.FIELD.PLANT_TRENDS AS
SELECT
  ROVER_ID,
  COALESCE(PLANT_ID, SCAN_ID)                                                   AS PLANT,
  TS, LABEL, STATUS, HEALTH, VOC_STRESS,
  AVG(HEALTH) OVER (PARTITION BY ROVER_ID, COALESCE(PLANT_ID, SCAN_ID)
                    ORDER BY TS ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)        AS HEALTH_MA5,
  HEALTH - LAG(HEALTH) OVER (PARTITION BY ROVER_ID, COALESCE(PLANT_ID, SCAN_ID) ORDER BY TS)
                                                                                AS HEALTH_DELTA,
  REGR_SLOPE(HEALTH, DATE_PART(EPOCH_SECOND, TS) / 3600.0)
    OVER (PARTITION BY ROVER_ID, COALESCE(PLANT_ID, SCAN_ID))                   AS HEALTH_PER_HOUR
FROM PLANTBOT.FIELD.SCANS;
