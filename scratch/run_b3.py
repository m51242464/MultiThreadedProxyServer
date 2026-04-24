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

RESULTS_FILE = "scratch/b3_results.txt"
os.makedirs("scratch", exist_ok=True)

def run_bench(url, num_requests, concurrency, label):
    print(f"\n>>> Running Benchmark: {label}")
    proxies = {"http": "http://127.0.0.1:8080"}
    latencies = []
    start_time = time.time()
    def do_request():
        req_start = time.time()
        try:
            requests.get(url, proxies=proxies, timeout=10)
            latencies.append((time.time() - req_start) * 1000)
        except:
            pass
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for _ in range(num_requests): executor.submit(do_request)
    total_time = time.time() - start_time
    rps = num_requests / total_time if total_time > 0 else 0
    mean_lat = statistics.mean(latencies) if latencies else 0
    report = f"--- {label} ---\nRPS: {rps:.2f}, Mean Latency: {mean_lat:.2f} ms\n"
    print(report)
    with open(RESULTS_FILE, "a") as f: f.write(report)
    return mean_lat

def compile_and_start():
    subprocess.run("pkill -f './proxy 8080'", shell=True)
    time.sleep(1)
    subprocess.run("gcc -g -Wall -o proxy_parse.o -c proxy_parse.c -lpthread && gcc -g -Wall -o proxy.o -c proxy_server_with_cache.c -lpthread && gcc -g -Wall -o proxy proxy_parse.o proxy.o -lpthread", shell=True)
    subprocess.Popen("./proxy 8080 > proxy_b3.log 2>&1", shell=True)
    time.sleep(3)

def main():
    target_file = "proxy_server_with_cache.c"
    subprocess.run(f"cp {target_file} {target_file}.bak", shell=True)
    
    # LIST
    print("[B3] LIST (20,000 URLs)")
    content = open(target_file).read()
    content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(tempReq\);[\s\r\n]+cache_element\*\s+site\s+=\s+hash_table\[index\];", 
                     "cache_element* site = lru_head;", content)
    content = content.replace("site = site->hash_next;", "site = site->lru_next;")
    content = re.sub(r"unsigned\s+int\s+index\s+=\s+fnv1a_hash\(url\);[\s\r\n]+element->hash_next\s+=\s+hash_table\[index\];[\s\r\n]+hash_table\[index\]\s+=\s+element;",
                     "// hash disabled", content)
    open(target_file, 'w').write(content)
    compile_and_start()
    print("Pre-filling...")
    subprocess.run("seq 1 20000 | xargs -I{} -P 10 curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html?id={} > /dev/null", shell=True)
    run_bench("http://127.0.0.1:9090/index.html?id=19999", 1000, 50, "B3_LIST")
    
    # HASH
    subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
    compile_and_start()
    print("Pre-filling...")
    subprocess.run("seq 1 20000 | xargs -I{} -P 10 curl -s -x http://localhost:8080 http://127.0.0.1:9090/index.html?id={} > /dev/null", shell=True)
    run_bench("http://127.0.0.1:9090/index.html?id=19999", 1000, 50, "B3_HASH")
    
    subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)

if __name__ == "__main__":
    main()
