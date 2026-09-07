"""Wyliczanie urządzeń wejściowych (mikrofonów)."""

from __future__ import annotations

import sounddevice as sd


def list_input_devices() -> list[dict]:
    """Zwraca listę urządzeń wejściowych: index, nazwa, kanały, samplerate.

    Oznacza urządzenia Bluetooth (bt=True) — o niskiej jakości nagrania
    (macOS przełącza je w tryb HFP ~24 kHz i psuje też dźwięk systemowy).
    """
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None

    devices = []
    for idx, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        name = d["name"]
        bt = any(k in name.lower() for k in ("airpods", "bluetooth", "headset", "handsfree"))
        # Continuity devices (iPhone/iPad mic) show up in the device list but
        # are rarely what anyone actually wants as a fallback microphone.
        external = any(k in name.lower() for k in ("iphone", "ipad", "continuity"))
        devices.append(
            {
                "index": idx,
                "name": name,
                "channels": d["max_input_channels"],
                "samplerate": int(d["default_samplerate"]),
                "bt": bt,
                "external": external,
                "default": idx == default_in,
            }
        )
    return devices
