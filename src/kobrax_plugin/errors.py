"""Anycubic printer error codes -> messages.

The code table is copied from stribor/anycubic_kobrax
(custom_components/anycubic_kobrax/error_codes.py), used under the MIT License:

    MIT License

    Copyright (c) 2026 stribor

    Permission is hereby granted, free of charge, to any person obtaining a copy of this
    software and associated documentation files (the "Software"), to deal in the Software
    without restriction, including without limitation the rights to use, copy, modify, merge,
    publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
    to whom the Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all copies or
    substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
    INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
    PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
    FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
    DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

from typing import Any

OK_CODES = {0, 200}
EVENT_TYPES = {"event", "printerevent", "printer_event"}

ANYCUBIC_ERROR_MESSAGES = {
    10000: "Unknown error",
    10105: "File download failed",
    10106: "Insufficient memory available",
    10107: "Abnormal filament",
    10108: "Print scratch, check nozzle and hotbed distance",
    10113: "Failed to verify the MD5 checksum of the downloaded file",
    10115: "The device cannot parse the file",
    10116: "Abnormal slice file",
    10118: "X-axis homing failed",
    10119: "Y-axis homing failed",
    10120: "Z-axis homing failed",
    10121: "Hotbed heating abnormal",
    10122: "Extruder heating abnormal",
    10123: "Hotbed NTC abnormal",
    10124: "Extruder NTC abnormal",
    10125: "Nozzle MCU abnormal",
    10126: "X-axis belt detection abnormal",
    10127: "Y-axis vibration compensation abnormal",
    10128: "X-axis vibration compensation abnormal",
    10129: "Y-axis vibration compensation abnormal",
    10130: "Z-axis homing failed, check the Z-axis sensor and drive",
    10131: "Nozzle MCU abnormal",
    10133: "File lacks necessary commands",
    10134: "This feature requires access to the camera",
    10135: "This function requires a USB flash drive",
    10136: "This function requires access to the camera and USB flash drive",
    10402: "Network connection timed out",
    10403: "Filament broken, load a new filament into the ACE",
    10408: "Firmware version is too low to start the system",
    10409: "An issue occurred, restart the device manually",
    10410: "Heater temperature exceeds limit",
    10412: "An issue occurred, restart the device manually",
    10413: "An issue occurred, restart the device manually",
    10414: "A system error occurred, reboot the device manually",
    10536: "Filament Hub ID error",
    10539: "The nozzle must be heated first",
    10801: "Firmware download failed",
    10802: "Firmware update failed",
    10803: "Insufficient memory available",
    10804: "Failed to mount external storage",
    10805: "Firmware verification failed",
    11001: "Failed to delete local file",
    11101: "Wi-Fi connection failed",
    11402: "Network connection timed out",
    11407: "Device startup failed",
    11412: "MCU disconnected from host",
    11503: "Color engine connection problem",
    11504: "Unknown feed location for non-multi-color model",
    11509: "Color engine response timed out",
    11511: "Extrusion abnormal",
    11512: "Retraction abnormal",
    11513: "ACE Pro response timed out",
    11518: "Filament clogging detected",
    11519: "Filament tangle detected",
    11520: "ACE filament abnormal",
    11521: "Color engine motor rotation abnormal",
    11524: "Color engine connection problem",
    11529: "ACE NTC abnormal",
    11530: "ACE PTC abnormal",
    11531: "Model has too many colors to print",
    11532: "Device lacks automatic leveling",
    11533: "Device lacks leveling resonance",
    11801: "Spaghetti detected",
    11816: "Z-axis motor anomaly",
    11819: "Motor cable fault",
    11842: "Filament clogging or entanglement",
    11854: "Retraction failure",
    11900: "Model nozzle diameter is not compatible with the equipment nozzle",
}


def describe(code: int) -> str:
    return ANYCUBIC_ERROR_MESSAGES.get(code, f"Unknown error code {code}")


def error_from_report(kind: str, payload: dict[str, Any]) -> tuple[int, str] | None:
    """(code, message) when a report announces a printer error, else None."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    state = str(payload.get("state") or data.get("state") or "").lower()
    kind_l = kind.lower()
    if kind_l in EVENT_TYPES:
        raw = next((data.get(k) for k in ("code", "event_code", "eventCode", "printerEventCode") if data.get(k) is not None), payload.get("code"))
    elif kind_l in ("print", "axis") and (state == "failed" or payload.get("code") not in (None, *OK_CODES)):
        raw = payload.get("code")
    else:
        return None
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return None
    if code in OK_CODES:
        return None
    msg = describe(code) if code in ANYCUBIC_ERROR_MESSAGES else str(payload.get("msg") or describe(code))
    return code, msg
