import base64
import hashlib
import json
import types
import zipfile

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from kobrax_plugin import lan
from kobrax_plugin.job import (
    JobError,
    build_box_mapping,
    build_start_payload,
    prepare_gcode,
    safe_remote_name,
)
from kobrax_plugin.main import KobraX
from kobrax_plugin.status import PrinterState

# Shapes captured from a Kobra X on firmware 2.0.2.2 (identifiers removed).
INFO_IDLE = {
    "action": "report",
    "code": 200,
    "type": "info",
    "data": {
        "fan_speed_pct": 40,
        "project": None,
        "state": "free",
        "temp": {
            "curr_hotbed_temp": 27,
            "curr_nozzle_temp": 30,
            "target_hotbed_temp": 0,
            "target_nozzle_temp": 0,
        },
        "urls": {"fileUploadurl": "http://192.168.0.84:18910/gcode_upload?s=abc"},
        "version": "2.0.2.2",
    },
}
BOX = {
    "action": "getInfo",
    "code": 200,
    "type": "multiColorBox",
    "data": {
        "multi_color_box": [
            {
                "id": -1,
                "slots": [
                    {"index": 0, "status": 4, "type": "PLA", "color": [111, 111, 111]},
                    {"index": 1, "status": 5, "type": "PLA", "color": [255, 255, 255]},
                    {"index": 2, "status": 5, "type": "PETG", "color": [255, 106, 20]},
                    {"index": 3, "status": 4, "type": "PLA", "color": [111, 111, 111]},
                ],
            }
        ]
    },
}


def test_sign_matches_formula():
    token = "0123456789abcdefFEDCBA9876543210"
    p = lan.sign_params(token, ts="1700000000000", nonce="AbC123")
    inner = hashlib.md5(token[:16].encode()).hexdigest()
    assert p["sign"] == hashlib.md5((inner + "1700000000000AbC123").encode()).hexdigest()
    assert len(p["did"]) == 32 and p["did"] == p["did"].upper()


def test_decrypt_bundle_roundtrip():
    token = "0123456789abcdefFEDCBA9876543210"
    iv = "ivivivivivivivi1"
    plain = json.dumps({"deviceId": "d", "username": "u"}).encode()
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(iv.encode())).encryptor()
    blob = enc.update(padder.update(plain) + padder.finalize()) + enc.finalize()
    assert lan.decrypt_bundle(token, base64.b64encode(blob).decode(), iv)["deviceId"] == "d"


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.9:18910/gcode_upload?s=x",
        "https://192.168.0.84:18910/gcode_upload?s=x",
        "http://192.168.0.84:80/gcode_upload?s=x",
        "http://192.168.0.84:18910/other?s=x",
        "http://192.168.0.84:18910/gcode_upload",
    ],
)
def test_upload_url_rejected(url):
    with pytest.raises(lan.KobraError):
        lan.validate_upload_url(url, "192.168.0.84")


def test_idle_status():
    st = PrinterState()
    assert st.update("info", INFO_IDLE)
    p = st.push_status()["print"]
    assert p["gcode_state"] == "IDLE"
    assert p["nozzle_temper"] == 30 and p["bed_target_temper"] == 0
    assert p["fan_gear"] == 102
    assert st.is_idle and st.version == "2.0.2.2"
    assert st.upload_url.endswith("s=abc")


def test_printing_status_and_task_id():
    st = PrinterState()
    info = json.loads(json.dumps(INFO_IDLE))
    info["data"]["state"] = "busy"
    info["data"]["project"] = {
        "state": "printing",
        "progress": 42,
        "remain_time": 17,
        "curr_layer": 30,
        "total_layers": 120,
        "filename": "/local/cube.gcode",
        "taskid": 99,
    }
    st.update("info", info)
    p = st.push_status()["print"]
    assert p["gcode_state"] == "RUNNING" and p["mc_print_stage"] == 1
    assert p["mc_percent"] == 42 and p["mc_remaining_time"] == 17
    assert p["layer_num"] == 30 and p["total_layer_num"] == 120
    assert p["subtask_name"] == "cube.gcode"
    assert st.task_id == "99" and not st.is_idle


def test_ams_payload_marks_loaded_slots():
    st = PrinterState()
    st.update("multiColorBox", BOX)
    ams = st.ams_payload()
    assert ams["ams_exist_bits"] == "1"
    assert ams["tray_exist_bits"] == "6"  # slots 2 and 3 (0-based 1 and 2)
    trays = ams["ams"][0]["tray"]
    assert trays[1]["tray_color"] == "FFFFFFFF" and trays[1]["tray_type"] == "PLA"
    assert trays[0]["tray_type"] == ""


