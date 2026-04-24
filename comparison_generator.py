import json
import os

def generate_report():
    """
    Aggregates all JSON benchmark results and prints a comparison table.
    """
    results_dir = "./benchmark_results"
    if not os.path.exists(results_dir):
        print(f"Error: {results_dir} directory not found.")
        return

    files = [f for f in os.listdir(results_dir) if f.endswith(".json")]
    
    data_list = []
    for f in files:
        with open(os.path.join(results_dir, f), 'r') as j:
            data_list.append(json.load(j))

    if not data_list:
        print("No JSON results found. Please run the parser first.")
        return

    # Identify 'baseline' for percentage comparison
    baseline = next((d for d in data_list if d['test_name'] == 'baseline'), None)
    
    print("\n" + "="*80)
    print(" PROXY SERVER PERFORMANCE COMPARISON")
    print("="*80)
    print(f"| {'Test Case':<25} | {'RPS':<10} | {'Latency (ms)':<15} | {'Failed':<8} | {'vs Baseline':<12} |")
    print("|" + "-"*27 + "|" + "-"*12 + "|" + "-"*17 + "|" + "-"*10 + "|" + "-"*14 + "|")
    
    # Sort by test name for consistent output
    for d in sorted(data_list, key=lambda x: x['test_name']):
        improv = "N/A"
        if baseline and baseline['rps'] > 0 and d['test_name'] != 'baseline':
            diff = ((d['rps'] - baseline['rps']) / baseline['rps']) * 100
            improv = f"{diff:+.2f}%"
        
        print(f"| {d['test_name']:<25} | {d['rps']:<10.2f} | {d['latency_ms']:<15.2f} | {d['failed_requests']:<8} | {improv:<12} |")
    print("="*80 + "\n")

if __name__ == "__main__":
    generate_report()
