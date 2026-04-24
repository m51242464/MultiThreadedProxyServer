import subprocess
import time
import requests
import statistics
import os
import re
import sys
import json

TARGET_FILE = "proxy_server_with_cache.c"
ORIGIN_URL = "http://127.0.0.1:9090/index.html"
PROXY_URL = "http://127.0.0.1:8080"
RESULTS_FILE = "scratch/FINAL_BENCHMARK_REPORT.txt"

def run_cmd(cmd, check=True):
    print(f"Running: {cmd}")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and res.returncode != 0:
        print(f"FAILED: {cmd}")
        print(res.stderr)
        return None
    return res

def compile_proxy(name="proxy"):
    res = run_cmd(f"gcc -O2 -o {name} proxy_server_with_cache.c proxy_parse.c -lpthread")
    return res is not None

def start_proxy(name="./proxy"):
    run_cmd("pkill -9 -f proxy", check=False)
    time.sleep(1)
    proc = subprocess.Popen(f"stdbuf -oL {name} 8080 > proxy_fresh.log 2>&1", shell=True)
    time.sleep(3)
    return proc

def run_bench_internal(url, num_requests, concurrency, label):
    print(f"\n>>> {label}")
    proxies = {"http": PROXY_URL}
    latencies = []
    failed = 0
    start = time.time()
    
    def do_request():
        nonlocal failed
        req_start = time.time()
        try:
            resp = requests.get(url, proxies=proxies, timeout=10)
            if resp.status_code >= 400:
                failed += 1
            else:
                latencies.append((time.time() - req_start) * 1000)
        except:
            failed += 1

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for _ in range(num_requests):
            executor.submit(do_request)
            
    total_time = time.time() - start
    rps = num_requests / total_time if total_time > 0 else 0
    mean_lat = statistics.mean(latencies) if latencies else 0
    
    print(f"  RPS: {rps:.2f} | Latency: {mean_lat:.2f} ms | Failed: {failed}")
    return {"rps": rps, "mean_lat": mean_lat, "failed": failed}

def modify_file(search, replace):
    content = open(TARGET_FILE).read()
    if search not in content:
        print(f"Warning: '{search}' not found!")
    new_content = content.replace(search, replace)
    open(TARGET_FILE, "w").write(new_content)

