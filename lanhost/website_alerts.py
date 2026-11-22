"""工作机的 IC交易网提醒。每次需要人工处理时保留一张本地截图。"""
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from inquiry import site_capture
from inquiry.runtime import require_d_path
from . import alert_webhook

POLL_SECONDS = 3


@dataclass
class WebsiteAlert:
    id: str
    kind: str
    target: str | None
    image_ready: bool = False


class WebsiteAlerts:
    def __init__(self, image_path: Path):
        self._path = require_d_path(image_path)
        self._lock = threading.Lock()
        self._alert: WebsiteAlert | None = None

    def _remove_image(self):
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass  # 已撤销图片标识，即使文件暂不可删除也不会再下发。

    def clear(self):
        with self._lock:
            self._alert = None
            self._remove_image()

    def status(self):
        with self._lock:
            alert = self._alert
            if alert is None:
                return None
            return {"id": alert.id, "kind": alert.kind, "image_ready": alert.image_ready}

    def image(self, event_id: str) -> bytes | None:
        with self._lock:
            alert = self._alert
            if alert and alert.id == event_id and alert.image_ready:
                try:
                    with self._path.open("rb") as file:
                        data = file.read(site_capture.MAX_SCREENSHOT_BYTES + 1)
                except OSError:
                    return None
                if data.startswith(b"\xff\xd8") and len(data) <= site_capture.MAX_SCREENSHOT_BYTES:
                    return data
        return None

    def poll(self, stopped=lambda: False):
        status, target = site_capture.inspect_browser()
        kind = status["code"]
        if kind not in ("login", "verification"):
            if self.status() is not None:
                self.clear()
            return
        with self._lock:
            if stopped():
                return
            alert = self._alert
            changed = alert is None or alert.kind != kind or bool(target and alert.target and alert.target != target)
            if changed:
                self._remove_image()
                alert = self._alert = WebsiteAlert(uuid.uuid4().hex, kind, target)
            elif alert.target != target:
                self._remove_image()
                alert.target = target
                alert.image_ready = False

        if changed:
            threading.Thread(target=alert_webhook.push, args=(kind,), daemon=True, name="website-alert-webhook").start()
        if target and not alert.image_ready:
            data = site_capture.capture_site(target, kind)
            if data and len(data) <= site_capture.MAX_SCREENSHOT_BYTES and data.startswith(b"\xff\xd8"):
                with self._lock:
                    if self._alert is alert and not stopped():
                        self._path.write_bytes(data)
                        alert.image_ready = True

    def start(self, stop: threading.Event):
        def monitor():
            try:
                self.clear()
                while not stop.is_set():
                    try:
                        self.poll(stop.is_set)
                    except OSError:
                        # 无法读取页面或保存图片时保留文字提醒，等待下一轮。
                        pass
                    stop.wait(POLL_SECONDS)
            finally:
                self.clear()

        thread = threading.Thread(target=monitor, daemon=True, name="website-alert-monitor")
        thread.start()
        return thread
