"""Per-model differences between Anycubic printers that speak the same LAN protocol.

The handshake (HTTP 18910) and the MQTT session (TLS 9883) are shared by the Kobra 3 family,
the Kobra S1 family and the Kobra X; the model comes from the printer's `modelId`. What changes
between them is the motion layout and the numbering of the `axis/move` command.

Only the Kobra X is checked on hardware. The other entries come from public sources:
model ids from jbatonnet/Rinkhals; home codes (1 = Y, 2 = X, 4 = all) from
ianloic/anycubic-orca-plugin for the Kobra 3 / S1. They are a best guess until someone
captures them (tools/sniff.py) on that printer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    # axis/move ids for X, Y, Z
    axis_ids: dict[str, int]
    # axis/move id that homes every axis
    home_all: int
    # OrcaSlicer's "Bed" jog buttons assume the bed moves in Z ("bed up" = Z-). On printers
    # where Z moves the print head, the sign is flipped so the up arrow raises the head.
    z_moves_head: bool
    confirmed: bool

    @property
    def axis_sign(self) -> dict[str, int]:
        return {"X": 1, "Y": 1, "Z": -1 if self.z_moves_head else 1}


# Kobra X, checked on fw 2.0.2.2 (2026-11-15): X = 1, Y = 2, Z = 3, home all = 5; Z moves the head.
KOBRA_X = ModelProfile("Anycubic Kobra X", {"X": 1, "Y": 2, "Z": 3}, 5, z_moves_head=True, confirmed=True)

# Kobra 3 family: bed slinger, Z moves the gantry with the head, like the Kobra X.
_K3_AXES = {"X": 2, "Y": 1, "Z": 3}
# Kobra S1 family: CoreXY, the bed moves in Z (what OrcaSlicer's buttons expect).
_S1_AXES = {"X": 2, "Y": 1, "Z": 3}

PROFILES: dict[str, ModelProfile] = {
    "20030": KOBRA_X,
    "20024": ModelProfile("Anycubic Kobra 3", _K3_AXES, 4, z_moves_head=True, confirmed=False),
    "20026": ModelProfile("Anycubic Kobra 3 Max", _K3_AXES, 4, z_moves_head=True, confirmed=False),
    "20027": ModelProfile("Anycubic Kobra 3 V2", _K3_AXES, 4, z_moves_head=True, confirmed=False),
    "20025": ModelProfile("Anycubic Kobra S1", _S1_AXES, 4, z_moves_head=False, confirmed=False),
    "20029": ModelProfile("Anycubic Kobra S1 Max", _S1_AXES, 4, z_moves_head=False, confirmed=False),
}


def profile_for(model_id: str | None) -> ModelProfile:
    """Profile for a printer's `modelId`; unknown models get the Kobra X layout."""
    return PROFILES.get(str(model_id or ""), KOBRA_X)
