"""Monitor GPU memory usage and update Prometheus metrics."""
import time

import pynvml
from comfy_studio.metrics import GPU_MEMORY_TOTAL, GPU_MEMORY_USAGE


def update_gpu_metrics():
    """Update GPU memory metrics."""
    pynvml.nvmlInit()
    device_count = pynvml.nvmlDeviceGetCount()
    for i in range(device_count):
        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        GPU_MEMORY_USAGE.labels(gpu_id=i).set(info.used)
        GPU_MEMORY_TOTAL.labels(gpu_id=i).set(info.total)
    pynvml.nvmlShutdown()

def monitor_gpu():
    """Continuously monitor GPU metrics."""
    while True:
        update_gpu_metrics()
        time.sleep(15)

if __name__ == "__main__":
    monitor_gpu()