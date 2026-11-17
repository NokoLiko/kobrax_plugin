from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from . import lan
from .job import JobError, build_box_mapping, build_start_payload, prepare_gcode
from .errors import error_from_report
from .page import WEBUI_PORT, LocalWebUI, make_page_capability
from .status import FAMILIES, LIGHT_CAMERA, SLOT_LOADED, SPEED_FROM_ORCA, SPEED_TO_ORCA, PrinterState, rgb_hex

try:
    import orca
except ImportError:  # running outside OrcaSlicer (tests, editor)
    orca = None

if orca is not None:
    pa = orca.printer_agent
    _AgentBase: type = pa.PrinterAgentBase
else:
    pa = None
    _AgentBase = object

PLUGIN_VERSION = "0.6.3"
AGENT_ID = "anykubic"

log = logging.getLogger("kobrax_plugin")
if not log.handlers:  # goes to OrcaSlicer's python_*.log through stderr
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[kobrax] %(levelname)s %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

# OrcaSlicer return codes (src/slic3r/Utils/bambu_networking.hpp)
OK = 0
ERR = -1
ERR_FILE_NOT_EXIST = -14
ERR_CANCELED = -18
ERR_LP_UPLOAD_FAILED = -4020
ERR_LP_PUBLISH_FAILED = -4030
ERR_SG_UPLOAD_FAILED = -5010
ERR_CONNECTION_FAILED = -6010
NOT_SUPPORTED = -7010

# PrintingStage
STAGE_CREATE, STAGE_UPLOAD, STAGE_SENDING, STAGE_FINISHED = 0, 1, 3, 6
# ConnectStatus
CONNECT_OK, CONNECT_FAILED, CONNECT_LOST = 0, 1, 2

POLL_INFO_S = 3
# axis/move: axis 1/2/3 = X/Y/Z, 4 = XY, 5 = XYZ; move_type 0 = negative, 1 = positive, 2 = home
AXIS_IDS = {"X": 1, "Y": 2, "Z": 3}
# OrcaSlicer's "Bed" jog buttons assume a printer whose bed moves in Z ("bed up" = Z-). On the
# Kobra X Z moves the print head, so the sign is flipped to make the up arrow raise the head
# (checked on hardware 2026-11-15: unflipped, "up" lowered the head).
AXIS_SIGN = {"X": 1, "Y": 1, "Z": -1}
AXIS_HOME_XYZ = 5
# OrcaSlicer fan index (M106 P / set_fan fan_index) -> Kobra X fan
FAN_BY_INDEX = {0: "part", 1: "part", 2: "aux", 3: "box"}
MOVE_NEGATIVE, MOVE_POSITIVE, MOVE_HOME = 0, 1, 2
POLL_BOX_S = 15

UpdateFn = Callable[[int, int, str], None] | None
CancelFn = Callable[[], bool] | None
WaitFn = Callable[[int, str], bool] | None
ReportPredicate = Callable[[str, dict[str, Any]], bool]


class _Canceled(Exception):
    pass


def orca_model_dir() -> str:
    """OrcaSlicer's per-user temp folder for sliced projects (the .3mf a print job sends).

    Mirrors libslic3r: boost::filesystem::temp_directory_path() (TMPDIR, TMP, TEMP, TEMPDIR,
    else /tmp) + "/orcaslicer_<uid>" (per_user_temp_dir, no uid on Windows) + "/orcaslicer_model".
    Read without probing the directory, which the audit hook would flag.
    """
    env = ("TMPDIR", "TMP", "TEMP", "TEMPDIR") if os.name != "nt" else ("TMP", "TEMP", "USERPROFILE")
    base = next((os.environ[v] for v in env if os.environ.get(v)), "/tmp" if os.name != "nt" else "C:\\Windows\\Temp")
    base = base.rstrip("/\\") or "/"
    if hasattr(os, "getuid"):
        base = os.path.join(base, f"orcaslicer_{os.getuid()}")
    return os.path.join(base, "orcaslicer_model")


AUDIT_RETRY_S = 60


def _is_audit_denial(err: BaseException) -> bool:
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        if isinstance(err, PermissionError) or "audited operation" in str(err):
            return True
        err = err.__cause__ or err.__context__
    return False


