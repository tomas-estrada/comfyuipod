"""Tests for job queue functionality."""

from fastapi.testclient import TestClient
from studio.comfy_studio.app import create_app

client = TestClient(create_app())


def test_job_submission():
    """Test job submission and prioritization."""
    response = client.post(
        "/studio/api/jobs",
        json={"task": "test_task", "priority": "high"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_resource_allocation():
    """Test GPU and CPU resource allocation."""
    response = client.post(
        "/studio/api/jobs",
        json={"task": "test_task", "gpu": True, "cpu": 2},
    )
    assert response.status_code == 200
    assert response.json()["resources"]["gpu"] is True
    assert response.json()["resources"]["cpu"] == 2


def test_error_handling():
    """Test error handling for failed jobs."""
    response = client.post(
        "/studio/api/jobs",
        json={"task": "invalid_task"},
    )
    assert response.status_code == 400
    assert "error" in response.json()