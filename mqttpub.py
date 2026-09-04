"""ROI 체류 이벤트를 MQTT로 내보낸다.

    DetectionWorker -> Publisher.publish(event) -> safe-vibe/alert

브로커는 이 프로젝트가 띄우지 않는다. 이미 있는 브로커 주소를 MQTT_HOST 로 주면
되고, 없으면 로컬에 mosquitto 를 띄워 localhost 를 주면 된다.

연동은 처음부터 끝까지 "있으면 좋고 없어도 그만"으로 다룬다. MQTT_HOST 가 비어
있거나 paho-mqtt 가 없거나 브로커가 죽어 있어도 감시와 송출은 그대로 돌아야 한다.
model/ 이 비면 검출만 끄고 스트리밍은 계속하는 것과 같은 태도다.
"""
import json
import threading
import time

try:
    import paho.mqtt.client as paho
except ImportError:                 # 설치가 없으면 조용히 비활성으로 간다
    paho = None


class Publisher:
    """이벤트 한 건을 JSON 한 줄로 발행한다.

    연결은 백그라운드 스레드가 맡는다(loop_start). 그래서 publish()는 큐에 넣고
    바로 돌아오고, 브로커가 느리거나 끊겨도 추론 루프가 멈추지 않는다.
    """

    def __init__(self, host, port, topic, qos=1, keepalive=60, client_id=""):
        self.host, self.port = host, port
        self.topic, self.qos, self.keepalive = topic, qos, keepalive
        self.client_id = client_id
        self.client = None
        self.connected = False
        self.sent = 0
        self.dropped = 0
        self.last_error = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 상태

    @property
    def enabled(self):
        return bool(self.host) and paho is not None

    def status(self):
        """/api/status 에 그대로 실린다. 왜 안 나가는지를 화면에서 바로 알 수 있게."""
        if not self.host:
            return {"enabled": False, "reason": "MQTT_HOST 미설정"}
        if paho is None:
            return {"enabled": False, "reason": "paho-mqtt 미설치"}
        return {
            "enabled": True,
            "broker": f"{self.host}:{self.port}",
            "topic": self.topic,
            "qos": self.qos,
            "connected": self.connected,
            "sent": self.sent,
            "dropped": self.dropped,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------ 연결

    def start(self):
        """브로커가 떠 있든 아니든 즉시 돌아온다.

        connect_async + loop_start 조합이라 첫 연결도 백그라운드에서 시도하고,
        실패하면 paho 가 알아서 간격을 늘려가며 재시도한다. 브로커를 나중에
        띄워도 앱을 다시 시작할 필요가 없다.
        """
        if not self.enabled:
            print("[mqtt] 비활성 — %s" % self.status().get("reason"), flush=True)
            return self
        try:
            self.client = self._new_client()
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.reconnect_delay_set(min_delay=1, max_delay=30)
            self.client.connect_async(self.host, self.port, self.keepalive)
            self.client.loop_start()
            print("[mqtt] %s:%d -> %s (연결 시도 중)"
                  % (self.host, self.port, self.topic), flush=True)
        except Exception as e:                  # 설정 오류로 앱이 죽어선 안 된다
            self.client = None
            self.last_error = str(e)
            print("[mqtt] 시작 실패: %s" % e, flush=True)
        return self

    def _new_client(self):
        """paho 1.x / 2.x 를 모두 받는다.

        2.x 는 콜백 API 버전을 반드시 지정해야 하고, 1.x 는 그 인자를 모른다.
        라즈베리파이의 apt 패키지(python3-paho-mqtt)는 아직 1.x 인 경우가 있다.
        """
        if hasattr(paho, "CallbackAPIVersion"):
            return paho.Client(paho.CallbackAPIVersion.VERSION2,
                               client_id=self.client_id)
        return paho.Client(client_id=self.client_id)

    # 콜백 시그니처가 1.x 와 2.x 에서 다르다. 뒤쪽 인자는 쓰지 않으므로 *args 로 받는다.
    def _on_connect(self, client, userdata, flags, *args):
        rc = args[0] if args else 0
        ok = (getattr(rc, "is_failure", None) is False) or rc == 0
        self.connected = bool(ok)
        if ok:
            print("[mqtt] 연결됨 — %s:%d" % (self.host, self.port), flush=True)
        else:
            self.last_error = "connect rc=%s" % rc
            print("[mqtt] 연결 거부: %s" % rc, flush=True)

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        print("[mqtt] 연결 끊김 — 재연결 시도", flush=True)

    # ------------------------------------------------------------ 발행

    def publish(self, event):
        """이벤트 하나를 발행한다. 실패해도 예외를 밖으로 내보내지 않는다."""
        if not self.enabled or self.client is None:
            return False

        payload = dict(event)
        payload["event"] = "roi_dwell"
        # 화면용 ts 는 "%H:%M:%S" 로컬 문자열이라 날짜도 시간대도 없다. 받는 쪽이
        # 정렬하고 보관하려면 절대 시각이 필요하므로 epoch 초를 같이 싣는다.
        payload.setdefault("ts_epoch", round(time.time(), 3))

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            # retain=False. 경보를 retain 하면 나중에 붙은 구독자가 한참 지난
            # 이벤트를 현재 상황인 것처럼 받게 된다.
            info = self.client.publish(self.topic, body, qos=self.qos, retain=False)
        except Exception as e:
            self._count_drop(str(e))
            return False

        if info.rc != 0:
            # 브로커가 끊긴 동안의 발행은 버린다. 지난 경보를 나중에 몰아서
            # 보내봐야 받는 쪽에서는 오탐과 구분할 수 없다.
            self._count_drop("publish rc=%s" % info.rc)
            return False
        with self._lock:
            self.sent += 1
        return True

    def _count_drop(self, reason):
        with self._lock:
            self.dropped += 1
            self.last_error = reason
        # 브로커가 오래 죽어 있으면 이벤트마다 찍혀 로그를 덮는다. 처음 몇 번만.
        if self.dropped <= 3:
            print("[mqtt] 발행 실패(%s) — 이벤트를 버립니다" % reason, flush=True)

    def stop(self):
        if self.client is None:
            return
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.connected = False
