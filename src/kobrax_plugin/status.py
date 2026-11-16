"""Kobra X reports -> Bambu-style `push_status` JSON that OrcaSlicer's device layer parses.

Field names follow the report shapes captured from a Kobra X (fw 2.0.2.2); the output
mirrors what OrcaSlicer's own MoonrakerPrinterAgent sends.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

# Anycubic project/work state -> Bambu gcode_state
_STATE_MAP = {
    "printing": "RUNNING",
    "resuming": "RUNNING",
    "preheating": "PREPARE",
    "heating": "PREPARE",
    "checking": "PREPARE",
    "auto_leveling": "PREPARE",
    "leveling": "PREPARE",
    "downloading": "PREPARE",
    "pausing": "PAUSE",
    "paused": "PAUSE",
    "finished": "FINISH",
    "complete": "FINISH",
    "completed": "FINISH",
    "stopping": "FAILED",
    "stoped": "FAILED",  # sic, firmware spelling
    "stopped": "FAILED",
    "canceled": "FAILED",
    "cancelled": "FAILED",
    "failed": "FAILED",
    "error": "FAILED",
}
_STAGE = {"RUNNING": 1, "PREPARE": 1, "PAUSE": 2, "FINISH": 3, "FAILED": 4}
# Anycubic project state -> Bambu stg_cur (DeviceManager.cpp get_stage_string)
_SUB_STAGE = {
    "printing": 0,
    "auto_leveling": 1,
    "leveling": 1,
    "preheating": 2,  # "Heatbed preheating"
    "heating": 7,  # "Heating nozzle"
}

# Anycubic print_speed_mode <-> Bambu spd_lvl (1 silent, 2 standard, 3 sport, 4 ludicrous).
# Anycubic: 1 = standard (100 %), 2 = silent (80 %), 3 = sport (150 %), as documented by
# ianloic/anycubic-orca-plugin for the Kobra 3 / S1 (not yet confirmed on the Kobra X).
SPEED_TO_ORCA = {1: 2, 2: 1, 3: 3}
SPEED_FROM_ORCA = {v: k for k, v in SPEED_TO_ORCA.items()}

LIGHT_CAMERA = 3  # the only light the Kobra X reports (light/query, type 3)

SLOT_LOADED = 5  # multiColorBox slot status observed for a loaded, usable slot
HAS_SDCARD_NORMAL = 1  # DevStorage::HAS_SDCARD_NORMAL


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Anycubic material names -> OrcaFilamentLibrary generic family ("Generic <family> @System")
_FAMILIES = {
    "PLA": "PLA", "PLA+": "PLA", "PLA PRO": "PLA", "PLA BASIC": "PLA", "PLA MATTE": "PLA",
    "PLA HIGH SPEED": "PLA High Speed", "PLA HS": "PLA High Speed", "HS PLA": "PLA High Speed",
    "PLA SILK": "PLA Silk", "SILK PLA": "PLA Silk", "PLA-CF": "PLA-CF",
    "PETG": "PETG", "PETG+": "PETG", "PET": "PETG",
    "ABS": "ABS", "ABS+": "ABS", "ASA": "ASA",
    "TPU": "TPU", "TPU 95A": "TPU", "PA": "PA", "NYLON": "PA", "PC": "PC",
    "PVA": "PVA", "HIPS": "HIPS",
}
FAMILIES = sorted(set(_FAMILIES.values()))


def family_of(material: str) -> str:
    key = " ".join(str(material or "").upper().split())
    if key in _FAMILIES:
        return _FAMILIES[key]
    first = key.split(" ")[0] if key else ""
    return _FAMILIES.get(first, "")


def _pct_to_255(value: Any) -> int | None:
    pct = _num(value)
    if pct is None:
        return None
    return max(0, min(255, int(pct * 255 / 100 + 0.5)))


def rgb_hex(color: Any) -> str:
    """[r, g, b(, a)] -> 'RRGGBBAA'."""
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        return "00000000"
    parts = [max(0, min(255, int(c))) for c in color[:4]]
    if len(parts) == 3:
        parts.append(255)
    return "".join(f"{c:02X}" for c in parts)


class PrinterState:
    """Thread-safe merge of the latest Kobra X reports."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.info: dict[str, Any] = {}
        self.temp: dict[str, Any] = {}
        self.fan: dict[str, Any] = {}
        self.boxes: list[dict[str, Any]] = []
        self.work_state = ""
        self.lights: list[dict[str, Any]] = []
        # The Kobra X does not report homed axes; set after a home command it accepted.
        self.homed = False
        # material type (upper case) -> OrcaSlicer filament_id, filled by the agent from presets
        self.filament_ids: dict[str, str] = {}
        self.nozzle_diameter = ""  # from the printer preset; the Kobra X does not report it
        self.slice: dict[str, Any] = {}  # last print/getSliceParam data
        self.files: dict[str, dict[str, Any]] = {}  # last result per file/<action>
        self.peripherals: dict[str, Any] = {}

    # ------------------------------------------------------------------ input
    def update(self, kind: str, payload: dict[str, Any]) -> bool:
        """Merge one report. Returns True when it carried printer state."""
        data = payload.get("data")
        with self._lock:
            if kind == "info" and isinstance(data, dict):
                self.info = data
                if isinstance(data.get("temp"), dict):
                    self.temp.update(data["temp"])
                for key in ("fan_speed_pct", "aux_fan_speed_pct", "box_fan_speed_pct", "box_fan_level"):
                    if key in data:
                        self.fan[key] = data[key]
                return True
            if kind == "tempature" and isinstance(data, dict):
                self.temp.update(data)
                return True
            if kind == "fan" and isinstance(data, dict):
                self.fan.update(data)
                return True
            if kind == "multiColorBox" and isinstance(data, dict):
                boxes = data.get("multi_color_box")
                if isinstance(boxes, list):
                    self.boxes = [b for b in boxes if isinstance(b, dict)]
                    return True
            if kind == "light" and isinstance(data, dict) and isinstance(data.get("lights"), list):
                self.lights = [x for x in data["lights"] if isinstance(x, dict)]
                return True
            if kind == "peripherie" and isinstance(data, dict):
                self.peripherals = data
                return False  # nothing OrcaSlicer shows directly
            if kind == "status" and payload.get("state"):
                self.work_state = str(payload["state"])
                return True
            if kind == "print" and payload.get("action") == "getSliceParam":
                if isinstance(data, dict) and isinstance(data.get("slice_param"), dict):
                    self.slice = data  # job settings + base64 PNG thumbnail
                return False
            if kind == "file":
                self.files[str(payload.get("action") or "")] = {
                    "code": payload.get("code"),
                    "state": payload.get("state"),
                    "msg": payload.get("msg"),
                    "data": data,
                    "time": time.strftime("%H:%M:%S"),
                }
                return False
            if kind == "print" and isinstance(data, dict) and (
                "progress" in data or "curr_layer" in data or "filename" in data
            ):
                # Live print reports carry the same fields as info.project.
                project = dict(self.info.get("project") or {})
                project.update(data)
                if payload.get("state"):
                    project.setdefault("state", payload["state"])
                self.info = {**self.info, "project": project}
                return True
        return False

    # ----------------------------------------------------------------- queries
    @property
    def version(self) -> str:
        return str(self.info.get("version") or "")

    @property
    def camera_url(self) -> str:
        urls = self.info.get("urls")
        url = str(urls.get("rtspUrl") or "") if isinstance(urls, dict) else ""
        return url if url.startswith(("rtsp://", "http://")) else ""

    def box_for_unit(self, unit_idx: int) -> dict[str, Any] | None:
        """The Kobra X box behind OrcaSlicer AMS unit `unit_idx` (same order as ams_payload)."""
        with self._lock:
            boxes = sorted(self.boxes, key=lambda b: (b.get("id", 0) != -1, b.get("id", 0)))
        return boxes[unit_idx] if 0 <= unit_idx < len(boxes) else None

    def filament_id(self, material: str) -> str:
        return self.filament_ids.get(family_of(material), "")

    @property
    def light_on(self) -> bool | None:
        with self._lock:
            for light in self.lights:
                if light.get("type") == LIGHT_CAMERA:
                    return bool(light.get("status"))
        return None

    @property
    def upload_url(self) -> str:
        urls = self.info.get("urls")
        return str(urls.get("fileUploadurl") or "") if isinstance(urls, dict) else ""

    @property
    def project(self) -> dict[str, Any]:
        project = self.info.get("project")
        return project if isinstance(project, dict) else {}

    @property
    def task_id(self) -> str:
        tid = self.project.get("taskid") or self.project.get("task_id")
        return str(tid) if tid not in (None, "") else "-1"

    @property
    def is_idle(self) -> bool:
        return str(self.info.get("state") or "") == "free" and not self.project

    def slots(self) -> list[dict[str, Any]]:
        """Onboard four-slot box (id -1) first, then ACE units, flattened."""
        with self._lock:
            boxes = sorted(self.boxes, key=lambda b: (b.get("id", 0) != -1, b.get("id", 0)))
            out: list[dict[str, Any]] = []
            for box in boxes:
                for slot in box.get("slots") or []:
                    if isinstance(slot, dict):
                        out.append(slot)
            return out

    def targets(self) -> tuple[int, int]:
        with self._lock:
            nozzle = int(_num(self.temp.get("target_nozzle_temp")) or 0)
            bed = int(_num(self.temp.get("target_hotbed_temp")) or 0)
        return nozzle, bed

    # ------------------------------------------------------------------ output
    def gcode_state(self) -> str:
        project = self.project
        state = str(project.get("state") or "").lower()
        if state in _STATE_MAP:
            return _STATE_MAP[state]
        if project and str(self.info.get("state") or "") == "busy":
            return "RUNNING"
        return "IDLE"

    def push_status(self) -> dict[str, Any]:
        state = self.gcode_state()
        p: dict[str, Any] = {
            "command": "push_status",
            "msg": 0,
            "support_mqtt_alive": True,
            "gcode_state": state,
            "mc_print_stage": _STAGE.get(state, 0),
            "mc_print_error_code": 0,
            "print_error": 0,
            "sdcard": True,
            # bits 0-2 = X/Y/Z homed (MachineObject::is_axis_at_home), bits 8-9 = storage
            "home_flag": (HAS_SDCARD_NORMAL << 8) | (0b111 if self.homed else 0),
            "nozzle_temp_range": [0, 300],
            "bed_temp_range": [0, 120],
            "support_send_to_sd": True,
        }
        # Send-dialog options (SelectMachineDialog::update_option_opts) from the printer's features
        features = self.info.get("features") if isinstance(self.info.get("features"), dict) else {}
        p["support_bed_leveling"] = 1 if features.get("auto_leveling_support", True) else 0
        if "flow_calibration_support" in features:
            p["support_flow_calibration"] = bool(features["flow_calibration_support"])
        if "camera_timelapse_support" in features:
            p["support_timelapse"] = bool(features["camera_timelapse_support"])
        with self._lock:
            temp = dict(self.temp)
            fan = dict(self.fan)
        for src, dst in (
            ("curr_nozzle_temp", "nozzle_temper"),
            ("target_nozzle_temp", "nozzle_target_temper"),
            ("curr_hotbed_temp", "bed_temper"),
            ("target_hotbed_temp", "bed_target_temper"),
        ):
            value = _num(temp.get(src))
            if value is not None:
                p[dst] = value
        # fan_gear packs three 0-255 speeds: part cooling | aux << 8 | chamber/box << 16 (DevFan.cpp)
        box_fan = fan.get("box_fan_level", fan.get("box_fan_speed_pct"))
        gears = [_pct_to_255(v) for v in (fan.get("fan_speed_pct"), fan.get("aux_fan_speed_pct"), box_fan)]
        if gears[0] is not None:
            p["fan_gear"] = sum((g or 0) << (8 * i) for i, g in enumerate(gears))

        # The Kobra X has part, auxiliary and box fans: lets the Device tab show all three.
        p["support_aux_fan"] = True
        p["support_chamber_fan"] = True

        speed_mode = _num(self.info.get("print_speed_mode"))
        if speed_mode is not None and int(speed_mode) in SPEED_TO_ORCA:
            p["spd_lvl"] = SPEED_TO_ORCA[int(speed_mode)]

        camera = self.camera_url
        if camera:
            p["ipcam"] = {
                "ipcam_dev": "1",
                "rtsp_url": camera,
                "liveview": {"local": "rtsp" if camera.startswith("rtsp://") else "http", "remote": "none"},
            }
        if self.nozzle_diameter:
            # Lets OrcaSlicer match the device to the printer preset (its log otherwise shows
            # "update printer preset name failed ... nozzle_diameter_str0.0").
            p["nozzle_diameter"] = self.nozzle_diameter

        light = self.light_on
        if light is not None:
            p["lights_report"] = [{"node": "chamber_light", "mode": "on" if light else "off"}]

        project = self.project
        if project:
            filename = str(project.get("filename") or "")
            if filename:
                p["subtask_name"] = os.path.basename(filename)
                p["gcode_file"] = filename
            progress = _num(project.get("progress"))
            if progress is not None:
                p["mc_percent"] = max(0, min(100, int(progress)))
            remain = _num(project.get("remain_time"))  # minutes
            if remain is not None:
                p["mc_remaining_time"] = max(0, int(remain))
            layer = _num(project.get("curr_layer"))
            total = _num(project.get("total_layers"))
            if layer is not None:
                p["layer_num"] = int(layer)
            if total is not None:
                p["total_layer_num"] = int(total)
            sub = _SUB_STAGE.get(str(project.get("state") or "").lower())
            if sub is not None:
                p["stg_cur"] = sub
                p["mc_print_sub_stage"] = sub

        ams = self.ams_payload()
        if ams:
            p["ams"] = ams
        return {"print": p, "t_utc": int(time.time() * 1000)}

    def ams_payload(self) -> dict[str, Any] | None:
        """Bambu `print.ams` block, one unit per Kobra X box (4 trays each)."""
        with self._lock:
            boxes = sorted(self.boxes, key=lambda b: (b.get("id", 0) != -1, b.get("id", 0)))
        if not boxes:
            return None
        units = []
        ams_bits = 0
        tray_bits = 0
        for unit_idx, box in enumerate(boxes):
            ams_bits |= 1 << unit_idx
            trays = []
            slots = [s for s in box.get("slots") or [] if isinstance(s, dict)][:4]
            for slot_idx, slot in enumerate(slots):
                tray: dict[str, Any] = {"id": str(slot_idx), "tag_uid": "0000000000000000"}
                if slot.get("status") == SLOT_LOADED:
                    tray_bits |= 1 << (unit_idx * 4 + slot_idx)
                    color = rgb_hex(slot.get("color"))
                    material = str(slot.get("type") or "")
                    tray.update(
                        {
                            "tray_info_idx": self.filament_id(material),
                            "tray_type": material,
                            "tray_color": color,
                            "cols": [color],
                            "ctype": 1,
                        }
                    )
                    if not tray["tray_info_idx"]:
                        # Unknown material: keeps the tray visible (in its colour) in the
                        # mapping dialog without replacing the project's filament on sync.
                        tray["tray_slot_placeholder"] = "1"
                else:
                    tray.update(
                        {
                            "tray_info_idx": "",
                            "tray_type": "",
                            "tray_color": "00000000",
                            "tray_slot_placeholder": "1",
                        }
                    )
                trays.append(tray)
            unit: dict[str, Any] = {"id": str(unit_idx), "info": "0002", "tray": trays}
            humidity = _num(box.get("humidity"))
            if humidity:  # the onboard box reports 0 (no sensor)
                unit["humidity_raw"] = str(int(humidity))
            temp = _num(box.get("temp"))
            if temp:
                unit["temp"] = str(temp)
            drying = box.get("drying_status")
            if isinstance(drying, dict) and drying.get("status"):
                remain = _num(drying.get("remain_time"))
                if remain is not None:
                    unit["dry_time"] = int(remain)
            units.append(unit)
        return {
            "ams": units,
            "ams_exist_bits": f"{ams_bits:X}",
            "tray_exist_bits": f"{tray_bits:X}",
        }
