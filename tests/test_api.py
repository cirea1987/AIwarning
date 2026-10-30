from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_channel_rule_and_detection_flow():
    channel = client.post(
        "/api/v1/channels",
        json={"name": "测试通道", "source_type": "rtsp", "stream_url": "rtsp://example/cam"},
    ).json()
    rule = client.post("/api/v1/rules", json={"channel_id": channel["id"], "cooldown_seconds": 0}).json()
    response = client.post(
        "/api/v1/detections",
        json={"channel_id": channel["id"], "detections": [{"type": "no_helmet", "confidence": 0.9}]},
    )
    assert response.status_code == 200
    event = response.json()[0]
    assert event["rule_id"] == rule["id"]
    assert client.post(f"/api/v1/events/{event['id']}/acknowledge").json()["status"] == "acknowledged"
    assert client.post(f"/api/v1/events/{event['id']}/close").json()["status"] == "closed"


def test_register_and_upload_model(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "MODEL_DIRECTORY", tmp_path)
    model = client.post(
        "/api/v1/models",
        json={
            "name": "helmet detector",
            "framework": "modelscope",
            "source": "https://www.modelscope.cn/models/iic/cv_tinynas_object-detection_damoyolo_safety-helmet",
            "labels": ["helmet", "no_helmet"],
        },
    ).json()
    response = client.post(
        f"/api/v1/models/{model['id']}/file",
        files={"file": ("helmet.onnx", b"test-model", "application/octet-stream")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
