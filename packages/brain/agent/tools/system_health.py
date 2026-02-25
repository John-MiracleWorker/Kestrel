import os
import psutil
import shutil
import socket
import time

def check_disk_usage(path="/"):
    total, used, free = shutil.disk_usage(path)
    return {
        "total_gb": total // (2**30),
        "used_gb": used // (2**30),
        "free_gb": free // (2**30),
        "percent": (used / total) * 100
    }

def check_cpu_memory():
    return {
        "cpu_percent": psutil.cpu_percent(interval=1),
        "memory_percent": psutil.virtual_memory().percent,
        "memory_used_gb": psutil.virtual_memory().used // (2**30)
    }

def check_service_port(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect((host, port))
            return True
        except:
            return False

def get_system_health():
    health = {
        "timestamp": time.time(),
        "disk": check_disk_usage(),
        "resources": check_cpu_memory(),
        "services": {
            "gateway": check_service_port("localhost", 4000),
            "brain": check_service_port("localhost", 50051),
            "hands": check_service_port("localhost", 50052)
        }
    }
    return health

if __name__ == "__main__":
    print(get_system_health())
