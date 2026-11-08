-- plantbot on Tiger Data (Timescale Cloud). Idempotent: safe to re-run.
-- Apply:  python -m plantbot.integrations.tigerdata --init-schema
--    or:  psql "$TIGERDATA_URL" -f plantbot/integrations/sql/tigerdata_schema.sql
-- Needs TimescaleDB >= 2.20 for CREATE TABLE ... WITH (tsdb.hypertable) (Tiger Cloud is always newer).
-- On an older self-hosted TimescaleDB use instead:
--   CREATE TABLE env_readings (...);  SELECT create_hypertable('env_readings', by_range('time', INTERVAL '1 day'));

-- 1. Raw 10 Hz sensor stream from every rover -> hypertable (chunked by day, columnstore on).
CREATE TABLE IF NOT EXISTS env_readings (
    time          TIMESTAMPTZ      NOT NULL,
    rover_id      TEXT             NOT NULL,
    mq2_v         DOUBLE PRECISION,           -- MQ-2 analog volts (plant-side sensor on rover v3)
    gas_ohms      DOUBLE PRECISION,           -- BME680 gas resistance (background sensor)
    temp_c        DOUBLE PRECISION,
    rh            DOUBLE PRECISION,
    pressure_hpa  DOUBLE PRECISION,
    voc_top       DOUBLE PRECISION,           -- plant side,  ln(baseline/now) in BME680 units (>0 = more VOCs)
    voc_bottom    DOUBLE PRECISION,           -- background,  same units
    warm          BOOLEAN,                    -- sensors past warm-up
    sniffing      BOOLEAN                     -- fan on, rover parked at a plant
) WITH (
    tsdb.hypertable,
    tsdb.partition_column = 'time',
    tsdb.chunk_interval   = '1 day',
    tsdb.segmentby        = 'rover_id',
    tsdb.orderby          = 'time DESC'
);

CREATE INDEX IF NOT EXISTS env_readings_rover_time ON env_readings (rover_id, time DESC);

-- 2. Scan results (one row per photographed + sniffed plant). Small, relational: a plain table.
CREATE TABLE IF NOT EXISTS scans (
    scan_id      TEXT PRIMARY KEY,
    time         TIMESTAMPTZ NOT NULL,
    rover_id     TEXT        NOT NULL,
    plant_id     TEXT,                        -- optional: tag / row position when known
    mode         TEXT,                        -- manual | auto
    label        TEXT,                        -- leaf classifier label
    p_healthy    DOUBLE PRECISION,
    health       DOUBLE PRECISION,            -- fused 0..100 score (image + VOC)
    status       TEXT,                        -- healthy | watch | stressed
    voc_stress   DOUBLE PRECISION,
    mq2_ratio    DOUBLE PRECISION,
    gas_ratio    DOUBLE PRECISION,
    diff_ratio   DOUBLE PRECISION,
    temp_c       DOUBLE PRECISION,
    rh           DOUBLE PRECISION,
    backend      TEXT,
    infer_ms     DOUBLE PRECISION,
    record       JSONB                        -- full scan.json for anything not broken out above
);
CREATE INDEX IF NOT EXISTS scans_rover_time ON scans (rover_id, time DESC);
CREATE INDEX IF NOT EXISTS scans_plant_time ON scans (plant_id, time DESC);

-- 3. Continuous aggregate: 1-minute VOC averages per rover (what the dashboard plots).
CREATE MATERIALIZED VIEW IF NOT EXISTS voc_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 minute', time) AS bucket,
    rover_id,
    avg(voc_top)               AS voc_top_avg,
    avg(voc_bottom)            AS voc_bottom_avg,
    avg(voc_top - voc_bottom)  AS voc_diff_avg,      -- plant minus background
    max(voc_top)               AS voc_top_max,
    avg(mq2_v)                 AS mq2_v_avg,
    avg(gas_ohms)              AS gas_ohms_avg,
    avg(temp_c)                AS temp_c_avg,
    avg(rh)                    AS rh_avg,
    count(*)                   AS n,
    bool_or(sniffing)          AS any_sniff
FROM env_readings
GROUP BY bucket, rover_id
WITH NO DATA;

-- Real-time aggregation: the newest (not yet materialized) minute is still visible in demos.
ALTER MATERIALIZED VIEW voc_1m SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy('voc_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => true);

-- 4. Storage policies.
-- Columnstore (compression): CREATE TABLE ... WITH (tsdb.hypertable) already adds one that
-- converts chunks older than the chunk interval. Replace it with a 1-hour one so you can show
-- compressed chunks during the demo (add_columnstore_policy replaces add_compression_policy, 2.18+).
CALL remove_columnstore_policy('env_readings', if_exists => true);
CALL add_columnstore_policy('env_readings', after => INTERVAL '1 hour');

-- Retention: raw 10 Hz data for 30 days; the 1-minute aggregate keeps a year.
SELECT add_retention_policy('env_readings', drop_after => INTERVAL '30 days', if_not_exists => true);
SELECT add_retention_policy('voc_1m',       drop_after => INTERVAL '365 days', if_not_exists => true);
