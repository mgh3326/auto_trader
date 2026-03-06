#!/bin/bash
# Quick Performance Benchmark for Subtask 5-5
# This script runs the performance benchmark with sensible defaults

set -e  # Exit on error

echo "=========================================="
echo "KR Hourly Candles Performance Benchmark"
echo "=========================================="
echo ""

# Check if required services are running
echo "Checking services..."
if ! docker compose ps postgres | grep -q "Up"; then
    echo "Error: PostgreSQL is not running"
    echo "Please start services with: docker compose up -d"
    exit 1
fi

echo "✓ Services are running"
echo ""

# Check if .env file exists
if [ ! -f .env ]; then
    echo "Error: .env file not found"
    echo "Please create .env file with required credentials"
    exit 1
fi

echo "✓ Configuration file found"
echo ""

# Parse command line arguments
SYMBOL=${1:-005930}
COUNT=${2:-5}
RUNS=${3:-3}

echo "Benchmark Configuration:"
echo "  Symbol: $SYMBOL"
echo "  Candle Count: $COUNT"
echo "  Benchmark Runs: $RUNS"
echo ""

# Run the benchmark
echo "Starting benchmark..."
echo ""

# Check if uv is available
if command -v uv &> /dev/null; then
    uv run python benchmark_performance_subtask_5_5.py "$SYMBOL" "$COUNT" --runs "$RUNS"
else
    # Fallback to python3
    python3 benchmark_performance_subtask_5_5.py "$SYMBOL" "$COUNT" --runs "$RUNS"
fi

# Capture exit code
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ Benchmark completed successfully - all targets met!"
else
    echo "✗ Benchmark completed with failures - see output above"
fi

exit $EXIT_CODE
