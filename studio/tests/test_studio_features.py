"""Tests for studio features."""

from fastapi.testclient import TestClient
from studio.comfy_studio.app import create_app

client = TestClient(create_app())


def test_template_rendering():
    """Test template rendering."""
    response = client.post(
        "/studio/api/templates/render",
        json={"template": "test_template", "params": {}},
    )
    assert response.status_code == 200
    assert "rendered_template" in response.json()


def test_model_manifest_validation():
    """Test model manifest validation."""
    response = client.post(
        "/studio/api/models/validate",
        json={"manifest": "test_manifest"},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_api_endpoint_accessibility():
    """Test API endpoint accessibility."""
    response = client.get("/studio/api/status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"