import requests
import time
import concurrent.futures
import statistics

def run_concurrency_bench(url, num_requests, concurrency, label):
    proxies = {"http": "http://127.0.0.1:8080"}
    latencies = []
    failed = 0
    
    # Warmup
    for i in range(50):
        try: requests.get(url, proxies=proxies, timeout=5)
        except: pass
        
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(requests.get, url, proxies=proxies, timeout=10) for _ in range(num_requests)]
        for future in concurrent.futures.as_completed(futures):
            try:
                resp = future.result()
                latencies.append(resp.elapsed.total_seconds() * 1000)
            except Exception as e:
                failed += 1
                
    total_time = time.time() - start_time
    rps = num_requests / total_time
    mean_lat = statistics.mean(latencies) if latencies else 0
    
    print(f"--- {label} ---")
    print(f"Requests/sec: {rps:.2f}")
    print(f"Mean Latency: {mean_lat:.2f} ms")
    print(f"Failed Requests: {failed}")
    return rps, mean_lat, failed

if __name__ == "__main__":
    url = "http://127.0.0.1:9090/index.html"
    run_concurrency_bench(url, 2000, 400, "RWLock (Current)")
