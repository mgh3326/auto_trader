# Monitoring Stack Testing Guide

This document describes how to test the Grafana Observability Stack integration.

## Quick Start

### Run Integration Tests

```bash
# Shell-based integration test (quick smoke test)
chmod +x scripts/test-monitoring-integration.sh
./scripts/test-monitoring-integration.sh

# Python-based integration tests (comprehensive)
uv run pytest tests/integration/test_monitoring_stack.py -v
```

## Test Coverage

### 1. Service Health Checks (`test_service_health_endpoints`)

Validates that all services expose proper health endpoints:
- Tempo: `http://localhost:3200/status`
- Loki: `http://localhost:3100/ready`
- Prometheus: `http://localhost:9090/-/healthy`
- Grafana: `http://localhost:3000/api/health`
- OTEL Collector: `http://localhost:13133/`
- Promtail: `http://localhost:9080/ready`

### 2. Datasource Provisioning (`test_grafana_datasources_provisioned`)

Verifies that Grafana automatically provisions all three datasources:
- Tempo (traces)
- Loki (logs)
- Prometheus (metrics)

### 3. OTLP Trace Ingestion (`test_otlp_trace_ingestion`)

Sends a real OTLP trace via gRPC to port 4317 and verifies:
- OTEL Collector accepts the trace
- Trace is forwarded to Tempo
- Tempo search API responds correctly

### 4. Prometheus Scraping (`test_prometheus_scraping_targets`)

Checks that Prometheus actively scrapes all configured targets:
- prometheus (self-monitoring)
- tempo (metrics)
- loki (metrics)
- grafana (metrics)
- otel-collector (metrics)

### 5. Query APIs (`test_*_query_api`)

Tests that all query APIs are functional:
- Prometheus query API (`/api/v1/query`)
- Loki query API (`/loki/api/v1/query`)
- Tempo search API (via Grafana proxy)

### 6. OTEL Collector Metrics (`test_otel_collector_metrics_exposed`)

Validates that OTEL Collector exposes its own telemetry metrics on port 8888.

### 7. Data Retention (`test_data_retention_config`)

Verifies that all services use consistent 7-day retention:
- Tempo: 168h
- Loki: 168h
- Prometheus: 7d

### 8. End-to-End Flow (`test_end_to_end_observability_flow`)

Complete observability pipeline test:
1. Application sends OTLP trace to Collector (port 4317)
2. Collector routes trace to Tempo
3. Grafana can query trace from Tempo
4. Verifies full telemetry pipeline

## Manual Testing

### Test OTLP Trace Ingestion

```bash
# Send test trace via OTLP HTTP
curl -X POST http://localhost:4318/v1/traces \
  -H "Content-Type: application/json" \
  -d '{
    "resourceSpans": [{
      "resource": {
        "attributes": [{
          "key": "service.name",
          "value": {"stringValue": "manual-test"}
        }]
      },
      "scopeSpans": [{
        "spans": [{
          "traceId": "5B8EFFF798038103D269B633813FC60C",
          "spanId": "EEE19B7EC3C1B174",
          "name": "manual-test-span",
          "kind": 1,
          "startTimeUnixNano": "1544712660000000000",
          "endTimeUnixNano": "1544712661000000000"
        }]
      }]
    }]
  }'
```

### Query Traces in Grafana

1. Open http://localhost:3000
2. Go to Explore
3. Select "Tempo" datasource
4. Click "Search" tab
5. Look for traces from "manual-test" service

### Verify Metrics Collection

```bash
# Check Prometheus targets
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, health: .health}'

# Query metrics
curl 'http://localhost:9090/api/v1/query?query=up' | jq '.data.result[] | {job: .metric.job, value: .value[1]}'
```

### Verify Logs Collection

```bash
# Query Loki for Docker logs
curl -G 'http://localhost:3100/loki/api/v1/query' \
  --data-urlencode 'query={job="dockerlogs"}' \
  --data-urlencode 'limit=10' | jq '.data.result[0].values'
```

## Continuous Integration

### GitHub Actions Integration (Optional)

