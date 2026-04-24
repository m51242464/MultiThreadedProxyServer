import requests
import time
import statistics

def run_bench(url, num_requests, concurrency, label):
    proxies = {"http": "http://127.0.0.1:8080"}
    latencies = []
    
    # Sequential for mean latency measurement as per B1 requirement (concurrency 1)
    for i in range(num_requests):
        start = time.time()
        try:
            r = requests.get(url, proxies=proxies, timeout=5)
            latencies.append((time.time() - start) * 1000)
        except Exception as e:
            print(f"Error: {e}")
            
    mean_lat = statistics.mean(latencies) if latencies else 0
    print(f"{label} Mean Latency: {mean_lat:.2f} ms")
    return mean_lat

if __name__ == "__main__":
    url = "http://127.0.0.1:9090/index.html?b1=" + str(time.time())
    
    print("--- BENCHMARK 1: Cache Miss vs Hit ---")
    # Miss
    miss_lat = run_bench(url, 200, 1, "MISS")
    
    # Warmup
    requests.get(url, proxies={"http": "http://127.0.0.1:8080"})
    
    # Hit
    hit_lat = run_bench(url, 200, 1, "HIT")
    
    speedup = miss_lat / hit_lat if hit_lat > 0 else 0
    print(f"Speedup: {speedup:.2f}x faster")
