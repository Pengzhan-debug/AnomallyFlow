from io import BytesIO
import os

from PIL import Image


def upload_demo_task(client, key: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": "inspector.demo",
                           "password": os.environ["DEMO_INSPECTOR_PASSWORD"]})
    response.raise_for_status()
    image = BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    response = client.post("/api/v1/inspections", headers={
        "Authorization": "Bearer " + response.json()["access_token"], "Idempotency-Key": key},
        data={"batch_id": "BA-2026-1003", "product_code": "VALVE-A"},
        files={"image": ("demo.png", image.getvalue(), "image/png")})
    response.raise_for_status()
    return response.json()["task_id"]
