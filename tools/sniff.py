"""Record every MQTT message on a Kobra X's local broker, to discover commands.

The broker lets any authenticated client subscribe to `#`, so commands sent by other clients
(Anycubic Slicer Next in LAN mode, this plugin, ...) are visible next to the printer's reports.
Only listens: it never publishes.

    uv run python tools/sniff.py 192.168.0.84 [-o capture.jsonl] [--commands-only]

Credentials, tokens and URLs with tokens are redacted before anything is printed or saved,
and the device id is replaced by <dev>.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time
from collections import Counter
from typing import Any

from kobrax_plugin import lan

SECRET_KEYS = {
    "token", "password", "username", "devicecrt", "devicepk", "fileUploadurl", "rtspUrl",
    "url", "ip", "mac", "sn", "cn", "deviceId", "printer_id", "did", "sign",
}


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: "<redacted>" if k in SECRET_KEYS and obj[k] not in (None, "") else redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str) and re.search(r"[?&](s|token|sign)=|/live/", obj):
        return "<redacted-url>"
    return obj


class Sniffer(lan.KobraMqtt):
    def _handle_connect(self, client, _userdata, _flags, reason_code, _props) -> None:
        if reason_code.is_failure:
            self._failed.set()
            return
        client.subscribe("#")
        self._connected.set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("-o", "--output", help="append messages as JSON lines to this file")
    ap.add_argument("--commands-only", action="store_true", help="only web/slicer command topics")
    args = ap.parse_args()

    _info, creds = lan.handshake(args.host)
    seen: Counter[str] = Counter()
    out = open(args.output, "a", encoding="utf-8") if args.output else None
    lock = threading.Lock()

    def on_report(topic: str, payload: dict[str, Any]) -> None:
        topic = topic.replace(creds.device_id, "<dev>")
        is_command = "/web/printer/" in topic or "/slicer/printer/" in topic
        if args.commands_only and not is_command:
            return
        key = f"{'CMD ' if is_command else 'RPT '}{payload.get('type')}/{payload.get('action')}"
        record = {"t": time.strftime("%H:%M:%S"), "topic": topic, "payload": redact(payload)}
        with lock:
            seen[key] += 1
            first = seen[key] == 1
            if out:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
            # New message kinds in full, repeats as a one-line counter
            if first:
                print(f"\n[{record['t']}] NEW {key}  {topic}\n{json.dumps(record['payload'], ensure_ascii=False)[:2000]}")
            else:
                print(f"\r{key} x{seen[key]}      ", end="", flush=True)

    sniffer = Sniffer(creds, on_report, lambda up: None)
    sniffer.connect()
    print(f"Escuchando {args.host} ({creds.model_name}). Ctrl+C para terminar.", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    sniffer.close()
    if out:
        out.close()
    print("\n\nResumen:")
    for key, n in sorted(seen.items()):
        print(f"  {n:6d}  {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
