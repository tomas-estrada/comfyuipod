from celery import Celery
from studio.config import REDIS_URL

app = Celery("comfyui_pod", broker=REDIS_URL, backend=REDIS_URL)

@app.task(bind=True, max_retries=3)
def run_comfyui_job(self, job_data: dict):
    """Run a ComfyUI job with retries."""
    try:
        # Existing job logic
        pass
    except Exception as e:
        self.retry(exc=e, countdown=60)