def _gcode(tmp_path, body):
    path = tmp_path / "Cube test (1).gcode"
    path.write_bytes(body)
    return path


def test_prepare_gcode_reads_filaments(tmp_path):
    path = _gcode(
        tmp_path,
        b"G28\nT0\nG1 X1\nT2\n; filament_type = PLA;PLA;PETG\n; filament_colour = #FF0000;#00FF00;#0000FF\n",
    )
    params = types.SimpleNamespace(dst_file="", filename=str(path), project_name="Cube test (1)")
    g = prepare_gcode(params)
    assert g.remote_name == "Cube_test_1.gcode"
    assert g.used_tools == [0, 2]
    assert g.filament_colors[2] == [0, 0, 255]
    assert g.md5 == hashlib.md5(path.read_bytes()).hexdigest()


def test_prepare_gcode_from_3mf(tmp_path):
    archive = tmp_path / "plate.gcode.3mf"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Metadata/plate_2.gcode", "T0\n; filament_type = PLA\n")
    params = types.SimpleNamespace(dst_file="", filename=str(archive), plate_index=2, project_name="p")
    g = prepare_gcode(params)
    try:
        assert g.temp_path and g.used_tools == [0]
    finally:
        g.cleanup()


def test_box_mapping_uses_orca_mapping(tmp_path):
    st = PrinterState()
    st.update("multiColorBox", BOX)
    path = _gcode(tmp_path, b"T0\nT1\n; filament_type = PLA;PETG\n; filament_colour = #FFFFFF;#FF6A14\n")
    g = prepare_gcode(types.SimpleNamespace(dst_file="", filename=str(path), project_name="x"))
    m = build_box_mapping(g, st.slots(), "[1, 2]")
    assert [(e["paint_index"], e["ams_index"]) for e in m] == [(0, 1), (1, 2)]
    assert m[1]["material_type"] == "PETG" and m[1]["ams_color"] == [255, 106, 20, 255]


def test_box_mapping_single_filament_fallback_picks_loaded_same_material(tmp_path):
    st = PrinterState()
    st.update("multiColorBox", BOX)
    path = _gcode(tmp_path, b"G1\n; filament_type = PETG\n")
    g = prepare_gcode(types.SimpleNamespace(dst_file="", filename=str(path), project_name="x"))
    assert build_box_mapping(g, st.slots(), "")[0]["ams_index"] == 2


def test_box_mapping_rejects_empty_slot(tmp_path):
    st = PrinterState()
    st.update("multiColorBox", BOX)
    path = _gcode(tmp_path, b"T0\n; filament_type = PLA\n")
    g = prepare_gcode(types.SimpleNamespace(dst_file="", filename=str(path), project_name="x"))
    with pytest.raises(JobError):
        build_box_mapping(g, st.slots(), "[0]")


def test_start_payload_shape(tmp_path):
    path = _gcode(tmp_path, b"T0\n")
    g = prepare_gcode(types.SimpleNamespace(dst_file="", filename=str(path), project_name="x"))
    params = types.SimpleNamespace(task_bed_leveling=True, task_flow_cali=False, task_vibration_cali=False)
    p = build_start_payload(g, [], params)
    assert p["url"] == "" and p["filetype"] == 1 and p["filename"] == "x.gcode"
    assert p["task_settings"]["auto_leveling"] == 1
    assert p["task_settings"]["flow_calibration"] == 0


def test_safe_remote_name():
    assert safe_remote_name("../a b/Ñandú.gcode.3mf") == "and.gcode"
    assert safe_remote_name("") == "orca_print.gcode"


def test_agent_answers_version_and_rejects_raw_gcode():
    agent = KobraX()
    sent = []
    agent.set_on_local_message_fn(lambda dev, msg: sent.append((dev, json.loads(msg))))
    agent._dev_id = "dev"
    agent._state.update("info", INFO_IDLE)
    assert agent.send_message_to_printer("dev", '{"info": {"command": "get_version"}}', 0, 0) == 0
    assert sent[-1][1]["info"]["module"][0]["sw_ver"] == "2.0.2.2"
    # not connected: G28 is never translated, the reply says failed
    rc = agent.send_message_to_printer("dev", '{"print": {"command": "gcode_line", "param": "G28"}}', 0, 0)
    assert rc == -7010 and sent[-1][1]["print"]["result"] == "failed"


