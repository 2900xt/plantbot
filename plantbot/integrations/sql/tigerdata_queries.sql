-- plantbot showcase queries for Tiger Data. Run: python -m plantbot.integrations.tigerdata --demo-queries

-- Q1. VOC anomaly vs a rolling baseline, per rover, on the 1-minute continuous aggregate.
--     z = how many rolling standard deviations this minute's plant-minus-background VOC sits
--     above the previous 15 minutes. z > 3 = "something near the plant just started smelling".
SELECT bucket, rover_id,
       round(voc_diff_avg::numeric, 4)                                     AS voc_diff,
       round(baseline::numeric, 4)                                         AS baseline_15m,
       round(((voc_diff_avg - baseline) / NULLIF(sd, 0))::numeric, 2)      AS z_score,
       (voc_diff_avg - baseline) / NULLIF(sd, 0) > 3                       AS anomaly
FROM (
    SELECT bucket, rover_id, voc_diff_avg,
           avg(voc_diff_avg)         OVER w AS baseline,
           stddev_samp(voc_diff_avg) OVER w AS sd
    FROM voc_1m
    WHERE bucket > now() - INTERVAL '6 hours'
    WINDOW w AS (PARTITION BY rover_id ORDER BY bucket ROWS BETWEEN 15 PRECEDING AND 1 PRECEDING)
) t
ORDER BY bucket DESC
LIMIT 60;

-- Q2. Per-plant VOC signature: for every scan, the plant-side VOC during the 8 s sniff window
--     versus the same rover's 10-minute rolling baseline just before it (raw 10 Hz hypertable,
--     time_bucket-free point lookups that hit the (rover_id, time) index).
SELECT s.scan_id, s.time, COALESCE(s.plant_id, s.scan_id) AS plant,
       s.status, round(s.health::numeric, 1)           AS health,
       round(sniff.voc_top::numeric, 4)                AS sniff_voc_top,
       round(base.voc_top::numeric, 4)                 AS baseline_voc_top,
       round((sniff.voc_top - base.voc_top)::numeric, 4) AS voc_lift
FROM scans s
CROSS JOIN LATERAL (
    SELECT avg(voc_top) AS voc_top FROM env_readings e
    WHERE e.rover_id = s.rover_id AND e.time BETWEEN s.time AND s.time + INTERVAL '8 seconds'
) sniff
CROSS JOIN LATERAL (
    SELECT avg(voc_top) AS voc_top FROM env_readings e
    WHERE e.rover_id = s.rover_id AND e.time BETWEEN s.time - INTERVAL '10 minutes' AND s.time
      AND NOT COALESCE(e.sniffing, false)
) base
ORDER BY s.time DESC
LIMIT 25;

-- Q3. Field health over time: 15-minute buckets, gap-filled so the chart has no holes,
--     with last observation carried forward for minutes the rover was elsewhere.
SELECT time_bucket_gapfill(INTERVAL '15 minutes', time) AS slot,
       rover_id,
       count(*)                        AS scans,
       round(avg(health)::numeric, 1)  AS avg_health,
       locf(avg(health))               AS health_locf,
       count(*) FILTER (WHERE status <> 'healthy') AS flagged
FROM scans
WHERE time > now() - INTERVAL '24 hours' AND time <= now()
GROUP BY slot, rover_id
ORDER BY slot DESC, rover_id;

-- Q4. Proof the storage tiering works: chunks and how much the columnstore saved.
SELECT chunk_name, range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE hypertable_name = 'env_readings'
ORDER BY range_start DESC
LIMIT 10;

SELECT pg_size_pretty(before_compression_total_bytes) AS before,
       pg_size_pretty(after_compression_total_bytes)  AS after
FROM hypertable_columnstore_stats('env_readings');
