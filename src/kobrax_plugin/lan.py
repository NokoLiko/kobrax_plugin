"""Anycubic Kobra X LAN protocol: HTTP handshake, MQTT session and file upload.

Protocol facts come from community research (stribor/anycubic_kobrax,
jaimevictor/kobra-x-local-slicer-haas, eduardobittencourt/kobra-lan-cli) and from
read-only captures of a Kobra X on firmware 2.0.2.2. There is no official API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import secrets
import ssl
import string
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("kobrax_plugin")

HTTP_PORT = 18910
MQTT_PORT = 9883
TOPIC_BASE = "anycubic/anycubicCloud/v1"
HTTP_TIMEOUT = 10


class KobraError(RuntimeError):
    """A protocol step failed; the message is safe to show (no secrets)."""


@dataclass(frozen=True)
class Credentials:
    host: str
    device_id: str
    model_id: str
    username: str
    password: str
    cert_pem: str
    key_pem: str
    device_name: str
    model_name: str


# --------------------------------------------------------------------------- HTTP


def fetch_info(host: str, timeout: float = 5) -> dict[str, Any]:
    """GET /info. Unauthenticated; returns the token used for the handshake."""
    resp = requests.get(f"http://{host}:{HTTP_PORT}/info", timeout=timeout)
    resp.raise_for_status()
    info = resp.json()
    if not isinstance(info, dict) or len(str(info.get("token", ""))) < 32:
        raise KobraError("La impresora devolvió un /info inválido (¿modo LAN activado?)")
    return info


def sign_params(token: str, ts: str | None = None, nonce: str | None = None) -> dict[str, str]:
    """Query parameters for the signed POST to ctrlInfoUrl."""
    ts = ts or str(int(time.time() * 1000))
    nonce = nonce or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
    inner = hashlib.md5(token[:16].encode()).hexdigest()
    sign = hashlib.md5((inner + ts + nonce).encode()).hexdigest()
    return {"ts": ts, "nonce": nonce, "sign": sign, "did": uuid.uuid4().hex.upper()}


def decrypt_bundle(token: str, encrypted_b64: str, iv: str) -> dict[str, Any]:
    """AES-128-CBC: key = token[16:32] from /info, IV = data.token from /ctrl."""
    key = token[16:32].encode()
    iv_bytes = iv.encode()
    if len(key) != 16 or len(iv_bytes) != 16:
        raise KobraError("Clave o IV con longitud inesperada")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv_bytes)).decryptor()
    padded = decryptor.update(base64.b64decode(encrypted_b64)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    bundle = json.loads(unpadder.update(padded) + unpadder.finalize())
    if not isinstance(bundle, dict):
        raise KobraError("Bundle de credenciales inválido")
    return bundle


def handshake(host: str) -> tuple[dict[str, Any], Credentials]:
    """/info -> signed POST /ctrl -> decrypt MQTT credentials."""
    info = fetch_info(host)
    token = str(info["token"])
    ctrl_url = str(info.get("ctrlInfoUrl") or f"http://{host}:{HTTP_PORT}/ctrl")
    resp = requests.post(ctrl_url, params=sign_params(token), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    ctrl = resp.json()
    data = ctrl.get("data") if isinstance(ctrl, dict) else None
    if ctrl.get("code") != 200 or not isinstance(data, dict):
        raise KobraError(f"La impresora rechazó /ctrl (code={ctrl.get('code')})")
    bundle = decrypt_bundle(token, str(data["info"]), str(data["token"]))
    for field in ("deviceId", "username", "password", "devicecrt", "devicepk"):
        if not bundle.get(field):
            raise KobraError(f"Falta '{field}' en las credenciales")
    creds = Credentials(
        host=host,
        device_id=str(bundle["deviceId"]),
        model_id=str(bundle.get("modelId") or bundle.get("modeId") or info.get("modelId") or "20030"),
        username=str(bundle["username"]),
        password=str(bundle["password"]),
        cert_pem=str(bundle["devicecrt"]),
        key_pem=str(bundle["devicepk"]),
        device_name=str(info.get("deviceName") or bundle.get("modelName") or "Anycubic Kobra X"),
        model_name=str(bundle.get("modelName") or info.get("modelName") or "Anycubic Kobra X"),
    )
    return info, creds


def validate_upload_url(url: str, host: str) -> str:
    """Only accept http://<host>:18910/gcode_upload?s=<token> from the printer."""
    p = urlparse(url)
    token = parse_qs(p.query).get("s", [])
    if (
        p.scheme != "http"
        or p.hostname != host
        or p.port != HTTP_PORT
        or p.path != "/gcode_upload"
        or p.username
        or p.password
        or p.fragment
        or len(token) != 1
        or not token[0]
    ):
        raise KobraError("La URL de subida que dio la impresora no es válida")
    return url