# --------------------------------------------------------------- Device tab data/controls


class _FakeSession:
    connected = True

    def __init__(self):
        self.sent = []

    def publish(self, source, kind, action, data=None):
        self.sent.append((source, kind, action, data))
        return "id"

    def query(self, kind, action="query"):
        self.sent.append(("web", kind, action, None))


def _agent(project=None):
    agent = KobraX()
    agent._dev_id = "dev"
    info = json.loads(json.dumps(INFO_IDLE))
    info["data"].update(aux_fan_speed_pct=100, print_speed_mode=3)
    info["data"]["urls"]["rtspUrl"] = "rtsp://192.168.0.84:18088/live/tok"
    if project:
        info["data"].update(state="busy", project=project)
    agent._state.update("info", info)
    agent._session = _FakeSession()
    return agent


def test_status_fans_speed_light_camera_substage():
    st = _agent({"state": "preheating", "taskid": 5})._state
    st.update("light", {"data": {"lights": [{"type": 3, "status": 1, "brightness": 100}]}})
    p = st.push_status()["print"]
    assert p["fan_gear"] == 102 | (255 << 8)  # part 40 % | aux 100 % << 8
    assert p["spd_lvl"] == 3
    assert p["lights_report"] == [{"node": "chamber_light", "mode": "on"}]
    assert p["gcode_state"] == "PREPARE" and p["stg_cur"] == 2
    assert st.camera_url.startswith("rtsp://")


def test_ams_humidity_only_when_reported():
    st = PrinterState()
    box = json.loads(json.dumps(BOX))
    box["data"]["multi_color_box"].append(
        {"id": 0, "humidity": 38, "temp": 25, "drying_status": {"status": 1, "remain_time": 90},
         "slots": [{"index": 0, "status": 5, "type": "PLA", "color": [1, 2, 3]}]}
    )
    st.update("multiColorBox", box)
    units = st.ams_payload()["ams"]
    assert "humidity_raw" not in units[0]  # onboard box reports 0
    assert units[1]["humidity_raw"] == "38" and units[1]["dry_time"] == 90
    assert st.ams_payload()["tray_exist_bits"] == "16"  # bits 1, 2 and 4


def test_light_speed_fan_translation():
    a = _agent()
    s = a._session
    assert a.send_message_to_printer("dev", '{"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "on"}}', 0, 0) == 0
    assert s.sent[0] == ("web", "light", "control", {"type": 3, "status": 1, "brightness": 100})
    # Orca 1 = silent -> Anycubic 2; Orca 4 (ludicrous) has no equivalent
    assert a.send_message_to_printer("dev", '{"print": {"command": "print_speed", "param": "1"}}', 0, 0) == 0
    assert s.sent[-1][2] == "update" and s.sent[-1][3]["settings"] == {"print_speed_mode": 2}
    assert a.send_message_to_printer("dev", '{"print": {"command": "print_speed", "param": "4"}}', 0, 0) == -7010
    assert a.send_message_to_printer("dev", '{"print": {"command": "gcode_line", "param": "M106 P1 S255"}}', 0, 0) == 0
    assert s.sent[-1] == ("web", "fan", "setSpeed", {"fan_speed_pct": 100})
    assert a.send_message_to_printer("dev", '{"print": {"command": "gcode_line", "param": "M106 P2 S255"}}', 0, 0) == 0
    assert s.sent[-1] == ("web", "print", "update", {"taskid": "-1", "settings": {"aux_fan_speed_pct": 100}})
    assert a.send_message_to_printer("dev", '{"print": {"command": "set_fan", "fan_index": 1, "speed": 50}}', 0, 0) == 0
    assert s.sent[-1][3] == {"fan_speed_pct": 50}


def test_axis_and_home_translation_and_blocked_while_printing():
    a = _agent()
    assert a.command_axis_control("dev", "Z", 10.0, -1, 900, True, False, 0, True) == 0
    # Orca "bed up" (Z-) must raise the Kobra X head: sent as positive Z
    assert a._session.sent[-1] == ("web", "axis", "move", {"axis": 3, "move_type": 1, "distance": 10})
    assert a.command_axis_control("dev", "E", 10.0, 1, 900, True, False, 0, True) == -7010
    assert a.command_go_home("dev", False, False, 0, True) == 0
    assert a._session.sent[-1][3] == {"axis": 5, "move_type": 2, "distance": 0}
    busy = _agent({"state": "printing", "taskid": 7})
    assert busy.command_go_home("dev", False, False, 0, True) == -7010
    assert busy.command_axis_control("dev", "X", 1.0, 1, 900, True, False, 0, True) == -7010
    assert busy._session.sent == []


