-- SigNoz ClickHouse Database Initialization
-- This script creates the necessary databases for SigNoz

-- Create databases if they don't exist
CREATE DATABASE IF NOT EXISTS signoz_traces;
CREATE DATABASE IF NOT EXISTS signoz_metrics;
CREATE DATABASE IF NOT EXISTS signoz_logs;

-- Grant permissions (ClickHouse default user has all permissions)
-- No explicit GRANT needed for default user in single-node setup

-- Note: Tables will be auto-created by SigNoz OTEL Collector on first connection
-- The collector handles schema migrations automatically