def _preflight_network_permission(host: str) -> None:
    """Trigger OrcaSlicer's permission prompt for `host` on the UI thread.

    A prompt raised on a background thread is shown without waiting and the call is refused,
    so the connection loop would stack one prompt per retry. On the UI thread the prompt is
    modal and its answer is remembered before the loop starts. AI_NUMERICHOST: no DNS lookup,
    so this never blocks the UI (a host name just fails here after the audit check).
    """
    try:
        socket.getaddrinfo(host, lan.HTTP_PORT, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except Exception:  # noqa: BLE001 - denied, or not a numeric address
        pass


def preset_printer_host() -> str:
    """Host of the selected printer preset when it uses this agent ("" otherwise)."""
    if orca is None:
        return ""
    try:
        preset = orca.host.preset_bundle().printers.get_edited_preset()
        if str(preset.config_value("printer_agent") or "") != AGENT_ID:
            return ""
        return _clean_host(str(preset.config_value("print_host") or ""))
    except Exception:  # noqa: BLE001 - GUI not ready, API missing
        return ""


def _clean_host(host: str) -> str:
    host = (host or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0].split(":", 1)[0]


_STATE_LABELS = {
    "IDLE": "En espera", "PREPARE": "Preparando", "RUNNING": "Imprimiendo",
    "PAUSE": "Pausada", "FINISH": "Terminada", "FAILED": "Detenida / error",
}
_SPEED_LABELS = {1: "Silencioso", 2: "Estándar", 3: "Sport"}
_JOB_FIELDS = {
    "layer_height": ("Altura de capa", "mm"), "fill_density": ("Relleno", "%"),
    "perimeters": ("Perímetros", ""), "temperature": ("Temp. boquilla", "°C"),
    "bed_temperature": ("Temp. cama", "°C"), "perimeter_speed": ("Vel. perímetro", "mm/s"),
    "travel_speed": ("Vel. viaje", "mm/s"), "filament_retract_length": ("Retracción", "mm"),
    "brim_type": ("Brim", ""), "support_material_auto": ("Soportes auto", ""),
}


class KobraX(_AgentBase):
    _instance: "KobraX | None" = None  # the live agent, for the plugin page
    _webui: LocalWebUI | None = None

    def __init__(self):
        super().__init__()
        KobraX._instance = self
        self._callbacks: dict[str, Callable[..., Any] | None] = {}
        self.selected_dev_id = ""
        self._lock = threading.RLock()
        self._gen = 0
        self._dev_id = ""  # OrcaSlicer's id for the connected printer
        self._host = ""
        self._access_code = ""
        self._session: lan.KobraMqtt | None = None
        self._creds: lan.Credentials | None = None
        self._state = PrinterState()
        self._waiters: list[ReportPredicate] = []
        self._storage: str | None = None
        self._camera_started = 0.0
        self._filament_ids: dict[str, str] = {}
        self._last_error: tuple[int, str] | None = None

    def _storage_dir(self) -> str | None:
        """The plugin's own folder, the one place OrcaSlicer lets it write without asking.

        Must be called from a plugin callback (connect_printer, start_*), not from our threads.
        """
        if self._storage is None and orca is not None:
            try:
                self._storage = str(orca.host.plugin.storage())
            except Exception as err:  # noqa: BLE001
                log.warning("plugin storage unavailable, using the temp dir: %s", err)
        return self._storage

    def _resolve_filament_ids(self) -> dict[str, str]:
        """Generic filament preset ids from the running OrcaSlicer (UI thread only).

        Looked up instead of hardcoded, like OrcaSlicer's Moonraker agent, because profile
        ids can change between OrcaSlicer versions.
        """
        if self._filament_ids or orca is None:
            return self._filament_ids
        ids: dict[str, str] = {}
        try:
            filaments = orca.host.preset_bundle().filaments
            for family in FAMILIES:
                preset = filaments.find_preset(f"Generic {family} @System")
                if preset is None or not preset.is_system:
                    continue
                fid = preset.config_value("filament_id")
                if isinstance(fid, (list, tuple)):
                    fid = fid[0] if fid else ""
                if fid:
                    ids[family] = str(fid)
        except Exception as err:  # noqa: BLE001
            log.warning("could not read filament presets: %s", err)
        self._filament_ids = ids
        log.info("filament ids resolved for %d materials", len(ids))
        return ids

    @staticmethod
    def _preset_nozzle_diameter() -> str:
        """Nozzle diameter of the selected printer preset (UI thread only)."""
        if orca is None:
            return ""
        try:
            value = orca.host.preset_bundle().printers.get_edited_preset().config_value("nozzle_diameter")
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            return f"{float(value):g}" if value not in (None, "") else ""
        except Exception as err:  # noqa: BLE001
            log.warning("could not read nozzle diameter: %s", err)
            return ""

    def _notify_error(self, code: int, message: str) -> None:
        log.error("printer error %s: %s", code, message)
        if orca is None:
            return
        try:
            ui = orca.host.ui
            ui.push_notification(ui.NotificationLevel.ErrorNotificationLevel, f"Kobra X: {message} ({code})")
        except Exception as err:  # noqa: BLE001
            log.warning("could not show notification: %s", err)

    # ---------------------------------------------------------------- plugin page
    def _start_webui(self) -> None:
        if KobraX._webui is None:
            KobraX._webui = LocalWebUI(self.page_snapshot, self.page_action)
        KobraX._webui.start()

    def on_unload(self) -> None:
        if KobraX._webui is not None:
            KobraX._webui.stop()
            KobraX._webui = None
        if KobraX._instance is self:
            KobraX._instance = None

    def page_snapshot(self) -> dict[str, Any]:
        st = self._state
        session = self._session
        project = st.project
        temp = st.temp
        fan = st.fan
        filename = str(project.get("filename") or "")
        speed = st.info.get("print_speed_mode")
        slice_data = st.slice if project else {}
        params = slice_data.get("slice_param") or {}
        job = {}
        for key, (label, unit) in _JOB_FIELDS.items():
            if params.get(key) not in (None, "", -1, -1.0):
                job[label] = f"{params[key]:g} {unit}".strip() if isinstance(params[key], (int, float)) else str(params[key])
        for info in params.get("paint_infos") or []:
            if isinstance(info, dict) and info.get("filament_used") is not None:
                job[f"Filamento {int(info.get('paint_index', 0)) + 1}"] = f"{info.get('material_type', '')} · {info['filament_used']:.1f} m"
        state = st.gcode_state()
        return {
            "connected": bool(session is not None and session.connected),
            "host": self._host,
            "name": self._creds.device_name if self._creds else "Anycubic Kobra X",
            "version": st.version,
            "state": state,
            "state_label": _STATE_LABELS.get(state, state) + (f" ({project.get('state')})" if project.get("state") else ""),
            "filename": filename.rsplit("/", 1)[-1],
            "progress": project.get("progress"),
            "layer": project.get("curr_layer"),
            "total_layers": project.get("total_layers"),
            "elapsed_min": project.get("print_time"),
            "remain_min": project.get("remain_time"),
            "temps": {
                "nozzle": temp.get("curr_nozzle_temp"), "nozzle_target": temp.get("target_nozzle_temp"),
                "bed": temp.get("curr_hotbed_temp"), "bed_target": temp.get("target_hotbed_temp"),
            },
            "fans": {
                "part": fan.get("fan_speed_pct"),
                "aux": fan.get("aux_fan_speed_pct"),
                "box": fan.get("box_fan_level", fan.get("box_fan_speed_pct")),
            },
            "speed": SPEED_TO_ORCA.get(int(speed)) if isinstance(speed, (int, float)) else None,
            "light": st.light_on,
            "slots": [
                {"index": i, "type": s.get("type"), "color": rgb_hex(s.get("color"))[:6], "loaded": s.get("status") == SLOT_LOADED}
                for i, s in enumerate(st.slots())
            ],
            "job": job,
            "thumb_key": slice_data.get("file_key") if slice_data.get("thumbnail") else None,
            "files": st.files,
            "last_error": list(self._last_error) if self._last_error else None,
            "plugin_version": PLUGIN_VERSION,
            "webui_url": KobraX._webui.url if KobraX._webui else "",
        }

    def page_action(self, msg: dict[str, Any]) -> dict[str, Any]:
        action = msg.get("action")
        try:
            if action == "thumbnail":
                return {"type": "result", "ok": True, "thumbnail": self._state.slice.get("thumbnail") or ""}
            session = self._require_session()
            if action == "light":
                rc = self._set_light(bool(msg.get("on")))
            elif action == "speed":
                rc = self._set_speed_mode(int(msg.get("level", 0)))
            elif action == "fan":
                rc = self._set_fan(float(msg.get("pct", 0)), str(msg.get("fan", "")))
            elif action == "job":
                session.publish("web", "print", "getSliceParam", {"taskid": self._state.task_id})
                rc = OK
            elif action == "files":
                kind = "listUdisk" if msg.get("root") == "udisk" else "listLocal"
                session.publish("web", "file", kind, {"path": "/"})
                rc = OK
            else:
                return {"type": "result", "ok": False, "error": f"acción desconocida: {action}"}
        except (lan.KobraError, ValueError, TypeError) as err:
            return {"type": "result", "ok": False, "error": str(err)}
        if rc == OK:
            return {"type": "result", "ok": True, "message": "Enviado"}
        return {"type": "result", "ok": False, "error": "no soportado" if rc == NOT_SUPPORTED else f"código {rc}"}

    def get_name(self) -> str:
        return "KobraX Wifi"

    def get_agent_info(self) -> pa.AgentInfo:
        return pa.AgentInfo(
            AGENT_ID,
            "anykubic",
            PLUGIN_VERSION,
            "Te permite conectarte a la KobraX por wifi",
        )

    # ------------------------------------------------------------------ helpers
    def _emit(self, name: str, *args: Any) -> None:
        fn = self._callbacks.get(name)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("callback %s failed", name)

    def _dispatch_status(self) -> None:
        if self._dev_id:
            self._emit("local_message", self._dev_id, json.dumps(self._state.push_status()))

    def _dispatch_json(self, payload: dict[str, Any]) -> None:
        if self._dev_id:
            self._emit("local_message", self._dev_id, json.dumps(payload))

    def _dispatch_version(self) -> None:
        module = {
            "name": "ota",
            "sw_ver": self._state.version or "unknown",
            "product_name": "Anycubic Kobra X",
        }
        self._dispatch_json({"info": {"command": "get_version", "result": "success", "module": [module]}})

    def _announce(self) -> None:
        with self._lock:
            dev_id, host, creds = self._dev_id, self._host, self._creds
            if not self.selected_dev_id and dev_id:
                self.selected_dev_id = dev_id
        if not dev_id or not host:
            return
        name = creds.device_name if creds else "Anycubic Kobra X"
        self._emit(
            "ssdp_msg",
            json.dumps(
                {
                    "dev_name": name,
                    "dev_id": dev_id,
                    "dev_ip": host,
                    "dev_type": creds.model_name if creds else name,
                    "dev_signal": "0",
                    "connect_type": "lan",
                    "bind_state": "free",
                    "sec_link": "secure",
                    "ssdp_version": "v1",
                }
            ),
        )

    @staticmethod
    def _run_in_thread(work: Callable[[], int]) -> int:
        """Run `work` on a plugin thread and wait for it (blocking network work never runs
        on the UI thread)."""
        result: dict[str, Any] = {}

        def runner() -> None:
            try:
                result["rc"] = work()
            except BaseException as err:  # noqa: BLE001
                result["err"] = err

        t = threading.Thread(target=runner, name="kobrax-job", daemon=True)
        t.start()
        t.join()
        if "err" in result:
            raise result["err"]
        return int(result.get("rc", ERR))

    def _wait_for(
        self, predicate: ReportPredicate, publish: Callable[[], Any], timeout: float
    ) -> dict[str, Any] | None:
        """Register a report predicate, run `publish`, wait for the matching report."""
        event = threading.Event()
        box: dict[str, Any] = {}

        def check(kind: str, payload: dict[str, Any]) -> bool:
            if not event.is_set() and predicate(kind, payload):
                box["payload"] = payload
                event.set()
                return True
            return False

        with self._lock:
            self._waiters.append(check)
        try:
            publish()
            return box["payload"] if event.wait(timeout) else None
        finally:
            with self._lock:
                if check in self._waiters:
                    self._waiters.remove(check)

    def _require_session(self) -> lan.KobraMqtt:
        session = self._session
        if session is None or not session.connected:
            raise lan.KobraError("La impresora no está conectada")
        return session

    def _refresh_info(self, timeout: float = 6) -> None:
        session = self._require_session()
        got = self._wait_for(lambda k, _p: k == "info", lambda: session.query("info"), timeout)
        if got is None:
            raise lan.KobraError("La impresora no respondió a la consulta de estado")

    # --------------------------------------------------------- MQTT -> OrcaSlicer
    def _on_report(self, topic: str, payload: dict[str, Any]) -> None:
        parts = topic.rsplit("/", 2)
        kind = parts[-2] if len(parts) == 3 and parts[-1] == "report" else ""
        if not kind:
            return  # bare /response ACKs carry nothing useful
        if kind == "axis":
            log.info("axis report: code=%s state=%s msg=%s", payload.get("code"), payload.get("state"), payload.get("msg"))
        with self._lock:
            waiters = list(self._waiters)
        for check in waiters:
            check(kind, payload)
        error = error_from_report(kind, payload)
        if error is not None and error != self._last_error:
            self._last_error = error
            self._notify_error(*error)
        had_version = bool(self._state.version)
        if self._state.update(kind, payload):
            self._dispatch_status()
            if not had_version and self._state.version:
                self._dispatch_version()  # OrcaSlicer asked before the first report arrived

    def _on_link(self, up: bool) -> None:
        if not up and self._dev_id:
            self._emit("local_connect", CONNECT_LOST, self._dev_id, "lost")

    # ---------------------------------------------------------- connection loop
    def _connection_loop(self, gen: int) -> None:
        backoff = 2.0
        lost_since: float | None = None
        last_info = last_box = 0.0
        while gen == self._gen:
            session = self._session
            if session is not None and not session.connected:
                lost_since = lost_since or time.monotonic()
                if time.monotonic() - lost_since < 8:  # give paho a chance to reconnect
                    self._sleep(gen, 0.5)
                    continue
                # Credentials change when the printer restarts: redo the handshake.
                session.close()
                with self._lock:
                    if self._session is session:
                        self._session = None
                session = None
            if session is None:
                try:
                    _info, creds = lan.handshake(self._host)
                    session = lan.KobraMqtt(
                        creds, self._on_report, self._on_link, work_dir=self._storage
                    )
                    session.connect()
                except Exception as err:  # noqa: BLE001
                    log.warning("connect to %s failed: %s", self._host, err)
                    if _is_audit_denial(err):
                        # OrcaSlicer is (or was) asking for permission. Retrying quickly
                        # would stack one identical prompt per attempt.
                        self._sleep(gen, AUDIT_RETRY_S)
                        continue
                    self._sleep(gen, backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                with self._lock:
                    if gen != self._gen:
                        session.close()
                        return
                    self._session, self._creds = session, creds
                backoff = 2.0
                log.info("connected to %s (%s)", creds.device_name, self._host)
                self._announce()
                self._emit("local_connect", CONNECT_OK, self._dev_id, "0")
                self._emit("printer_connected", self._dev_id)
                last_info = last_box = 0.0
            lost_since = None
            now = time.monotonic()
            try:
                if now - last_info >= POLL_INFO_S:
                    session.query("info")
                    last_info = now
                if now - last_box >= POLL_BOX_S:
                    session.query("multiColorBox", "getInfo")
                    session.query("light")
                    if self._state.project and not self._state.slice:
                        session.publish("web", "print", "getSliceParam", {"taskid": self._state.task_id})
                    last_box = now
            except lan.KobraError:
                pass
            self._sleep(gen, 0.5)
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()

    def _sleep(self, gen: int, seconds: float) -> None:
        end = time.monotonic() + seconds
        while gen == self._gen and time.monotonic() < end:
            time.sleep(0.1)

    # 1. Callbacks del host: guardar fn, devolver 0
    def set_server_callback(self, fn: Callable[[str, int], None] | None) -> int:
        self._callbacks["server_callback"] = fn
        return OK

    def set_on_ssdp_msg_fn(self, fn: Callable[[str], None] | None) -> int:
        self._callbacks["ssdp_msg"] = fn
        return OK

    def set_on_printer_connected_fn(self, fn: Callable[[str], None] | None) -> int:
        self._callbacks["printer_connected"] = fn
        return OK

    def set_on_subscribe_failure_fn(self, fn: Callable[[str], None] | None) -> int:
        self._callbacks["subscribe_failure"] = fn
        return OK

    def set_on_message_fn(self, fn: Callable[[str, str], None] | None) -> int:
        self._callbacks["on_message"] = fn
        return OK

    def set_on_user_message_fn(self, fn: Callable[[str, str], None] | None) -> int:
        self._callbacks["user_message"] = fn
        return OK

    def set_on_local_connect_fn(self, fn: Callable[[int, str, str], None] | None) -> int:
        self._callbacks["local_connect"] = fn
        return OK

    def set_on_local_message_fn(self, fn: Callable[[str, str], None] | None) -> int:
        self._callbacks["local_message"] = fn
        return OK

    def set_queue_on_main_fn(self, fn: Callable[[Callable[[], None]], None] | None) -> int:
        self._callbacks["queue_on_main"] = fn
        return OK

    # 2. Vinculación: la Kobra X no tiene, devolver éxito
    def check_cert(self) -> int:
        return OK

    def ping_bind(self, ping_code: str) -> int:
        return OK

    def bind(
        self,
        dev_ip: str,
        dev_id: str,
        dev_model: str,
        sec_link: str,
        timezone: str,
        improved: bool,
        update_fn: UpdateFn,
    ) -> int:
        return OK

    def unbind(self, dev_id: str) -> int:
        return OK

    def request_bind_ticket(self) -> tuple[int, str]:
        return (OK, "")

    # 3. No soportados (nube / Bambu): devolver -7010
    def send_message(self, dev_id: str, json_str: str, qos: int, flag: int) -> int:
        return NOT_SUPPORTED

    def start_print(
        self, params: pa.PrintParams, update_fn: UpdateFn, cancel_fn: CancelFn, wait_fn: WaitFn
    ) -> int:
        return NOT_SUPPORTED

    def start_local_print_with_record(
        self, params: pa.PrintParams, update_fn: UpdateFn, cancel_fn: CancelFn, wait_fn: WaitFn
    ) -> int:
        return NOT_SUPPORTED

    def start_sdcard_print(
        self, params: pa.PrintParams, update_fn: UpdateFn, cancel_fn: CancelFn
    ) -> int:
        return NOT_SUPPORTED

    # 4. Selección y descubrimiento
    def get_user_selected_machine(self) -> str:
        return self.selected_dev_id or ""

    def set_user_selected_machine(self, dev_id: str) -> int:
        self.selected_dev_id = dev_id or ""
        return OK

    def start_discovery(self, start: bool, sending: bool) -> bool:
        # The Kobra X does not answer SSDP M-SEARCH; the printer is announced from the
        # print host configured in the preset once connect_printer has reached it.
        if start:
            self._announce()
        return True

    # 5. Conexión: handshake 18910 + MQTT TLS 9883
    def connect_printer(self, params: pa.PrinterConnectionParams) -> int:
        host = _clean_host(params.host)
        if not host:
            log.error("connect_printer without host")
            return ERR
        self._storage_dir()
        self._start_webui()
        _preflight_network_permission(host)
        with self._lock:
            self._gen += 1
            gen = self._gen
            old, self._session = self._session, None
            self._dev_id = params.dev_id or host
            self._host = host
            self._access_code = params.password or ""
            self._state = PrinterState()
            self._state.filament_ids = self._resolve_filament_ids()
            self._state.nozzle_diameter = self._preset_nozzle_diameter()
            self._last_error = None
        if old is not None:
            threading.Thread(target=old.close, name="kobrax-close", daemon=True).start()
        threading.Thread(
            target=self._connection_loop, args=(gen,), name="kobrax-conn", daemon=True
        ).start()
        return OK

    def disconnect_printer(self) -> int:
        with self._lock:
            self._gen += 1
            session, self._session = self._session, None
        if session is not None:
            threading.Thread(target=session.close, name="kobrax-close", daemon=True).start()
        return OK

    def send_message_to_printer(self, dev_id: str, json_str: str, qos: int, flag: int) -> int:
        """Bambu-style requests from OrcaSlicer, translated to Kobra X commands."""
        try:
            req = json.loads(json_str)
        except ValueError:
            return ERR
        if not isinstance(req, dict):
            return ERR
        try:
            if (req.get("info") or {}).get("command") == "get_version":
                self._dispatch_version()
                return OK
            if (req.get("system") or {}).get("command") == "get_access_code":
                self._dispatch_json(
                    {"system": {"command": "get_access_code", "access_code": self._access_code}}
                )
                return OK
            if (req.get("pushing") or {}).get("command") in ("pushall", "start"):
                self._dispatch_status()
                return OK
            system_req = req.get("system") or {}
            if system_req.get("command") == "ledctrl":
                if system_req.get("led_node") != "chamber_light":
                    return OK  # chamber_light2 etc.: the Kobra X has one light
                return self._set_light(system_req.get("led_mode") == "on")
            print_req = req.get("print") or {}
            cmd = print_req.get("command")
            if cmd == "print_speed":
                return self._set_speed_mode(int(str(print_req.get("param") or "0")))
            if cmd == "set_fan":
                # New fan protocol; fan 1 = part cooling. Speed is 0-100 or 0-255 depending on
                # the OrcaSlicer build, so values above 100 are read as PWM.
                speed = float(print_req.get("speed") or 0)
                pct = speed * 100 / 255 if speed > 100 else speed
                return self._set_fan(pct, FAN_BY_INDEX.get(int(print_req.get("fan_index") or 0), ""))
            if cmd == "ams_filament_setting":
                return self._set_slot(print_req)
            if cmd == "ams_change_filament":
                return self._change_filament(print_req)
            if cmd in ("pause", "resume", "stop"):
                self._require_session().publish(
                    "web", "print", cmd, {"taskid": self._state.task_id}
                )
                return OK
            if cmd == "set_bed_temp" and isinstance(print_req.get("temp"), (int, float)):
                return self._set_temperature(bed=int(print_req["temp"]))
            if cmd == "set_nozzle_temp" and isinstance(print_req.get("target_temp"), (int, float)):
                return self._set_temperature(nozzle=int(print_req["target_temp"]))
            if cmd == "gcode_line":
                return self._gcode_line(str(print_req.get("param") or ""), print_req.get("sequence_id"))
        except (lan.KobraError, ValueError, TypeError) as err:
            log.warning("command failed: %s", err)
            return ERR
        return OK  # unknown requests are ignored, like OrcaSlicer's Moonraker agent

    def _set_temperature(self, nozzle: int | None = None, bed: int | None = None) -> int:
        cur_nozzle, cur_bed = self._state.targets()
        kind = 0 if bed is None else 1 if nozzle is None else 2
        data = {
            "type": kind,
            "target_nozzle_temp": max(0, min(300, cur_nozzle if nozzle is None else nozzle)),
            "target_hotbed_temp": max(0, min(120, cur_bed if bed is None else bed)),
        }
        self._require_session().publish("web", "tempature", "set", data)
        return OK

    def _box_and_slot(self, ams_id: Any, slot_id: Any) -> tuple[dict[str, Any], int]:
        box = self._state.box_for_unit(int(ams_id))
        slot = int(slot_id)
        if box is None or not 0 <= slot < len(box.get("slots") or []):
            raise lan.KobraError(f"No existe el slot {ams_id}/{slot_id}")
        return box, slot

    def _set_slot(self, req: dict[str, Any]) -> int:
        """Edit a slot's material/colour from OrcaSlicer -> multiColorBox/setInfo."""
        box, slot = self._box_and_slot(req.get("ams_id", 0), req.get("slot_id", req.get("tray_id", 0)))
        color = str(req.get("tray_color") or "FFFFFFFF")
        rgb = [int(color[i : i + 2], 16) for i in (0, 2, 4)] if len(color) >= 6 else [255, 255, 255]
        data = {"box_id": box.get("id", -1), "index": slot, "type": str(req.get("tray_type") or "PLA"), "color": rgb}
        session = self._require_session()
        session.publish("web", "multiColorBox", "setInfo", data)
        session.query("multiColorBox", "getInfo")
        return OK

    def _change_filament(self, req: dict[str, Any]) -> int:
        """Load / unload from the Device tab -> multiColorBox/feedFilament (1 load, 2 unload)."""
        if self._printing():
            return NOT_SUPPORTED
        unload = int(req.get("target", 0)) == 255 or int(req.get("slot_id", 0)) == 255
        box = self._state.box_for_unit(int(req.get("ams_id", 0)) if not unload else 0)
        if unload:
            box = next((b for b in self._state.boxes if int(b.get("loaded_slot", -1)) >= 0), box)
            if box is None or int(box.get("loaded_slot", -1)) < 0:
                return OK  # nothing loaded
            slot = int(box["loaded_slot"])
        else:
            box, slot = self._box_and_slot(req.get("ams_id", 0), req.get("slot_id", 0))
        data = {"multi_color_box": [{"id": box.get("id", -1), "feed_status": {"slot_index": slot, "type": 2 if unload else 1}}]}
        self._require_session().publish("slicer", "multiColorBox", "feedFilament", data)
        return OK

    def _set_light(self, on: bool) -> int:
        session = self._require_session()
        data = {"type": LIGHT_CAMERA, "status": 1 if on else 0, "brightness": 100 if on else 0}
        session.publish("web", "light", "control", data)
        session.query("light")  # the report updates the toggle in the Device tab
        return OK

    def _set_fan(self, pct: float, fan: str = "part") -> int:
        """part: fan/setSpeed (stribor). aux / box: print/update settings
        (ianloic/anycubic-orca-plugin). None of them tested on the Kobra X yet."""
        pct = int(max(0, min(100, round(pct))))
        session = self._require_session()
        if fan == "part":
            session.publish("web", "fan", "setSpeed", {"fan_speed_pct": pct})
        elif fan in ("aux", "box"):
            key = "aux_fan_speed_pct" if fan == "aux" else "box_fan_level"
            session.publish("web", "print", "update", {"taskid": self._state.task_id, "settings": {key: pct}})
        else:
            return NOT_SUPPORTED
        return OK

    def _set_speed_mode(self, level: int) -> int:
        """Bambu speed level (1 silent .. 4 ludicrous) -> Kobra X print_speed_mode."""
        if level not in SPEED_FROM_ORCA:
            return NOT_SUPPORTED  # ludicrous has no Kobra X equivalent
        settings = {"print_speed_mode": SPEED_FROM_ORCA[level]}
        self._require_session().publish(
            "web", "print", "update", {"taskid": self._state.task_id, "settings": settings}
        )
        return OK

    def _gcode_line(self, gcode: str, sequence_id: Any) -> int:
        """The Kobra X takes no raw G-code; temperature and part-fan lines are translated."""
        words = gcode.strip().upper().split()
        args = {w[0]: w[1:] for w in words[1:] if len(w) > 1}
        rc = NOT_SUPPORTED
        try:
            value = float(args["S"]) if "S" in args else None
            if words and value is not None:
                if words[0] in ("M104", "M109"):
                    rc = self._set_temperature(nozzle=int(value))
                elif words[0] in ("M140", "M190"):
                    rc = self._set_temperature(bed=int(value))
                elif words[0] == "M106":  # S is PWM 0-255; P1 part, P2 aux, P3 chamber
                    rc = self._set_fan(value * 100 / 255, FAN_BY_INDEX.get(int(args.get("P", "1")), ""))
            elif words and words[0] == "M107":
                rc = self._set_fan(0, FAN_BY_INDEX.get(int(args.get("P", "1")), ""))
            elif words and words[0] in ("M84", "M18") and not self._printing():
                self._require_session().publish("web", "axis", "turnOff")
                self._state.homed = False  # motors off: positions are lost
                rc = OK
        except ValueError:
            rc = ERR
        except lan.KobraError:
            rc = ERR
        reply: dict[str, Any] = {
            "command": "gcode_line",
            "param": gcode,
            "result": "success" if rc == OK else "failed",
        }
        if sequence_id is not None:
            reply["sequence_id"] = sequence_id
        self._dispatch_json({"print": reply})
        return rc

    def bind_detect(self, dev_ip: str, sec_link: str, detect: pa.DetectResult) -> int:
        host = _clean_host(dev_ip) or self._host
        info: dict[str, Any] = {}

        def work() -> int:
            info.update(lan.fetch_info(host))
            return OK

        try:
            self._run_in_thread(work)
        except Exception as err:  # noqa: BLE001
            detect.result_msg = str(err)
            return ERR
        detect.dev_id = self._dev_id or host
        detect.model_id = str(info.get("modelId") or "")
        detect.dev_name = str(info.get("deviceName") or "Anycubic Kobra X")
        detect.version = self._state.version
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return OK

    # 6. Impresión: subir archivo (y verify_job), después print/start
    def start_send_gcode_to_sdcard(
        self, params: pa.PrintParams, update_fn: UpdateFn, cancel_fn: CancelFn, wait_fn: WaitFn
    ) -> int:
        if params.project_name == "verify_job":
            session = self._session
            return OK if session is not None and session.connected else ERR_CONNECTION_FAILED
        return self._job(params, update_fn, cancel_fn, start=False)

    def start_local_print(
        self, params: pa.PrintParams, update_fn: UpdateFn, cancel_fn: CancelFn
    ) -> int:
        return self._job(params, update_fn, cancel_fn, start=True)

    def _job(self, params: Any, update_fn: UpdateFn, cancel_fn: CancelFn, start: bool) -> int:
        def update(stage: int, pct: int, msg: str) -> None:
            if update_fn is not None:
                update_fn(stage, pct, msg)

        def check_cancel() -> None:
            if cancel_fn is not None and cancel_fn():
                raise _Canceled()

        step = "prepare"
        work_dir = self._storage_dir()

        def work() -> int:
            nonlocal step
            update(STAGE_CREATE, 0, "Preparando…")
            self._require_session()
            gcode = prepare_gcode(params, work_dir)
            try:
                if start:
                    self._refresh_info()
                    if not self._state.is_idle:
                        raise lan.KobraError("La impresora está ocupada")
                check_cancel()
                step = "upload"
                self._refresh_info()  # the upload token changes when the printer restarts
                url = self._state.upload_url
                if not url:
                    raise lan.KobraError("La impresora no informó la URL de subida")
                update(STAGE_UPLOAD, 0, f"Subiendo {gcode.remote_name}…")
                lan.upload_file(url, self._host, gcode.path, gcode.remote_name, PLUGIN_VERSION)
                update(STAGE_UPLOAD, 100, "Archivo subido")
                log.info("uploaded %s (%d bytes)", gcode.remote_name, gcode.size)
                if not start:
                    update(STAGE_FINISHED, 100, "Archivo subido")
                    return OK
                check_cancel()
                step = "start"
                session = self._require_session()
                self._wait_for(
                    lambda k, _p: k == "multiColorBox",
                    lambda: session.query("multiColorBox", "getInfo"),
                    6,
                )
                mapping = build_box_mapping(
                    gcode, self._state.slots(), getattr(params, "ams_mapping", "")
                )
                payload = build_start_payload(gcode, mapping, params)
                update(STAGE_SENDING, 0, "Iniciando impresión…")
                # Sent exactly once: a repeated print/start could start a second job.
                ack = self._wait_for(
                    lambda k, p: k == "print" and p.get("action") == "start",
                    lambda: session.publish("slicer", "print", "start", payload),
                    15,
                )
                if ack is None:
                    log.warning("no ACK for print/start of %s", gcode.remote_name)
                    raise lan.KobraError(
                        "La impresora no confirmó el inicio. Revisala antes de volver a enviar."
                    )
                if ack.get("code") != 200:
                    raise lan.KobraError(
                        f"La impresora rechazó la impresión: {ack.get('msg') or ack.get('code')}"
                    )
                log.info("print/start accepted (state=%s)", ack.get("state"))
                update(STAGE_FINISHED, 100, "Impresión iniciada")
                return OK
            finally:
                gcode.cleanup()

        try:
            return self._run_in_thread(work)
        except _Canceled:
            return ERR_CANCELED
        except JobError as err:
            log.error("job failed: %s", err)
            update(STAGE_CREATE, 0, str(err))
            return ERR_FILE_NOT_EXIST
        except Exception as err:  # noqa: BLE001
            log.error("job failed at %s: %s", step, err)
            update(STAGE_CREATE, 0, str(err))
            if step == "start":
                return ERR_LP_PUBLISH_FAILED
            if step == "upload":
                return ERR_LP_UPLOAD_FAILED if start else ERR_SG_UPLOAD_FAILED
            return ERR_CONNECTION_FAILED

    # Opcionales: la Kobra X no acepta G-code crudo
    def command_set_bed(self, dev_id, temp, supports_mqtt_bed_ctrl, sequence_id, lan_mode) -> int:
        try:
            return self._set_temperature(bed=int(temp))
        except lan.KobraError:
            return ERR

    def command_set_nozzle(self, dev_id, temp, sequence_id, lan_mode) -> int:
        try:
            return self._set_temperature(nozzle=int(temp))
        except lan.KobraError:
            return ERR

    # Movement: Kobra X axis/move over MQTT (stribor/anycubic_kobrax). Untested on hardware.
    def _printing(self) -> bool:
        return self._state.gcode_state() in ("RUNNING", "PREPARE", "PAUSE")

    def command_go_home(self, dev_id, is_printing, supports_mqtt_homing, sequence_id, lan_mode) -> int:
        if is_printing or self._printing():
            return NOT_SUPPORTED
        try:
            data = {"axis": AXIS_HOME_XYZ, "move_type": MOVE_HOME, "distance": 0}
            self._require_session().publish("web", "axis", "move", data)
        except lan.KobraError:
            return ERR
        # OrcaSlicer only allows jogging axes it believes are homed (home_flag bits 0-2).
        self._state.homed = True
        self._dispatch_status()
        return OK

    def command_axis_control(
        self, dev_id, axis, unit, input_val, speed, is_core_xy, supports_mqtt_axis_control, sequence_id, lan_mode
    ) -> int:
        axis_id = AXIS_IDS.get(str(axis).upper())
        if axis_id is None or self._printing():
            return NOT_SUPPORTED  # E (extrude) has no known MQTT command
        # same as OrcaSlicer's "G1 <axis><unit*value>", with the Z sign adapted (see AXIS_SIGN)
        distance = float(unit) * float(input_val) * AXIS_SIGN[str(axis).upper()]
        if distance == 0:
            return OK
        magnitude = abs(distance)
        data = {
            "axis": axis_id,
            "move_type": MOVE_POSITIVE if distance > 0 else MOVE_NEGATIVE,
            "distance": int(magnitude) if magnitude.is_integer() else magnitude,
        }
        try:
            self._require_session().publish("web", "axis", "move", data)
        except lan.KobraError:
            return ERR
        return OK

    def command_auto_leveling(self, dev_id, sequence_id, lan_mode) -> int:
        return NOT_SUPPORTED

    def command_xyz_abs(self, dev_id, sequence_id, lan_mode) -> int:
        return OK  # axis/move is always relative; nothing to switch

    # Camera: stream URL from info.urls.rtspUrl, played by OrcaSlicer's GStreamer player
    def command_start_camera(self, dev_id) -> int:
        try:
            self._require_session().publish("web", "video", "startCapture")
        except lan.KobraError:
            return ERR
        return OK

    def get_camera_stream_mode(self):
        # Despite its name, info.urls.rtspUrl is http://<ip>:18088/... (HTTP-FLV) on fw 2.0.2.2.
        url = self._state.camera_url
        if url.startswith("rtsp://"):
            return pa.CameraStreamMode.RTSP
        if url.startswith("http://"):
            return pa.CameraStreamMode.HTTP
        return pa.CameraStreamMode.None_

    def get_camera_url(self) -> str:
        url = self._state.camera_url
        session = self._session
        now = time.monotonic()
        if url and session is not None and session.connected and now - self._camera_started > 60:
            # The stream may need capture to be running; asking again is harmless.
            self._camera_started = now
            try:
                session.publish("web", "video", "startCapture")
            except lan.KobraError:
                pass
        return url

    def get_filament_sync_mode(self):
        return pa.FilamentSyncMode.Pull

    def fetch_filament_info(self, dev_id: str, sync_mode) -> bool:
        session = self._session
        if session is None or not session.connected:
            return False
        try:
            session.query("multiColorBox", "getInfo")  # its report triggers a status push
        except lan.KobraError:
            return False
        self._dispatch_status()
        return True


if orca is not None:

    @orca.plugin
    class MiPlugin(orca.base):
        def register_capabilities(self):
            try:
                # Declared up front so the first print job is not refused: a prompt raised on
                # the job thread is shown asynchronously and the call that raised it fails.
                #  - fs_read: the sliced .3mf OrcaSlicer hands to start_* lives in a temp folder
                #    whose subfolder changes every session, so the whole tree is granted.
                #  - threads, plus the loopback binds asked about on the first connection.
                # The printer's own address is asked once and then remembered.
                # No "('', 0)": OrcaSlicer cannot match a host-less address against the saved grant
                # and would ask on every launch; lan._LanClient never binds it.
                sockets = ["('127.0.0.1', 0)", f"('127.0.0.1', {WEBUI_PORT})"]
                printer = preset_printer_host()
                if printer:
                    sockets.append(printer)  # same dialog instead of a separate prompt later
                orca.request_permissions(
                    fs_read=[os.path.join(orca_model_dir(), "*")],
                    network_socket=sockets,
                    threading=True,
                )
            except (AttributeError, TypeError):
                log.warning("orca.request_permissions not available in this OrcaSlicer")
            orca.register_capability(KobraX)
            page = make_page_capability(orca, lambda: KobraX._instance)
            if page is not None:
                orca.register_capability(page)
