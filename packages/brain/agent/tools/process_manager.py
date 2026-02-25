import psutil
import os

def list_processes(filter_name=None):
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_info']):
        try:
            info = proc.info
            if filter_name and filter_name.lower() not in info['name'].lower():
                continue
            processes.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return sorted(processes, key=lambda x: x.get('cpu_percent', 0), reverse=True)[:20]

def kill_process(pid):
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        return f"Process {pid} terminated."
    except Exception as e:
        return f"Error killing process {pid}: {str(e)}"

if __name__ == "__main__":
    import json
    print(json.dumps(list_processes(), indent=2))
