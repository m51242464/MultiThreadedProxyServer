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

RESULTS_FILE = "scratch/benchmark_results.txt"
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
            resp = requests.get(url, proxies=proxies, timeout=15)
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
        # B1
        if compile_and_start():
            results.append(run_bench("http://httpbin.org/get?b1=1", 200, 1, "B1_MISS"))
            requests.get("http://httpbin.org/get?b1=1", proxies={"http": "http://127.0.0.1:8080"})
            results.append(run_bench("http://httpbin.org/get?b1=1", 200, 1, "B1_HIT"))
        
        # B2: Mutex
        print("\n[B2] Downgrading to Mutex...")
        content = open(target_file).read()
        content = content.replace("pthread_rwlock_t rwlock;", "pthread_mutex_t rwlock;")
        content = re.sub(r"pthread_rwlock_init\(&rwlock,\s*NULL\);", "pthread_mutex_init(&rwlock, NULL);", content)
        content = content.replace("pthread_rwlock_rdlock(&rwlock)", "pthread_mutex_lock(&rwlock)")
        content = content.replace("pthread_rwlock_wrlock(&rwlock)", "pthread_mutex_lock(&rwlock)")
        content = content.replace("pthread_rwlock_unlock(&rwlock)", "pthread_mutex_unlock(&rwlock)")
        open(target_file, 'w').write(content)
        if compile_and_start():
            results.append(run_bench("http://httpbin.org/get?b2=1", 2000, 400, "B2_MUTEX"))
        
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        if compile_and_start():
            results.append(run_bench("http://httpbin.org/get?b2=2", 2000, 400, "B2_RWLOCK"))
        
        # B3: List
        print("\n[B3] Downgrading to Linked List scan...")
        content = open(target_file).read()
        content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(tempReq\);[\s\r\n]+cache_element\*\s+site\s+=\s+hash_table\[index\];", 
                         "cache_element* site = lru_head;", content)
        content = content.replace("site = site->hash_next;", "site = site->lru_next;")
        content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(url\);[\s\r\n]+element->hash_next\s+=\s+hash_table\[index\];[\s\r\n]+hash_table\[index\]\s+=\s+element;",
                         "// hash disabled", content)
        open(target_file, 'w').write(content)
        if compile_and_start():
            print("Pre-filling 500 URLs...")
            subprocess.run("for i in {1..500}; do curl -s -x http://localhost:8080 http://httpbin.org/get?id=$i > /dev/null; done", shell=True)
            results.append(run_bench("http://httpbin.org/get?id=499", 1000, 50, "B3_LIST"))
        
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        if compile_and_start():
            print("Pre-filling 500 URLs...")
            subprocess.run("for i in {1..500}; do curl -s -x http://localhost:8080 http://httpbin.org/get?id=$i > /dev/null; done", shell=True)
            results.append(run_bench("http://httpbin.org/get?id=499", 1000, 50, "B3_HASH"))

        # B4: Stampede
        print("\n[B4] Disabling Stampede Protection...")
        content = open(target_file).read()
        # More robust replacement for the pending block
        search_pattern = r"pending_entry_t\*\s+pending\s+=\s+pending_wait_or_register\(tempReq\);.*?if\s*\(pending\s*!=\s*NULL\)\s*\{.*?\}\s*else\s*\{"
        # We need to find the matching brace for the 'else' block. 
        # But since we know the structure, we can just replace up to handle_request and then handle the braces.
        # Actually, the easiest way is to just replace the whole if/else logic with a single call.
        # Let's find the 'else {' and its matching '}'
        
        content = re.sub(search_pattern, "if (1) {", content, flags=re.DOTALL)
        content = content.replace("pending_complete(tempReq);", "")
        
        open(target_file, 'w').write(content)
        if compile_and_start():
            subprocess.run("echo '' > proxy.log", shell=True)
            run_bench("http://httpbin.org/get?stampede=1", 10, 10, "B4_NO_PROT")
            fetch_count = subprocess.check_output("grep -c 'Add Cache Lock Acquired' proxy.log || true", shell=True).strip().decode()
            results.append({"label": "B4_NO_PROT_FETCHES", "fetches": int(fetch_count)})
            
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        if compile_and_start():
            subprocess.run("echo '' > proxy.log", shell=True)
            run_bench("http://httpbin.org/get?stampede=2", 10, 10, "B4_WITH_PROT")
            fetch_count = subprocess.check_output("grep -c 'Add Cache Lock Acquired' proxy.log || true", shell=True).strip().decode()
            results.append({"label": "B4_WITH_PROT_FETCHES", "fetches": int(fetch_count)})

    finally:
        subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
        subprocess.run("pkill -f './proxy 8080'", shell=True)
    print("\nFINAL SUMMARY DATA\n" + json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
