#!/usr/bin/env python3
"""
sigrok .sr → Logic 2 .sal converter.

.sr layout (libsigrok's srzip format):
  version          plain "2"
  metadata         GLib GKeyFile (INI). [device N] block lists
                   `total probes` (digital), `total analog`, `samplerate`,
                   `unitsize`, `probeM`/`analogM` channel names, and
                   `capturefile` (chunk filename prefix).
  logic-N-K        uint8[unitsize] per sample, bit i = digital channel i+1.
                   K = 1, 2, ... chunk index.
  analog-N-C-K     float32 little-endian per sample, voltage units.
                   C = global channel index (analog channels follow digital).

We collapse the per-chunk pieces into one stream per channel, then emit
.sal via the existing sigrok2sal builders.

Usage:
  ./sr2sal.py [-v MAX_VOLTS] [-d DEVICE] input.sr output.sal
"""
from __future__ import annotations

import argparse
import configparser
import json
import re
import struct
import sys
import zipfile
from pathlib import Path

import numpy as np

import sigrok2sal


SR_RATE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kMG]?)Hz\s*$")
_MULTIPLIER = {"": 1, "k": 1_000, "M": 1_000_000, "G": 1_000_000_000}


def parse_rate(s: str) -> int:
    m = SR_RATE.match(s)
    if not m:
        raise ValueError(f"unrecognized samplerate string: {s!r}")
    return int(float(m.group(1)) * _MULTIPLIER[m.group(2)])


def load_sr(path: Path) -> dict:
    """Open a .sr archive and return a dict of {name: bytes} for every entry."""
    with zipfile.ZipFile(path) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist()}


def parse_metadata(meta_bytes: bytes, device: int = 1) -> dict:
    cp = configparser.ConfigParser()
    cp.read_string(meta_bytes.decode("utf-8"))
    section = f"device {device}"
    if section not in cp:
        raise ValueError(f"no [{section}] in metadata")
    d = cp[section]
    info: dict = {
        "capturefile": d.get("capturefile", "logic-1"),
        "unitsize": int(d.get("unitsize", "1")),
        "samplerate": parse_rate(d["samplerate"]),
        "total_digital": int(d.get("total probes", "0") or 0),
        "total_analog": int(d.get("total analog", "0") or 0),
        "digital_channels": [],
        "analog_channels": [],
    }
    for k, v in d.items():
        m = re.match(r"^probe(\d+)$", k)
        if m:
            info["digital_channels"].append((int(m.group(1)), v))
        m = re.match(r"^analog(\d+)$", k)
        if m:
            info["analog_channels"].append((int(m.group(1)), v))
    info["digital_channels"].sort()
    info["analog_channels"].sort()
    return info


def _ordered_chunks(files: dict[str, bytes], prefix: str) -> bytes:
    """Concatenate all `<prefix>-<k>` chunks (k = 1, 2, ...) in order."""
    matches = []
    for name, data in files.items():
        if not name.startswith(prefix + "-"):
            continue
        try:
            k = int(name.rsplit("-", 1)[1])
        except ValueError:
            continue
        matches.append((k, data))
    matches.sort()
    return b"".join(data for _, data in matches)


def digital_bits_from_packed(packed: bytes, unitsize: int, bit_index: int,
                              total_samples: int) -> bytes:
    """Extract a single channel as 0/1 bytes from packed logic data."""
    if unitsize == 1:
        # Fast path: 1 byte per sample, channel is bit `bit_index` of each byte.
        arr = np.frombuffer(packed, dtype=np.uint8)[:total_samples]
        return ((arr >> bit_index) & 1).astype(np.uint8).tobytes()
    elif unitsize in (2, 4, 8):
        dt = {2: "<u2", 4: "<u4", 8: "<u8"}[unitsize]
        arr = np.frombuffer(packed, dtype=dt)[:total_samples]
        return ((arr >> bit_index) & 1).astype(np.uint8).tobytes()
    else:
        raise ValueError(f"unsupported unitsize {unitsize}")