def upload_file(
    url: str,
    host: str,
    path: str,
    remote_name: str,
    client_version: str = "1.0.0",
) -> dict[str, Any]:
    """Multipart POST with fields `filename` and `gcode` (byte-preserving)."""
    validate_upload_url(url, host)
    size = os.path.getsize(path)
    headers = {
        "X-File-Length": str(size),
        "X-BBL-Client-Name": "OrcaSlicer-KobraX-Plugin",
        "X-BBL-Client-Type": "slicer",
        "X-BBL-Client-Version": client_version,
        "X-BBL-Device-ID": uuid.uuid4().hex,
        "X-BBL-Language": "en-US",
        "X-BBL-OS-Type": platform.system().lower(),
        "X-BBL-OS-Version": platform.release()[:80],
    }
    with open(path, "rb") as fh:
        resp = requests.post(
            url,
            data={"filename": remote_name},
            files={"gcode": (remote_name, fh, "application/octet-stream")},
            headers=headers,
            timeout=(10, 600),
            allow_redirects=False,
        )
    if resp.status_code != 200:
        raise KobraError(f"Subida rechazada (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError as err:
        raise KobraError("La respuesta de la subida no es JSON") from err
    if body.get("code") != 200:
        raise KobraError(f"La impresora rechazó el archivo (code={body.get('code')})")
    return body


# --------------------------------------------------------------------------- MQTT


def _remove_dir(path: str, names: tuple[str, ...]) -> None:
    for name in names:
        try:
            os.remove(os.path.join(path, name))
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not remove %s", os.path.join(path, name))
    try:
        os.rmdir(path)
    except OSError:
        log.warning("could not remove %s", path)


def purge_tls_dirs(work_dir: str) -> None:
    """Remove TLS scratch folders a crashed or blocked run may have left behind."""
    try:
        entries = os.listdir(work_dir)
    except OSError:
        return
    for name in entries:
        if name.startswith("tls-"):
            _remove_dir(os.path.join(work_dir, name), ("client.pem", "client.key"))



ReportFn = Callable[[str, dict[str, Any]], None]


class KobraMqtt:
    """One TLS MQTT session to the printer's local broker."""

    def __init__(
        self,
        creds: Credentials,
        on_report: ReportFn,
        on_link: Callable[[bool], None],
        work_dir: str | None = None,
    ):
        self.creds = creds
        self._on_report = on_report
        self._on_link = on_link
        # Inside OrcaSlicer this is the plugin's storage folder: the audit hook allows it
        # without prompting, unlike the system temp directory.
        self._work_dir = work_dir
        self._connected = threading.Event()
        self._failed = threading.Event()
        self._client: mqtt.Client | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def topic(self, source: str, kind: str) -> str:
        return f"{TOPIC_BASE}/{source}/printer/{self.creds.model_id}/{self.creds.device_id}/{kind}"

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the printer uses a self-signed certificate
        # load_cert_chain needs files; keep them only for the duration of the load.
        # Path-based calls only (no fd-based open/scandir): OrcaSlicer's audit hook can see a
        # path inside the plugin's storage and allow it, but asks about descriptor-based calls.
        if self._work_dir:
            purge_tls_dirs(self._work_dir)
            tmp = os.path.join(self._work_dir, f"tls-{uuid.uuid4().hex}")
            os.mkdir(tmp, 0o700)
        else:
            tmp = tempfile.mkdtemp(prefix="kobrax-")
        crt = os.path.join(tmp, "client.pem")
        key = os.path.join(tmp, "client.key")
        try:
            for p, content in ((crt, self.creds.cert_pem), (key, self.creds.key_pem)):
                with open(p, "x") as fh:
                    fh.write(content)
                os.chmod(p, 0o600)
            ctx.load_cert_chain(crt, key)
        finally:
            _remove_dir(tmp, ("client.pem", "client.key"))
        return ctx

    def connect(self, timeout: float = 10) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"orca-kobrax-{uuid.uuid4()}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(self.creds.username, self.creds.password)
        client.tls_set_context(self._ssl_context())
        client.tls_insecure_set(True)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        self._client = client
        client.connect(self.creds.host, MQTT_PORT, keepalive=60)
        client.loop_start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._connected.wait(0.1):
                return
            if self._failed.is_set():
                break
        self.close()
        raise KobraError("El broker MQTT de la impresora no aceptó la conexión")

    def close(self) -> None:
        client, self._client = self._client, None
        self._connected.clear()
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                log.debug("error closing MQTT", exc_info=True)

    def publish(self, source: str, kind: str, action: str, data: Any = None) -> str:
        if self._client is None or not self.connected:
            raise KobraError("No hay conexión MQTT con la impresora")
        msgid = str(uuid.uuid4())
        payload = {
            "type": kind,
            "action": action,
            "timestamp": int(time.time() * 1000),
            "msgid": msgid,
            "data": data,
        }
        info = self._client.publish(
            self.topic(source, kind), json.dumps(payload, separators=(",", ":")), qos=0
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise KobraError(f"No se pudo publicar en MQTT (rc={info.rc})")
        return msgid

    def query(self, kind: str, action: str = "query") -> None:
        self.publish("web", kind, action)

    # paho callbacks (paho network thread)
    def _handle_connect(self, client, _userdata, _flags, reason_code, _props) -> None:
        if reason_code.is_failure:
            log.error("MQTT connect rejected: %s", reason_code)
            self._failed.set()
            return
        client.subscribe(
            f"{TOPIC_BASE}/printer/public/{self.creds.model_id}/{self.creds.device_id}/#"
        )
        self._connected.set()
        self._on_link(True)

    def _handle_disconnect(self, _client, _userdata, _flags, reason_code, _props) -> None:
        was = self._connected.is_set()
        self._connected.clear()
        if was:
            log.warning("MQTT disconnected: %s", reason_code)
            self._on_link(False)

    def _handle_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if isinstance(payload, dict):
            try:
                self._on_report(message.topic, payload)
            except Exception:  # noqa: BLE001
                log.exception("error handling report on %s", message.topic)
