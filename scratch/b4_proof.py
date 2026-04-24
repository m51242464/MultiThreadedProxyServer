import requests
import time
import subprocess
import os
import concurrent.futures
import re

def modify_file(target_file, search_str, replace_str):
    with open(target_file, 'r') as f:
        content = f.read()
    new_content = content.replace(search_str, replace_str)
    with open(target_file, 'w') as f:
        f.write(new_content)

def compile_and_start():
    subprocess.run("pkill -f './proxy 8080'", shell=True)
    time.sleep(1)
    subprocess.run("gcc -g -Wall -o proxy_parse.o -c proxy_parse.c -lpthread && gcc -g -Wall -o proxy.o -c proxy_server_with_cache.c -lpthread && gcc -g -Wall -o proxy proxy_parse.o proxy.o -lpthread", shell=True)
    # Use stderr for logging to avoid buffering issues
    subprocess.Popen("./proxy 8080 > proxy_b4.log 2>&1", shell=True)
    time.sleep(3)

def run_concurrent(url, count):
    proxies = {"http": "http://127.0.0.1:8080"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(requests.get, url, proxies=proxies, timeout=20) for _ in range(count)]
        concurrent.futures.wait(futures)

def main():
    target_file = "proxy_server_with_cache.c"
    subprocess.run(f"cp {target_file} {target_file}.bak", shell=True)
    
    # Add origin fetch log with stderr and fflush
    modify_file(target_file, "int handle_request(int socket, struct ParsedRequest *request, char *tempReq)\n{", 
                "int handle_request(int socket, struct ParsedRequest *request, char *tempReq)\n{\n    fprintf(stderr, \"FETCHING_FROM_ORIGIN: %s\\n\", tempReq);\n    fflush(stderr);\n    usleep(1000000);")
    
    # 1. NO PROTECTION
    print("[B4] Running NO PROTECTION (with 1s origin latency)...")
    content = open(target_file).read()
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
    open(target_file, 'w').write(content)
    compile_and_start()
    
    subprocess.run("echo '' > proxy_b4.log", shell=True)
    run_concurrent(f"http://127.0.0.1:9090/index.html?stampede=no_{int(time.time())}", 10)
    time.sleep(2)
    count_no = subprocess.check_output("grep -c 'FETCHING_FROM_ORIGIN' proxy_b4.log || true", shell=True).strip().decode()
    print(f"B4_NO_PROT Count: {count_no}")
    
    # 2. WITH PROTECTION
    print("[B4] Running WITH PROTECTION (with 1s origin latency)...")
    subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)
    modify_file(target_file, "int handle_request(int socket, struct ParsedRequest *request, char *tempReq)\n{", 
                "int handle_request(int socket, struct ParsedRequest *request, char *tempReq)\n{\n    fprintf(stderr, \"FETCHING_FROM_ORIGIN: %s\\n\", tempReq);\n    fflush(stderr);\n    usleep(1000000);")
    compile_and_start()
    
    subprocess.run("echo '' > proxy_b4.log", shell=True)
    run_concurrent(f"http://127.0.0.1:9090/index.html?stampede=yes_{int(time.time())}", 10)
    time.sleep(2)
    count_yes = subprocess.check_output("grep -c 'FETCHING_FROM_ORIGIN' proxy_b4.log || true", shell=True).strip().decode()
    print(f"B4_WITH_PROT Count: {count_yes}")
    
    subprocess.run(f"cp {target_file}.bak {target_file}", shell=True)

if __name__ == "__main__":
    main()
