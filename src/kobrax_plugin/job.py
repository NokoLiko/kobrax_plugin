"""Print job preparation: pick the G-code, read its filaments, build `print/start`."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .status import SLOT_LOADED

log = logging.getLogger("kobrax_plugin")

_PLATE_RE = re.compile(r"Metadata/plate_\d+\.gcode")
_TOOL_RE = re.compile(rb"^T(\d+)\s*(?:;|$)", re.M)


class JobError(RuntimeError):
    pass


@dataclass
class GcodeFile:
    path: str
    remote_name: str
    size: int
    md5: str
    filament_types: list[str] = field(default_factory=list)
    filament_colors: list[list[int]] = field(default_factory=list)
    used_tools: list[int] = field(default_factory=list)
    temp_path: str | None = None  # extracted from a .3mf; delete after the job

    def cleanup(self) -> None:
        if self.temp_path:
            try:
                os.remove(self.temp_path)
            except OSError:
                pass


def safe_remote_name(name: str) -> str:
    base = os.path.basename(name or "")
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("._") or "orca_print"
    return base[:90] + ".gcode"


def _extract_plate(archive: str, plate_index: int, work_dir: str | None) -> str:
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        wanted = f"Metadata/plate_{plate_index}.gcode"
        if wanted not in names:
            plates = [n for n in names if _PLATE_RE.fullmatch(n)]
            if len(plates) != 1:  # another plate's G-code would print the wrong part
                raise JobError(f"No encontré el G-code de la placa {plate_index} en el .3mf")
            wanted = plates[0]
        fd, out = tempfile.mkstemp(prefix="kobrax-", suffix=".gcode", dir=work_dir)
        os.close(fd)  # reopen by path: the audit hook can only allow path-based opens
        with open(out, "wb") as dst, zf.open(wanted) as src:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    return out


def _parse_hex_colors(value: str) -> list[list[int]]:
    colors = []
    for item in value.split(";"):
        item = item.strip().lstrip("#")
        if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", item):
            colors.append([int(item[i : i + 2], 16) for i in (0, 2, 4)])
        else:
            colors.append([255, 255, 255])
    return colors


def scan_gcode(path: str) -> tuple[str, list[str], list[list[int]], list[int]]:
    """md5, filament types, filament colours and the tool numbers the file selects."""
    md5 = hashlib.md5()
    types: list[str] = []
    colors: list[list[int]] = []
    tools: set[int] = set()
    with open(path, "rb") as fh:
        for line in fh:
            md5.update(line)
            if line.startswith(b"T") and (m := _TOOL_RE.match(line)):
                tools.add(int(m.group(1)))
            elif line.startswith(b"; filament_type = "):
                types = [t.strip() for t in line[18:].decode(errors="replace").split(";")]
            elif line.startswith(b"; filament_colour = "):
                colors = _parse_hex_colors(line[20:].decode(errors="replace").strip())
    return md5.hexdigest(), types, colors, sorted(tools) or [0]


def prepare_gcode(params: Any, work_dir: str | None = None) -> GcodeFile:
    """Use dst_file / filename when it is G-code; extract the plate from a .3mf otherwise."""
    candidates = [getattr(params, "dst_file", ""), getattr(params, "filename", "")]
    source = next((c for c in candidates if c and os.path.isfile(c)), "")
    if not source:
        raise JobError("No encuentro el archivo a enviar")
    temp_path = None
    path = source
    if source.lower().endswith(".3mf"):
        plate = int(getattr(params, "plate_index", 0) or 0) or 1
        path = temp_path = _extract_plate(source, plate, work_dir)
    name_hint = getattr(params, "project_name", "") or getattr(params, "task_name", "") or source
    md5, types, colors, tools = scan_gcode(path)
    return GcodeFile(
        path=path,
        remote_name=safe_remote_name(name_hint),
        size=os.path.getsize(path),
        md5=md5,
        filament_types=types,
        filament_colors=colors,
        used_tools=tools,
        temp_path=temp_path,
    )


def _parse_mapping(raw: str) -> list[int]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [int(v) for v in value] if isinstance(value, list) else []


def build_box_mapping(
    gcode: GcodeFile, slots: list[dict[str, Any]], ams_mapping: str
) -> list[dict[str, Any]]:
    """One entry per filament the file uses: paint_index (filament) -> ams_index (slot)."""
    mapping = _parse_mapping(ams_mapping)
    loaded = [i for i, s in enumerate(slots) if s.get("status") == SLOT_LOADED]
    entries = []
    for tool in gcode.used_tools:
        slot_idx = mapping[tool] if tool < len(mapping) else -1
        if slot_idx < 0:
            material = gcode.filament_types[tool] if tool < len(gcode.filament_types) else ""
            if len(gcode.used_tools) == 1:
                # Single filament without a mapping: first loaded slot of the same material.
                same = [i for i in loaded if _same_material(slots[i].get("type"), material)]
                slot_idx = (same or loaded or [0])[0]
            else:
                slot_idx = tool
            log.warning("No AMS mapping for filament %d; using slot %d", tool, slot_idx + 1)
        if not 0 <= slot_idx < max(len(slots), 1):
            raise JobError(f"El filamento {tool + 1} apunta a un slot inexistente ({slot_idx + 1})")
        slot = slots[slot_idx] if slot_idx < len(slots) else {}
        if slots and slot.get("status") != SLOT_LOADED:
            raise JobError(f"El slot {slot_idx + 1} no tiene filamento cargado")
        color = gcode.filament_colors[tool] if tool < len(gcode.filament_colors) else [255, 255, 255]
        slot_color = list(slot.get("color") or color)[:4]
        if len(slot_color) == 3:
            slot_color.append(255)
        entries.append(
            {
                "paint_index": tool,
                "ams_index": slot_idx,
                "paint_color": color,
                "ams_color": slot_color,
                "material_type": str(slot.get("type") or (gcode.filament_types[tool] if tool < len(gcode.filament_types) else "PLA")),
            }
        )
    return entries


def _same_material(slot_type: Any, gcode_type: str) -> bool:
    a = str(slot_type or "").split()
    b = gcode_type.split()
    return bool(a and b and a[0].upper() == b[0].upper())


def build_start_payload(gcode: GcodeFile, box_mapping: list[dict[str, Any]], params: Any) -> dict[str, Any]:
    """`print/start` data, in the shape validated on Kobra X fw 2.0.2.2."""
    return {
        "taskid": "-1",
        "url": "",  # empty URL = print the file already stored on the printer
        "filename": gcode.remote_name,
        "md5": gcode.md5,
        "filepath": None,
        "filetype": 1,
        "project_type": 1,
        "filesize": gcode.size,
        "ams_settings": {"use_ams": True, "ams_box_mapping": box_mapping},
        "task_settings": {
            "auto_leveling": int(bool(getattr(params, "task_bed_leveling", True))),
            "vibration_compensation": int(bool(getattr(params, "task_vibration_cali", False))),
            "flow_calibration": int(bool(getattr(params, "task_flow_cali", False))),
            "dry_mode": 0,
            "ai_settings": {"status": 0, "count": 0, "type": 0},
            "timelapse": {
                "status": int(bool(getattr(params, "task_record_timelapse", False))),
                "count": 0,
                "type": 0,
            },
            "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
            "model_objects_skip_parts": [],
        },
    }
