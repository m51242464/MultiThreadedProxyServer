#!/bin/bash

PROXY_PORT=$1
if [ -z "$PROXY_PORT" ]; then
    PROXY_PORT=8080
fi

RESULTS_DIR="./benchmark_results"
mkdir -p "$RESULTS_DIR"

TARGET_URL="http://httpbin.org/get"

echo "Starting Benchmark Suite on port $PROXY_PORT..."

# 1. Baseline / Cache Miss (Small number of requests to ensure one fetch happens)
echo "Running Test: Baseline (Cache Miss)..."
ab -X localhost:$PROXY_PORT -n 10 -c 1 "$TARGET_URL?t=baseline" > "$RESULTS_DIR/baseline.txt" 2>&1

# 2. Cache Hit (Sequential)
echo "Running Test: Cache Hit (Sequential)..."
ab -X localhost:$PROXY_PORT -n 500 -c 1 "$TARGET_URL?t=baseline" > "$RESULTS_DIR/cache_hit_seq.txt" 2>&1

# 3. High Concurrency (Concurrent Cache Hits)
echo "Running Test: High Concurrency (100 concurrent threads)..."
ab -X localhost:$PROXY_PORT -n 2000 -c 100 "$TARGET_URL?t=baseline" > "$RESULTS_DIR/concurrency_100.txt" 2>&1

# 4. Cache Stampede Simulation
# We'll use a new URL and many concurrent requests to test the locking/coalescing logic
STAMPEDE_URL="http://httpbin.org/get?stampede=true"
echo "Running Test: Cache Stampede Protection..."
ab -X localhost:$PROXY_PORT -n 50 -c 50 "$STAMPEDE_URL" > "$RESULTS_DIR/stampede.txt" 2>&1

echo "Benchmarks complete. Results saved in $RESULTS_DIR"
