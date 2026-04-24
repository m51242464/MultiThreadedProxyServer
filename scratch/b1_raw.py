import socket
import time

def send_raw_request(host, port, raw_msg):
    start = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        s.sendall(raw_msg.encode())
        response = b""
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            response += chunk
        s.close()
    except Exception as e:
        print(f"Error: {e}")
    return (time.time() - start) * 1000

if __name__ == "__main__":
    proxy_host = "127.0.0.1"
    proxy_port = 8080
    raw_req = "GET http://127.0.0.1:9090/index.html HTTP/1.1\r\nHost: 127.0.0.1:9090\r\n\r\n"
    
    print("--- RAW REQUEST BENCHMARK ---")
    # Miss
    lat1 = send_raw_request(proxy_host, proxy_port, raw_req)
    print(f"Miss Latency: {lat1:.2f} ms")
    
    time.sleep(1)
    
    # Hit
    latencies = []
    for _ in range(100):
        latencies.append(send_raw_request(proxy_host, proxy_port, raw_req))
    
    print(f"Hit Mean Latency: {sum(latencies)/len(latencies):.2f} ms")