Integration tests can be added to CI but require significant resources. For Raspberry Pi deployments, **manual verification is recommended**.

#### Option 1: Add to GitHub Actions (Resource Intensive)

```yaml
  monitoring-integration-tests:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'  # Only on PRs
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install uv
          uv sync --all-groups

      - name: Start monitoring stack
        run: |
          docker compose -f docker-compose.monitoring-rpi.yml up -d
          sleep 45  # Wait for all services to be healthy

      - name: Run integration tests
        run: |
          uv run pytest tests/integration/test_monitoring_stack.py -v --run-integration

      - name: Run smoke tests
        run: |
          chmod +x scripts/test-monitoring-integration.sh
          ./scripts/test-monitoring-integration.sh

      - name: Collect logs on failure
        if: failure()
        run: |
          docker compose -f docker-compose.monitoring-rpi.yml logs
          docker compose -f docker-compose.monitoring-rpi.yml ps

      - name: Cleanup
        if: always()
        run: |
          docker compose -f docker-compose.monitoring-rpi.yml down -v
```

**Note:** This adds ~2-3 minutes to CI time and requires Docker resources.

#### Option 2: Manual Verification (Recommended for Raspberry Pi)

Instead of automated CI tests, document manual verification steps in PRs:

```markdown
## Monitoring Stack Verification

**Tested on:** Raspberry Pi 5 (8GB RAM)

### Pre-deployment Tests
- [x] All services start successfully
- [x] Health checks pass for all services
- [x] Grafana datasources provisioned correctly
- [x] OTLP endpoints accept traces/metrics

### Manual Test Results
```bash
$ ./scripts/test-monitoring-integration.sh
✓ PASS: Tempo /status endpoint responding
✓ PASS: Loki /ready endpoint responding
✓ PASS: Prometheus /-/healthy endpoint responding
✓ PASS: Grafana /api/health endpoint responding
✓ PASS: OTEL Collector health endpoint responding
✓ PASS: Promtail /ready endpoint responding
...
✓ All tests passed!
```

### Screenshots
- Attached: Grafana Explore showing test traces
- Attached: Prometheus targets all healthy
```

This approach is more practical for resource-constrained CI environments.

## Troubleshooting

### Tests Fail: Services Not Ready

Services may take time to start. Increase wait times:

```python
# In tests
time.sleep(5)  # Increase to 10-30 seconds
```

### Traces Not Found in Tempo

Tempo indexing has a delay. Traces are ingested immediately but may take 30-60 seconds to appear in search results.

### Prometheus Targets "Down"

Check if services expose `/metrics` endpoints:

```bash
curl http://localhost:3200/metrics  # Tempo
curl http://localhost:3100/metrics  # Loki
curl http://localhost:8888/metrics  # OTEL Collector
```

### OTLP Connection Refused

Ensure OTEL Collector is running and ports are exposed:

```bash
docker compose -f docker-compose.monitoring-rpi.yml ps otel-collector
netstat -an | grep 4317
netstat -an | grep 4318
```

## Test Results Documentation

After running tests, document results in PR:

```markdown
## Monitoring Stack Integration Test Results

**Environment:** Raspberry Pi 5 (8GB RAM)

**Test Execution:**
- ✅ Service health checks: All passed
- ✅ Datasource provisioning: Tempo, Loki, Prometheus configured
- ✅ OTLP trace ingestion: Test trace sent successfully
- ✅ Prometheus scraping: All targets scraped
- ✅ Query APIs: All responding correctly
- ✅ End-to-end flow: Validated

**Metrics:**
- Test duration: ~45 seconds
- All 15 tests passed
- No errors or warnings

**Screenshots:**
- Attached Grafana Explore showing test traces
- Prometheus targets page showing all services healthy
```

## Performance Testing

For load testing the monitoring stack:

```bash
# Install k6 for load testing
brew install k6  # macOS
# or download from https://k6.io/

# Run load test
k6 run scripts/loadtest-otel.js
```

This ensures the stack can handle production load on Raspberry Pi 5.