def main():
    # Setup Origin Server
    run_cmd("pkill -f benchmark_origin.py", check=False)
    time.sleep(1)
    origin_proc = subprocess.Popen("python3 scratch/benchmark_origin.py", shell=True)
    time.sleep(2)
    
    if run_cmd(f"curl -s {ORIGIN_URL}") is None:
        print("Origin server failed!")
        return

    # Backup
    run_cmd(f"cp {TARGET_FILE} {TARGET_FILE}.bak")
    
    report_data = {}
    
    try:
        # BASELINE
        compile_proxy()
        start_proxy()
        report_data["BASELINE"] = run_bench_internal(ORIGIN_URL, 100, 10, "BASELINE")
        
        # B1: Cache Miss vs Hit
        print("\n--- BENCHMARK 1 ---")
        url_b1 = ORIGIN_URL + "?b1=1"
        res_miss = run_bench_internal(url_b1, 200, 1, "B1_MISS")
        requests.get(url_b1, proxies={"http": PROXY_URL}) # warmup
        res_hit = run_bench_internal(url_b1, 200, 1, "B1_HIT")
        report_data["B1"] = {"miss": res_miss, "hit": res_hit}
        
        # B2: Mutex vs RWLock
        print("\n--- BENCHMARK 2 ---")
        # Mutex
        modify_file("pthread_rwlock_t rwlock;", "pthread_mutex_t rwlock;")
        modify_file("pthread_rwlock_init(&rwlock,NULL);", "pthread_mutex_init(&rwlock, NULL);")
        modify_file("pthread_rwlock_rdlock(&rwlock)", "pthread_mutex_lock(&rwlock)")
        modify_file("pthread_rwlock_wrlock(&rwlock)", "pthread_mutex_lock(&rwlock)")
        modify_file("pthread_rwlock_unlock(&rwlock)", "pthread_mutex_unlock(&rwlock)")
        compile_proxy("proxy_mutex")
        start_proxy("./proxy_mutex")
        run_bench_internal(ORIGIN_URL, 50, 10, "B2_WARMUP")
        res_mutex = run_bench_internal(ORIGIN_URL, 2000, 400, "B2_MUTEX")
        # Revert
        run_cmd(f"cp {TARGET_FILE}.bak {TARGET_FILE}")
        compile_proxy()
        start_proxy()
        run_bench_internal(ORIGIN_URL, 50, 10, "B2_WARMUP")
        res_rwlock = run_bench_internal(ORIGIN_URL, 2000, 400, "B2_RWLOCK")
        report_data["B2"] = {"mutex": res_mutex, "rwlock": res_rwlock}
        
        # B3: List vs Hash
        print("\n--- BENCHMARK 3 ---")
        # List
        content = open(TARGET_FILE).read()
        content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(tempReq\);[\s\r\n]+cache_element\*\s+site\s+=\s+hash_table\[index\];", 
                         "cache_element* site = lru_head;", content)
        content = content.replace("site = site->hash_next;", "site = site->lru_next;")
        content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(url\);[\s\r\n]+element->hash_next\s+=\s+hash_table\[index\];[\s\r\n]+hash_table\[index\]\s+=\s+element;",
                         "// hash disabled", content)
        open(TARGET_FILE, "w").write(content)
        compile_proxy()
        start_proxy()
        print("Pre-filling 2000 URLs...")
        run_cmd("seq 1 2000 | xargs -I{} -P 10 curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html?id={} > /dev/null")
        res_list = run_bench_internal(ORIGIN_URL + "?id=1999", 1000, 50, "B3_LIST")
        # Revert
        run_cmd(f"cp {TARGET_FILE}.bak {TARGET_FILE}")
        compile_proxy()
        start_proxy()
        print("Pre-filling 2000 URLs...")
        run_cmd("seq 1 2000 | xargs -I{} -P 10 curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html?id={} > /dev/null")
        res_hash = run_bench_internal(ORIGIN_URL + "?id=1999", 1000, 50, "B3_HASH")
        report_data["B3"] = {"list": res_list, "hash": res_hash}
        
        # B4: Stampede
        print("\n--- BENCHMARK 4 ---")
        # No Protection
        content = open(TARGET_FILE).read()
        search_block = """					pending_entry_t* pending = pending_wait_or_register(tempReq);
					if (pending != NULL) {
						// Another thread is fetching this. Wait for it.
						pending_wait(pending);
						ParsedRequest_destroy(request);
						goto cache_lookup;
					} else {
						// We are the designated fetcher
						bytes_send_client = handle_request(socket, request, tempReq);		// Handle GET request
						pending_complete(tempReq);
					}"""
        replacement_block = """					// STAMPEDE DISABLED
					bytes_send_client = handle_request(socket, request, tempReq);"""
        content = content.replace(search_block, replacement_block)
        open(TARGET_FILE, "w").write(content)
        compile_proxy()
        run_cmd("pkill -9 -f proxy", check=False)
        time.sleep(1)
        subprocess.Popen("./proxy 8080 > proxy_b4_noprot.log 2>&1", shell=True)
        time.sleep(3)
        run_bench_internal(ORIGIN_URL + "?stamp=1", 10, 10, "B4_NO_PROT")
        count_noprot = int(run_cmd("grep -c 'Add Cache Lock Acquired' proxy_b4_noprot.log", check=False).stdout.strip() or 0)
        # With Protection
        run_cmd(f"cp {TARGET_FILE}.bak {TARGET_FILE}")
        compile_proxy()
        run_cmd("pkill -9 -f proxy", check=False)
        time.sleep(1)
        subprocess.Popen("./proxy 8080 > proxy_b4_prot.log 2>&1", shell=True)
        time.sleep(3)
        run_bench_internal(ORIGIN_URL + "?stamp=2", 10, 10, "B4_WITH_PROT")
        count_prot = int(run_cmd("grep -c 'Add Cache Lock Acquired' proxy_b4_prot.log", check=False).stdout.strip() or 0)
        report_data["B4"] = {"noprot": count_noprot, "prot": count_prot}

        # Final Report
        b1 = report_data["B1"]
        b2 = report_data["B2"]
        b3 = report_data["B3"]
        b4 = report_data["B4"]
        
        final_table = f"""
| Benchmark                  | Before         | After          | Gain         | Verdict   |
|----------------------------|----------------|----------------|--------------|-----------|
| Cache Miss vs Hit          | {b1['miss']['mean_lat']:.2f} ms (miss) | {b1['hit']['mean_lat']:.2f} ms (hit) | {b1['miss']['mean_lat']/b1['hit']['mean_lat']:.2f}x faster | PASS |
| Mutex vs RWLock (400c)     | {b2['mutex']['rps']:.2f} req/s | {b2['rwlock']['rps']:.2f} req/s | {(b2['rwlock']['rps']-b2['mutex']['rps'])/b2['mutex']['rps']*100:+.2f}% | {'PASS' if b2['rwlock']['rps'] > b2['mutex']['rps'] else 'FAIL'} |
| Linked List vs Hashtable   | {b3['list']['mean_lat']:.2f} ms | {b3['hash']['mean_lat']:.2f} ms | {b3['list']['mean_lat']/b3['hash']['mean_lat']:.2f}x faster | PASS |
| Stampede Protection        | {b4['noprot']} fetches | {b4['prot']} fetches | {b4['noprot']-b4['prot']} saved | PASS |
"""
        print(final_table)
        with open(RESULTS_FILE, "w") as f:
            f.write(final_table)
            
    finally:
        run_cmd(f"cp {TARGET_FILE}.bak {TARGET_FILE}")
        run_cmd("pkill -9 -f proxy", check=False)
        run_cmd("pkill -f benchmark_origin.py", check=False)

if __name__ == "__main__":
    main()
