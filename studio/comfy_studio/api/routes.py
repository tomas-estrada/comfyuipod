"""API routes for job submission and status tracking."""

from fastapi import APIRouter
from studio.orchestration.queue import run_comfyui_job
from studio.projects import JobRequest, submit_job

router = APIRouter()

@router.post("/jobs/submit")
async def submit_job_endpoint(job: JobRequest):
    """Submit a job to the queue."""
    submit_job(job)
    return {"status": "submitted"}

@router.get("/jobs/{job_id}/status")
async def get_job_status(job_id: str):
    """Get the status of a job."""
    result = run_comfyui_job.AsyncResult(job_id)
    if not result.ready():
        return {"status": "pending"}
    return {"status": "completed", "result": result.get()}