from __future__ import annotations

import base64
import logging
import math
import os
import time
from typing import Any, Dict, List

LOGGER = logging.getLogger("aiwarning.worker")
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_FRAME_INTERVAL_SECONDS = 1.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


def normalize_detections(response_payload: Any) -> List[Dict]:
    if not isinstance(response_payload, dict) or not isinstance(response_payload.get("detections"), list):
        raise ValueError("inference response must contain a detections array")

    detections = response_payload["detections"]
    for index, detection in enumerate(detections):
        if not isinstance(detection, dict) or not isinstance(detection.get("type"), str):
            raise ValueError(f"detection {index} must contain a string type")
        try:
            confidence = float(detection["confidence"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"detection {index} must contain a numeric confidence") from error
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError(f"detection {index} confidence must be between 0 and 1")
        detection["confidence"] = confidence
    return detections


def submit_frame(
    *,
    channel_id: int,
    frame: Any,
    rules: List[Dict],
    cv2_module: Any,
    http_client: httpx.Client,
    api_url: str,
    inference_url: str,
) -> List[Dict]:
    encoded, buffer = cv2_module.imencode(".jpg", frame)
    if not encoded:
        raise ValueError("could not encode video frame as JPEG")
    image_base64 = base64.b64encode(buffer).decode("ascii")

    inference_response = http_client.post(
        inference_url,
        json={
            "channel_id": channel_id,
            "image_base64": image_base64,
            "rules": [
                {
                    "id": rule["id"],
                    "model_id": rule["model_id"],
                    "alarm_type": rule["alarm_type"],
                    "confidence": rule["confidence"],
                }
                for rule in rules
            ],
        },
    )
    inference_response.raise_for_status()
    detections = normalize_detections(inference_response.json())

    detection_payload: Dict[str, Any] = {
        "channel_id": channel_id,
        "detections": detections,
    }
    if detections:
        detection_payload["image_base64"] = image_base64
    result = http_client.post(f"{api_url.rstrip('/')}/api/v1/detections", json=detection_payload)
    result.raise_for_status()
    return result.json()


def run_worker(
    *,
    api_url: str,
    inference_url: str,
    frame_interval_seconds: float,
    request_timeout_seconds: float,
) -> None:
    import cv2
    import httpx

    captures: Dict[int, Any] = {}
    with httpx.Client(timeout=request_timeout_seconds) as http_client:
        try:
            while True:
                try:
                    response = http_client.get(f"{api_url.rstrip('/')}/api/v1/channels")
                    response.raise_for_status()
                    channels = response.json()
                except (httpx.HTTPError, ValueError):
                    LOGGER.exception("Could not retrieve channels from the AI Warning API")
                    time.sleep(frame_interval_seconds)
                    continue

                active_channels = {
                    channel["id"]: channel
                    for channel in channels
                    if channel.get("enabled") and channel.get("stream_url")
                }
                for channel_id in set(captures) - set(active_channels):
                    captures.pop(channel_id).release()

                for channel_id, channel in active_channels.items():
                    capture = captures.get(channel_id)
                    if capture is None:
                        capture = cv2.VideoCapture(channel["stream_url"])
                        if not capture.isOpened():
                            capture.release()
                            LOGGER.warning("Could not open video stream for channel %s", channel_id)
                            continue
                        captures[channel_id] = capture

                    success, frame = capture.read()
                    if not success:
                        capture.release()
                        captures.pop(channel_id, None)
                        LOGGER.warning("Lost video stream for channel %s; it will be reconnected", channel_id)
                        continue

                    try:
                        rules_response = http_client.get(
                            f"{api_url.rstrip('/')}/api/v1/rules",
                            params={"channel_id": channel_id},
                        )
                        rules_response.raise_for_status()
                        rules = [rule for rule in rules_response.json() if rule.get("enabled")]
                        submit_frame(
                            channel_id=channel_id,
                            frame=frame,
                            rules=rules,
                            cv2_module=cv2,
                            http_client=http_client,
                            api_url=api_url,
                            inference_url=inference_url,
                        )
                    except (httpx.HTTPError, ValueError):
                        LOGGER.exception("Frame processing failed for channel %s", channel_id)

                time.sleep(frame_interval_seconds)
        finally:
            for capture in captures.values():
                capture.release()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("AIWARNING_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    inference_url = os.getenv("AIWARNING_INFERENCE_URL")
    if not inference_url:
        raise SystemExit("AIWARNING_INFERENCE_URL must point to an inference service")

    try:
        frame_interval_seconds = float(
            os.getenv("AIWARNING_FRAME_INTERVAL_SECONDS", str(DEFAULT_FRAME_INTERVAL_SECONDS))
        )
        request_timeout_seconds = float(
            os.getenv("AIWARNING_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS))
        )
    except ValueError as error:
        raise SystemExit("Worker interval and timeout settings must be numbers") from error
    if frame_interval_seconds <= 0 or request_timeout_seconds <= 0:
        raise SystemExit("Worker interval and timeout settings must be greater than zero")

    api_url = os.getenv("AIWARNING_API_URL", DEFAULT_API_URL).rstrip("/")
    LOGGER.info("Starting video worker for API %s", api_url)
    run_worker(
        api_url=api_url,
        inference_url=inference_url,
        frame_interval_seconds=frame_interval_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


if __name__ == "__main__":
    main()