def analog_voltages_to_int16(volts: np.ndarray, max_v: float) -> np.ndarray:
    """Map ±max_v volts → int16 in the same 10V/2047 grid Logic 2 uses.

    Logic 2's analog samples are int16 with a fixed scale where the
    full hardware range maps to ±2047. The 35-byte channel-specific
    header we replay from the Logic Pro 8 simulator encodes a ±10 V
    range, so to keep the same V/raw mapping we scale by 2047/10
    irrespective of the sigrok signal's true range. Inputs outside
    ±max_v just clip.
    """
    scale = 2047.0 / 10.0
    out = np.clip(volts * scale, -2047, 2047).round().astype(np.int16)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, help="input .sr file")
    p.add_argument("output", type=Path, help="output .sal file")
    p.add_argument("-d", "--device", type=int, default=1,
                   help="device index in [device N] metadata block (default 1)")
    args = p.parse_args()

    files = load_sr(args.input)
    if "metadata" not in files:
        print(f"{args.input}: no metadata in archive", file=sys.stderr)
        return 1
    info = parse_metadata(files["metadata"], device=args.device)

    capturefile = info["capturefile"]
    samplerate = info["samplerate"]
    unitsize = info["unitsize"]
    # Capture timestamp: use the .sr file's mtime as a stable proxy.
    capture_unix_ms = int(args.input.stat().st_mtime * 1000)

    # Total number of logic samples is determined by the size of all logic
    # chunks combined, divided by unitsize. (.sr can have multiple chunks
    # for huge captures; this concatenates them in order.)
    packed_logic = _ordered_chunks(files, capturefile)
    n_logic = len(packed_logic) // unitsize

    print(f"sample rate: {samplerate} Hz", file=sys.stderr)
    print(f"digital channels: {info['total_digital']}, "
          f"analog channels: {info['total_analog']}", file=sys.stderr)
    print(f"logic samples: {n_logic}", file=sys.stderr)

    # Build the per-file outputs.
    out_files: dict[str, bytes] = {}

    # --- digital ---
    n_digital_samples = n_logic
    for i, (probe_idx, name) in enumerate(info["digital_channels"]):
        # In .sr digital data, bit i (0-indexed from LSB) holds probe i+1.
        bit = probe_idx - 1
        bits = digital_bits_from_packed(packed_logic, unitsize, bit, n_logic)
        runs = sigrok2sal.runs_from_digital_bits(bits, samplerate)
        out_files[f"digital-{i}.bin"] = sigrok2sal.build_digital_bin(
            runs, sample_rate=samplerate, capture_unix_ms=capture_unix_ms)
        print(f"  digital ch{i} ({name}): {len(runs)} runs", file=sys.stderr)

    # --- analog ---
    # All analog channels share a sample rate (sigrok's session abstraction);
    # in libsigrok session files, analog samples are float32 volts at the
    # same rate as logic.
    n_analog_samples = 0
    for i, (probe_idx, name) in enumerate(info["analog_channels"]):
        chunk_prefix = f"analog-{args.device}-{probe_idx}"
        raw = _ordered_chunks(files, chunk_prefix)
        if not raw:
            print(f"  WARNING: no chunks for analog channel {probe_idx} ({name})",
                  file=sys.stderr)
            continue
        n = len(raw) // 4
        volts = np.frombuffer(raw, dtype="<f4")[:n]
        samples_i16 = analog_voltages_to_int16(volts, max_v=10.0)
        out_files[f"analog-{i}.bin"] = sigrok2sal.build_analog_bin(
            samples_i16.tobytes(), sample_rate=samplerate, capture_unix_ms=capture_unix_ms)
        n_analog_samples = max(n_analog_samples, n)
        print(f"  analog ch{i} ({name}): {n} samples, "
              f"raw range [{int(samples_i16.min())}, {int(samples_i16.max())}]",
              file=sys.stderr)

    # --- meta + trigger ---
    out_files["trigger-store.bin"] = sigrok2sal.TRIGGER_STORE_BIN
    meta = sigrok2sal.build_meta_json(
        digital_channels=list(range(len(info["digital_channels"]))),
        analog_channels=list(range(len(info["analog_channels"]))),
        digital_rate=samplerate,
        analog_rate=samplerate,
        n_digital_samples=n_digital_samples,
        n_analog_samples=n_analog_samples,
        capture_unix_ms=capture_unix_ms,
    )
    out_files["meta.json"] = json.dumps(meta).encode()

    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, data in out_files.items():
            zf.writestr(name, data)
    print(f"wrote {args.output} "
          f"({args.output.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