def test_home_marks_axes_homed():
    a = _agent()
    assert a._state.push_status()["print"]["home_flag"] == 256
    assert a.command_go_home("dev", False, False, 0, True) == 0
    assert a._state.push_status()["print"]["home_flag"] == 256 | 7


def test_send_dialog_flags_from_features():
    st = PrinterState()
    info = json.loads(json.dumps(INFO_IDLE))
    info["data"]["features"] = {
        "auto_leveling_support": True,
        "flow_calibration_support": True,
        "camera_timelapse_support": False,
    }
    st.update("info", info)
    p = st.push_status()["print"]
    assert p["support_bed_leveling"] == 1
    assert p["support_flow_calibration"] is True and p["support_timelapse"] is False


def test_trays_carry_orca_filament_ids():
    from kobrax_plugin.status import family_of

    assert family_of("PLA High Speed") == "PLA High Speed"
    assert family_of("pla+") == "PLA" and family_of("PETG Translucent") == "PETG"
    assert family_of("Wood") == ""
    st = PrinterState()
    st.filament_ids = {"PLA": "OFDSrzZ8"}
    st.update("multiColorBox", BOX)
    trays = st.ams_payload()["ams"][0]["tray"]
    assert trays[1]["tray_info_idx"] == "OFDSrzZ8" and "tray_slot_placeholder" not in trays[1]
    assert trays[2]["tray_info_idx"] == "" and trays[2]["tray_slot_placeholder"] == "1"  # PETG unresolved


def test_errors_are_detected_and_deduplicated():
    from kobrax_plugin.errors import error_from_report

    assert error_from_report("printerEvent", {"data": {"code": 10121}}) == (10121, "Hotbed heating abnormal")
    assert error_from_report("print", {"code": 200, "state": "printing"}) is None
    assert error_from_report("info", {"code": 500}) is None
    a = _agent()
    seen = []
    a._notify_error = lambda code, msg: seen.append(code)
    for _ in range(2):
        a._on_report("anycubic/x/printerEvent/report", {"type": "printerEvent", "data": {"code": 11518}})
    assert seen == [11518]


def test_orca_model_dir_matches_orca(monkeypatch):
    import os

    from kobrax_plugin.main import orca_model_dir

    for v in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        monkeypatch.delenv(v, raising=False)
    assert orca_model_dir() == f"/tmp/orcaslicer_{os.getuid()}/orcaslicer_model"
    monkeypatch.setenv("TMPDIR", "/var/tmp/")
    assert orca_model_dir() == f"/var/tmp/orcaslicer_{os.getuid()}/orcaslicer_model"


def test_slot_edit_and_load_unload():
    a = _agent()
    a._state.update("multiColorBox", BOX)
    s = a._session
    req = {"print": {"command": "ams_filament_setting", "ams_id": 0, "slot_id": 2, "tray_type": "PETG", "tray_color": "FF6A14FF"}}
    assert a.send_message_to_printer("dev", json.dumps(req), 0, 0) == 0
    assert ("web", "multiColorBox", "setInfo", {"box_id": -1, "index": 2, "type": "PETG", "color": [255, 106, 20]}) in s.sent
    load = {"print": {"command": "ams_change_filament", "ams_id": 0, "slot_id": 1, "target": 1}}
    assert a.send_message_to_printer("dev", json.dumps(load), 0, 0) == 0
    assert s.sent[-1][:3] == ("slicer", "multiColorBox", "feedFilament")
    assert s.sent[-1][3]["multi_color_box"][0]["feed_status"] == {"slot_index": 1, "type": 1}
    # unload with nothing loaded (loaded_slot missing) -> no command
    n = len(s.sent)
    unload = {"print": {"command": "ams_change_filament", "ams_id": 0, "slot_id": 255, "target": 255}}
    assert a.send_message_to_printer("dev", json.dumps(unload), 0, 0) == 0 and len(s.sent) == n
    a._state.boxes[0]["loaded_slot"] = 1
    assert a.send_message_to_printer("dev", json.dumps(unload), 0, 0) == 0
    assert s.sent[-1][3]["multi_color_box"][0]["feed_status"] == {"slot_index": 1, "type": 2}


