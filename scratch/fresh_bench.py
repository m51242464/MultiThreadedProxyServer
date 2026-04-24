import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import statistics
import json
import os
import sys

def run_bench(url, num_requests, concurrency, label):
    # Use proxy for all requests
    proxies = {"http": "http://127.0.0.1:8080"}
    latencies = []
    failed_count = 0
    start_time = time.time()
    
    def do_request():
        nonlocal failed_count
        req_start = time.time()
        try:
            resp = requests.get(url, proxies=proxies, timeout=10)
            if resp.status_code >= 400:
                failed_count += 1
            else:
                latencies.append((time.time() - req_start) * 1000)
        except:
            failed_count += 1

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for _ in range(num_requests):
            executor.submit(do_request)
            
    total_time = time.time() - start_time
    rps = num_requests / total_time if total_time > 0 else 0
    mean_lat = statistics.mean(latencies) if latencies else 0
    
    print(f"\n--- {label} ---")
    print(f"Requests/sec: {rps:.2f}")
    print(f"Mean Latency: {mean_lat:.2f} ms")
    print(f"Failed Requests: {failed_count}")
    return {"rps": rps, "mean_lat": mean_lat, "failed": failed_count}

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: fresh_bench.py <url> <num_requests> <concurrency> <label>")
        sys.exit(1)
    run_bench(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
