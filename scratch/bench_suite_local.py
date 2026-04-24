import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import statistics
import sys
import json
import os
import subprocess
import re

RESULTS_FILE = "scratch/benchmark_results_v4.txt"
os.makedirs("scratch", exist_ok=True)

def run_bench(url, num_requests, concurrency, label):
    print(f"\n>>> Running Benchmark: {label}")
    proxies = {"http": "http://127.0.0.1:8080"}
    latencies = []
    failed_count = 0
    errors = {}
    start_time = time.time()
    
    def do_request():
        nonlocal failed_count
        req_start = time.time()
        try:
            # Use a short timeout for local server
            resp = requests.get(url, proxies=proxies, timeout=5)
            if resp.status_code >= 400:
                failed_count += 1
                errors[resp.status_code] = errors.get(resp.status_code, 0) + 1
            else:
                latencies.append((time.time() - req_start) * 1000)
        except Exception as e:
            failed_count += 1
            err_name = type(e).__name__
            errors[err_name] = errors.get(err_name, 0) + 1

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for _ in range(num_requests):
            executor.submit(do_request)
            
    total_time = time.time() - start_time
    rps = num_requests / total_time if total_time > 0 else 0
    mean_lat = statistics.mean(latencies) if latencies else 0
    min_lat = min(latencies) if latencies else 0
    max_lat = max(latencies) if latencies else 0
    
    report = f"""
--- {label} ---
Total Requests: {num_requests}
Concurrency:    {concurrency}
Total Time:     {total_time:.3f} s
Requests/sec:   {rps:.2f}
Mean Latency:   {mean_lat:.2f} ms
Min Latency:    {min_lat:.2f} ms
Max Latency:    {max_lat:.2f} ms
Failed Requests: {failed_count}
Errors:         {json.dumps(errors)}
"""
    print(report)
    with open(RESULTS_FILE, "a") as f:
        f.write(report)
    return {"label": label, "rps": rps, "mean_lat": mean_lat, "failed": failed_count}

def compile_and_start():
    subprocess.run("pkill -f './proxy 8080'", shell=True)
    time.sleep(1)
    comp = subprocess.run("gcc -g -Wall -o proxy_parse.o -c proxy_parse.c -lpthread && gcc -g -Wall -o proxy.o -c proxy_server_with_cache.c -lpthread && gcc -g -Wall -o proxy proxy_parse.o proxy.o -lpthread", shell=True, capture_output=True, text=True)
    if comp.returncode != 0:
        print("Compilation Failed!")
        print(comp.stderr)
        return False
    subprocess.Popen("stdbuf -oL ./proxy 8080 > proxy.log 2>&1", shell=True)
    time.sleep(3)
    return True

def main():
    target_file = "proxy_server_with_cache.c"
    subprocess.run(f"cp {target_file} {target_file}.bak", shell=True)
    results = []
    
    try:
        # B2: Mutex vs RWLock
        print("\n[B2] Rerunning Mutex vs RWLock on Local Server...")
        content = open(target_file).read()
        content = content.replace("pthread_rwlock_t rwlock;", "pthread_mutex_t rwlock;")
        content = content.replace("pthread_rwlock_init(&rwlock,NULL);", "pthread_mutex_init(&rwlock,NULL);")
        content = content.replace("pthread_rwlock_rdlock(&rwlock)", "pthread_mutex_lock(&rwlock)")
        content = content.replace("pthread_rwlock_wrlock(&rwlock)", "pthread_mutex_lock(&rwlock)")
        content = content.replace("pthread_rwlock_unlock(&rwlock)", "pthread_mutex_unlock(&rwlock)")
        open(target_file, 'w').write(content)
        if compile_and_start():
            # Warm cache with 100 requests
            print("Warming cache (100 requests)...")
            subprocess.run("for i in {1..100}; do curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html > /dev/null; done", shell=True)
            results.append(run_bench("http://127.0.0.1:9090/index.html", 2000, 1000, "B2_MUTEX_LOCAL"))
        
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        if compile_and_start():
            # Warm cache with 100 requests
            print("Warming cache (100 requests)...")
            subprocess.run("for i in {1..100}; do curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html > /dev/null; done", shell=True)
            results.append(run_bench("http://127.0.0.1:9090/index.html", 2000, 1000, "B2_RWLOCK_LOCAL"))
        
        # B3: List vs Hash
        print("\n[B3] Rerunning Linked List vs Hashtable on Local Server (5000 URLs)...")
        content = open(target_file).read()
        content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(tempReq\);[\s\r\n]+cache_element\*\s+site\s+=\s+hash_table\[index\];", 
                         "cache_element* site = lru_head;", content)
        content = content.replace("site = site->hash_next;", "site = site->lru_next;")
        content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(url\);[\s\r\n]+element->hash_next\s+=\s+hash_table\[index\];[\s\r\n]+hash_table\[index\]\s+=\s+element;",
                         "// hash disabled", content)
        open(target_file, 'w').write(content)
        if compile_and_start():
            print("Pre-filling 50000 URLs (Parallel)...")
            subprocess.run("seq 1 50000 | xargs -I{} -P 20 curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html?id={} > /dev/null", shell=True)
            results.append(run_bench("http://127.0.0.1:9090/index.html?id=49999", 1000, 50, "B3_LIST_LOCAL"))
        
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        if compile_and_start():
            print("Pre-filling 50000 URLs (Parallel)...")
            subprocess.run("seq 1 50000 | xargs -I{} -P 20 curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html?id={} > /dev/null", shell=True)
            results.append(run_bench("http://127.0.0.1:9090/index.html?id=49999", 1000, 50, "B3_HASH_LOCAL"))

    finally:
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        subprocess.run("pkill -f './proxy 8080'", shell=True)
    print("\nFINAL SUMMARY DATA (B2 & B3 RERUN)\n" + json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
