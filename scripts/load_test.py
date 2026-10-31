"""Load testing for ComfyUIPod."""

from locust import HttpUser, between, task


class ComfyUIPodUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def submit_job(self):
        self.client.post(
            "/studio/api/jobs",
            json={"task": "test_task", "priority": "high"},
        )

    @task
    def check_status(self):
        self.client.get("/studio/api/status")

    @task
    def render_template(self):
        self.client.post(
            "/studio/api/templates/render",
            json={"template": "test_template", "params": {}},
        )