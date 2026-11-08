from fastapi.testclient import TestClient
from pathlib import Path
import time

from app.main import app

client = TestClient(app)


def test_channel_rule_and_detection_flow():
    channel = client.post(
        "/api/v1/channels",
        json={"name": "测试通道", "source_type": "rtsp", "stream_url": "rtsp://example/cam"},
    ).json()
    rule = client.post(
        "/api/v1/rules",
        json={"channel_id": channel["id"], "duration_seconds": 0, "cooldown_seconds": 0},
    ).json()
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


def test_quick_create_channel_with_default_rule():
    response = client.post(
        "/api/v1/channels/quick-create",
        json={
            "name": "一键通道",
            "source_type": "rtsp",
            "stream_url": "rtsp://example/quick-cam",
            "preview_url": "https://example.com/quick.m3u8",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["channel"]["name"] == "一键通道"
    assert payload["channel"]["preview_url"] == "https://example.com/quick.m3u8"
    assert payload["rule"]["channel_id"] == payload["channel"]["id"]
    assert payload["rule"]["alarm_type"] == "no_helmet"


def test_channel_preview_info():
    channel = client.post(
        "/api/v1/channels",
        json={
            "name": "预览通道",
            "source_type": "rtsp",
            "stream_url": "rtsp://example/live",
            "preview_url": "https://example.com/live.mp4",
        },
    ).json()
    preview = client.get(f"/api/v1/channels/{channel['id']}/preview")
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["playable_in_browser"] is True
    assert payload["preview_url"] == "https://example.com/live.mp4"


def test_batch_preview_channels_csv():
    content = (
        "name,source_type,stream_url\n"
        "北门通道,rtsp,rtsp://example/north\n"
        "坏通道,invalid,rtsp://example/bad\n"
    ).encode("utf-8")
    response = client.post(
        "/api/v1/channels/batch-preview",
        files={"file": ("channels.csv", content, "text/csv")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["valid_count"] == 1
    assert payload["invalid_count"] == 1
    assert payload["rows"][1]["valid"] is False


def test_batch_create_channels_with_default_rule():
    response = client.post(
        "/api/v1/channels/batch-create",
        json={
            "rows": [
                {"name": "批量通道1", "source_type": "rtsp", "stream_url": "rtsp://example/b1"},
                {"name": "批量通道2", "source_type": "gb28181", "stream_url": "device://34020000001320000001"},
            ]
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["created_count"] == 2
    assert payload["failed_count"] == 0
    assert payload["channels"][0]["rule"]["alarm_type"] == "no_helmet"


def test_rule_with_roi_polygon_filters_detection():
    channel = client.post(
        "/api/v1/channels",
        json={"name": "ROI通道", "source_type": "rtsp", "stream_url": "rtsp://example/roi"},
    ).json()
    rule = client.post(
        "/api/v1/rules",
        json={
            "channel_id": channel["id"],
            "alarm_type": "intrusion",
            "duration_seconds": 0,
            "cooldown_seconds": 0,
            "roi_polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
        },
    )
    assert rule.status_code == 201
    hit = client.post(
        "/api/v1/detections",
        json={
            "channel_id": channel["id"],
            "detections": [{"type": "intrusion", "confidence": 0.95, "bbox": [0.2, 0.2, 0.3, 0.3]}],
        },
    )
    miss = client.post(
        "/api/v1/detections",
        json={
            "channel_id": channel["id"],
            "detections": [{"type": "intrusion", "confidence": 0.95, "bbox": [0.95, 0.95, 0.99, 0.99]}],
        },
    )
    assert hit.status_code == 200
    assert len(hit.json()) == 1
    assert miss.status_code == 200
    assert len(miss.json()) == 0


def test_detection_waits_for_rule_duration():
    channel = client.post(
        "/api/v1/channels",
        json={"name": "持续时长通道", "source_type": "rtsp", "stream_url": "rtsp://example/duration"},
    ).json()
    client.post(
        "/api/v1/rules",
        json={"channel_id": channel["id"], "duration_seconds": 0.05, "cooldown_seconds": 60},
    )
    detection = {
        "channel_id": channel["id"],
        "detections": [{"type": "no_helmet", "confidence": 0.95}],
    }
    assert client.post("/api/v1/detections", json=detection).json() == []
    time.sleep(0.08)
    assert len(client.post("/api/v1/detections", json=detection).json()) == 1
    assert client.post("/api/v1/detections", json=detection).json() == []
    empty = client.post("/api/v1/detections", json={"channel_id": channel["id"], "detections": []})
    assert empty.json() == []


def test_detection_saves_image_and_position(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "EVENT_IMAGE_DIRECTORY", tmp_path)
    channel = client.post(
        "/api/v1/channels",
        json={"name": "留证通道", "source_type": "rtsp", "stream_url": "rtsp://example/evidence"},
    ).json()
    client.post(
        "/api/v1/rules",
        json={"channel_id": channel["id"], "duration_seconds": 0, "cooldown_seconds": 0, "alarm_type": "no_helmet"},
    )
    response = client.post(
        "/api/v1/detections",
        json={
            "channel_id": channel["id"],
            "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAoMBgQfS6l0AAAAASUVORK5CYII=",
            "detections": [{"type": "no_helmet", "confidence": 0.91, "bbox": [0.2, 0.3, 0.4, 0.5]}],
        },
    )
    assert response.status_code == 200
    event = response.json()[0]
    assert event["alarm_position"]["bbox"] == [0.2, 0.3, 0.4, 0.5]
    saved_path = Path(event["snapshot_path"])
    assert saved_path.exists()
    image_resp = client.get(f"/api/v1/events/{event['id']}/image")
    assert image_resp.status_code == 200


def test_onvif_probe_and_quick_create(monkeypatch):
    from app import main

    def fake_detect_onvif_stream(**_: dict):
        return {
            "stream_url": "rtsp://camera.local/Streaming/Channels/101",
            "profile_token": "profile-main",
            "media_service_url": "http://camera.local/onvif/media_service",
            "device_service_url": "http://camera.local/onvif/device_service",
            "transport": "RTSP",
        }

    monkeypatch.setattr(main, "detect_onvif_stream", fake_detect_onvif_stream)
    probe = client.post(
        "/api/v1/onvif/probe-stream",
        json={"host": "camera.local", "port": 80, "username": "admin", "password": "pass12345", "transport": "RTSP"},
    )
    assert probe.status_code == 200
    stream = probe.json()
    created = client.post(
        "/api/v1/channels/quick-create",
        json={
            "name": "ONVIF通道",
            "source_type": "onvif",
            "stream_url": stream["stream_url"],
            "onvif_params": {
                "host": "camera.local",
                "port": 80,
                "username": "admin",
                "profile_token": stream["profile_token"],
                "device_service_url": stream["device_service_url"],
                "transport": stream["transport"],
            },
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["channel"]["source_type"] == "onvif"
    assert payload["channel"]["onvif_params"]["host"] == "camera.local"


def test_worker_submits_frame_and_evidence_only_for_detections():
    import base64

    from app.worker import submit_frame

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeHttpClient:
        def __init__(self, detections):
            self.calls = []
            self.detections = detections

        def post(self, url, json):
            self.calls.append((url, json))
            if url == "http://inference.local/infer":
                return FakeResponse({"detections": self.detections})
            return FakeResponse([])

    class FakeCv2:
        @staticmethod
        def imencode(extension, frame):
            assert extension == ".jpg"
            assert frame == "frame"
            return True, b"jpeg-data"

    http_client = FakeHttpClient([{"type": "no_helmet", "confidence": "0.91"}])
    result = submit_frame(
        channel_id=7,
        frame="frame",
        rules=[{"id": 3, "model_id": 2, "alarm_type": "no_helmet", "confidence": 0.6}],
        cv2_module=FakeCv2,
        http_client=http_client,
        api_url="http://api.local/",
        inference_url="http://inference.local/infer",
    )

    encoded_frame = base64.b64encode(b"jpeg-data").decode("ascii")
    assert result == []
    assert http_client.calls[0][1] == {
        "channel_id": 7,
        "image_base64": encoded_frame,
        "rules": [{"id": 3, "model_id": 2, "alarm_type": "no_helmet", "confidence": 0.6}],
    }
    assert http_client.calls[1] == (
        "http://api.local/api/v1/detections",
        {
            "channel_id": 7,
            "detections": [{"type": "no_helmet", "confidence": 0.91}],
            "image_base64": encoded_frame,
        },
    )

    empty_client = FakeHttpClient([])
    submit_frame(
        channel_id=7,
        frame="frame",
        rules=[],
        cv2_module=FakeCv2,
        http_client=empty_client,
        api_url="http://api.local",
        inference_url="http://inference.local/infer",
    )
    assert empty_client.calls[1][1] == {"channel_id": 7, "detections": []}


def test_worker_rejects_invalid_inference_detections():
    import pytest

    from app.worker import normalize_detections

    with pytest.raises(ValueError, match="between 0 and 1"):
        normalize_detections({"detections": [{"type": "no_helmet", "confidence": 1.2}]})