def test_speed_mode_mapping_and_camera_block():
    st = PrinterState()
    info = json.loads(json.dumps(INFO_IDLE))
    info["data"]["print_speed_mode"] = 1  # Anycubic standard
    info["data"]["urls"]["rtspUrl"] = "http://192.168.0.84:18088/flv"
    st.update("info", info)
    st.nozzle_diameter = "0.4"
    p = st.push_status()["print"]
    assert p["spd_lvl"] == 2
    assert p["ipcam"]["ipcam_dev"] == "1" and p["ipcam"]["liveview"]["local"] == "http"
    assert p["nozzle_diameter"] == "0.4" and p["support_aux_fan"] is True



def test_page_snapshot_and_actions():
    a = _agent({"state": "printing", "progress": 40, "curr_layer": 20, "total_layers": 173, "print_time": 14,
                "remain_time": 41, "filename": "/local/Snoopy.gcode", "taskid": -1})
    a._state.update("multiColorBox", BOX)
    a._state.update("print", {"type": "print", "action": "getSliceParam", "code": 200, "data": {
        "file_key": "k1", "thumbnail": "iVBOR", "slice_param": {"layer_height": 0.16, "fill_density": 12.0,
        "paint_infos": [{"paint_index": 0, "material_type": "PLA", "filament_used": 12.08}]}}})
    s = a.page_snapshot()
    assert s["connected"] and s["state"] == "RUNNING" and s["filename"] == "Snoopy.gcode"
    assert s["job"]["Altura de capa"] == "0.16 mm" and s["job"]["Filamento 1"] == "PLA · 12.1 m"
    assert s["thumb_key"] == "k1" and s["speed"] == 3 and len(s["slots"]) == 4
    assert a._state.project["progress"] == 40  # getSliceParam did not overwrite the live job
    assert a.page_action({"action": "thumbnail"})["thumbnail"] == "iVBOR"
    assert a.page_action({"action": "speed", "level": 2})["ok"]
    assert a._session.sent[-1][3]["settings"] == {"print_speed_mode": 1}
    assert a.page_action({"action": "files", "root": "udisk"})["ok"]
    assert a._session.sent[-1][:3] == ("web", "file", "listUdisk")
    assert not a.page_action({"action": "nope"})["ok"]


def test_local_webui_serves_page_and_requires_header():
    import urllib.error
    import urllib.request

    from kobrax_plugin.page import LocalWebUI

    calls = []
    ui = LocalWebUI(lambda: {"connected": True}, lambda m: calls.append(m) or {"type": "result", "ok": True}, port=0)
    assert ui.start()
    try:
        base = ui.url
        assert b"Kobra X" in urllib.request.urlopen(base).read()
        assert json.loads(urllib.request.urlopen(base + "api/state").read())["data"]["connected"]
        body = json.dumps({"type": "action", "action": "light", "on": True}).encode()
        with pytest.raises(urllib.error.HTTPError) as err:  # no X-Kobrax header: refused
            urllib.request.urlopen(urllib.request.Request(base + "api/action", data=body))
        assert err.value.code == 403 and calls == []
        req = urllib.request.Request(base + "api/action", data=body, headers={"X-Kobrax": "1", "Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"] and calls[0]["action"] == "light"
        bad_host = urllib.request.Request(base + "api/state", headers={"Host": "evil.example:80"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad_host)
    finally:
        ui.stop()



def test_audit_denial_detection_and_preflight(monkeypatch):
    import socket as socket_mod

    from kobrax_plugin import main as m

    wrapped = RuntimeError("HTTPConnectionPool: Failed to establish a new connection")
    wrapped.__cause__ = RuntimeError('Plugin attempted audited operation "socket.getaddrinfo" without permission')
    assert m._is_audit_denial(wrapped)
    assert m._is_audit_denial(PermissionError("x"))
    assert not m._is_audit_denial(TimeoutError("timed out"))
    calls = []
    monkeypatch.setattr(socket_mod, "getaddrinfo", lambda *a, **k: calls.append((a, k)) or [])
    m._preflight_network_permission("192.168.0.84")
    assert calls[0][0][0] == "192.168.0.84" and calls[0][1]["flags"] == socket_mod.AI_NUMERICHOST
