-- Privacy-safe hourly aggregates. No identity, device, session, IP, secret, or
-- controller-state dimensions are permitted by the Worker metric whitelist.
CREATE TABLE aggregate_metrics (
    hour INTEGER NOT NULL,
    metric TEXT NOT NULL,
    dimension TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL,
    value_sum REAL NOT NULL,
    PRIMARY KEY (hour, metric, dimension)
);
CREATE INDEX aggregate_metrics_hour ON aggregate_metrics(hour);
