"""Prometheus metrics for studio-server."""
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# ComfyUI Metrics
COMFYUI_RESTARTS = Counter(
    'comfyui_restarts_total', 
    'Total number of ComfyUI restarts'
)

MODEL_DOWNLOADS = Counter(
    'model_downloads_total', 
    'Total number of model downloads', 
    ['model_name', 'status']
)

# API Metrics
API_LATENCY = Histogram(
    'studio_api_latency_seconds', 
    'Latency of API endpoints in seconds', 
    ['endpoint']
)

API_ERRORS = Counter(
    'studio_api_errors_total', 
    'Total number of API errors', 
    ['endpoint', 'status_code']
)

# GPU Metrics
GPU_MEMORY_USAGE = Gauge(
    'gpu_memory_usage_bytes', 
    'GPU memory usage in bytes', 
    ['gpu_id']
)
GPU_MEMORY_TOTAL = Gauge(
    'gpu_memory_total_bytes', 
    'Total GPU memory in bytes', 
    ['gpu_id']
)

def start_metrics_server(port: int = 8000) -> None:
    """Start the Prometheus metrics server."""
    start_http_server(port)