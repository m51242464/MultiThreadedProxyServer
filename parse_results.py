import sys
import re
import json
import os

def parse_ab_output(filepath):
    """
    Parses the text output from Apache Bench and extracts key performance metrics.
    """
    if not os.path.exists(filepath):
        return None

    with open(filepath, 'r') as f:
        content = f.read()

    data = {
        "test_name": os.path.basename(filepath).replace(".txt", ""),
        "rps": 0.0,
        "latency_ms": 0.0,
        "failed_requests": 0
    }

    # Extract Requests Per Second (RPS)
    rps_match = re.search(r"Requests per second:\s+([\d.]+)", content)
    if rps_match:
        data["rps"] = float(rps_match.group(1))

    # Extract Latency (mean time per request across all concurrent threads)
    latency_match = re.search(r"Time per request:\s+([\d.]+)\s+\[ms\]\s+\(mean\)", content)
    if latency_match:
        data["latency_ms"] = float(latency_match.group(1))

    # Extract Failed requests (non-2xx responses or connection errors)
    failed_match = re.search(r"Failed requests:\s+(\d+)", content)
    if failed_match:
        data["failed_requests"] = int(failed_match.group(1))

    return data

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 parse_results.py <ab_output_file>")
        sys.exit(1)

    input_file = sys.argv[1]
    result = parse_ab_output(input_file)
    
    if result:
        json_path = input_file.replace(".txt", ".json")
        with open(json_path, 'w') as f:
            json.dump(result, f, indent=4)
        print(f"Successfully parsed {input_file} -> {json_path}")
    else:
        print(f"Error: Could not parse {input_file}")
