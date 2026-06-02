#!/usr/bin/env python3
"""
sigrok → Logic 2 .sal converter (work in progress).

Currently:
  - Container: ✔ ZIP, meta.json, per-channel .bin, trigger-store.bin
  - Digital .bin: ✔ FULLY supported (RLE encoding reverse-engineered
    from libgraph_server_shared.so Saleae::Digital::AppendRle)
  - Analog .bin: 🟡 raw samples (int16) written, pyramid layout still
    partially unknown — produced files may or may not load depending
    on how strict Logic 2's loader is.

Usage:
  ./sigrok2sal.py --sample-rate 125000000 --digital ch0.bin --digital ch1.bin out.sal
"""
import argparse
import io
import json
import struct
import sys
import zipfile
from pathlib import Path


# File-level header that follows the universal `<SALEAE>` + version + type.
# All fields fully decoded against two reference captures:
#
#   0x10       u8   valid_flag                (= 1)
#   0x11..0x18 f64  sample_rate (Hz)
#   0x19..0x20 u64  capture_start_unix_ms     (must match
#                                              meta.captureStartTime
#                                              .unixTimeMilliseconds)
#   0x21..0x28 f64  channel_time_offset_ms    (digital channels: equals
#                                              meta.captureStartTime
#                                              .fractionalMilliseconds;
#                                              analog channels: same minus
#                                              (pre_trigger_samples *
#                                               sample_period_ms). 0.0 is
#                                              valid for sigrok-sourced
#                                              captures with no trigger.)
#   0x29       u8   has_field_a (= 0, no u64 follows)
#   0x2A       u8   has_field_b (= 0, no u64 follows)
#   0x2B..0x32 u64  chunk_count               (must match chunks emitted)


def _channel_header(
    sample_rate: float,
    chunk_count: int,
    capture_unix_ms: int = 0,
    channel_time_offset_ms: float = 0.0,
) -> bytes:
    out = bytearray()
    out += bytes([1])
    out += struct.pack("<d", float(sample_rate))
    out += struct.pack("<Q", capture_unix_ms)
    out += struct.pack("<d", channel_time_offset_ms)
    out += bytes([0, 0])
    out += struct.pack("<Q", chunk_count)
    assert len(out) == 35
    return bytes(out)

MAGIC = b"<SALEAE>"
TYPE_DIGITAL = 100
TYPE_ANALOG = 101
TYPE_TRIGGER = 103
VERSION = 3

TRIGGER_STORE_BIN = (
    MAGIC + struct.pack("<I", VERSION) + struct.pack("<I", TYPE_TRIGGER) +
    struct.pack("<I", 1) + struct.pack("<I", 0) + struct.pack("<Q", 0)
)
assert len(TRIGGER_STORE_BIN) == 32


def encode_run(n: int) -> bytes:
    """Encode a run-length using Saleae::Digital::AppendRle's varint.

    Stores `n - 1`. Single byte for n-1 < 0x40, otherwise:
      first byte:    01xxxxxx  (top 2 bits = 01)   six MSBs of (n-1)
      middle bytes:  1xxxxxxx                       7 more bits each
      last byte:     0xxxxxxx                       low 7 bits
    """
    if n < 1:
        raise ValueError(f"run length must be >= 1, got {n}")
    v = n - 1
    if v < 0x40:
        return bytes([v])
    # Determine the number of 7-bit chunks needed (excluding the top
    # 6-bit chunk in the first byte). We start with 1 byte and shift a
    # sentinel mask up by 7 bits each iteration until no high bits of v
    # poke through.
    mask = (0xFFFFFFFFFFFFFFC0).to_bytes(8, "little", signed=False)
    sentinel = int.from_bytes(mask, "little", signed=False)
    nbytes = 1
    shift = 0
    while True:
        sentinel = (sentinel << 7) & ((1 << 64) - 1)
        nbytes += 1
        shift += 7
        if sentinel & v == 0:
            break
    out = bytearray()
    # First byte: top bits = 01, low 6 bits = v >> shift
    out.append(0x40 | ((v >> shift) & 0x3F))
    # Middle/last bytes
    for i in range(nbytes - 1):
        shift -= 7
        b = (v >> shift) & 0x7F
        if i < nbytes - 2:
            b |= 0x80   # continuation
        out.append(b)
    return bytes(out)


def encode_digital_chunk(start_sample: int, runs: list[int], is_final: bool) -> bytes:
    """One chunk = 26-byte header + RLE varint stream."""
    varints = b"".join(encode_run(n) for n in runs)
    end_sample = start_sample + sum(runs)
    third = (1 if is_final else 0) | (len(varints) << 16)
    hdr = struct.pack("<3Q", start_sample, end_sample, third) + struct.pack("<H", 0)
    return hdr + varints


def build_digital_bin(
    runs: list[int],
    sample_rate: float,
    capture_unix_ms: int = 0,
) -> bytes:
    """Render a complete digital-N.bin from a single list of runs."""
    out = bytearray()
    out += MAGIC
    out += struct.pack("<I", VERSION)
    out += struct.pack("<I", TYPE_DIGITAL)
    out += _channel_header(
        sample_rate,
        chunk_count=1,
        capture_unix_ms=capture_unix_ms,
        channel_time_offset_ms=0.0,
    )
    out += encode_digital_chunk(0, runs, is_final=True)
    return bytes(out)


def _build_pyramid(samples: list[int]) -> bytes:
    """Build the multi-level (min, max) decimation pyramid for one chunk.

    Pyramid format (reverse-engineered from libgraph_server_shared.so):

      uint32  pair_count_L1     (= ceil(N / 16))
      uint32  size_field        (= 848 + raw_bytes + 4 * sum_of_all_level_pair_counts;
                                 looks like an in-memory total bytes counter)
      uint32  unknown2  = 0
      uint32  level_count       (number of pyramid levels written)
      uint32  unknown3  = 0
      uint32  pair_count_L1     (= field 0, repeated)
      uint32  unknown4  = 0
      L1 data: pair_count_L1 (min, max) int16 pairs
      for each L2..LN:
        uint64  pair_count_Li
        Li data: pair_count_Li (min, max) int16 pairs

    L1 pairs are (min, max) over groups of K=16 consecutive raw samples.
    L_(k+1)[i] = (min(L_k[2i].min, L_k[2i+1].min),
                  max(L_k[2i].max, L_k[2i+1].max))
    Subsequent levels merge pairwise from the previous level.

    Termination:
      - If pair_count_L1 > 1: write levels until count reaches 1; that's
        the last level (no empty terminator follows).
      - If pair_count_L1 == 1: write a single empty L2 with u64=0 as a
        terminator after L1. level_count = 2.
    """
    n = len(samples)
    K = 16

    # Build L1
    l1 = []
    for i in range(0, n, K):
        grp = samples[i:i+K]
        l1.append((min(grp), max(grp)))
    levels = [l1]
    while len(levels[-1]) > 1:
        prev = levels[-1]
        nxt = [(min(prev[2*i][0], prev[2*i+1][0]),
                max(prev[2*i][1], prev[2*i+1][1]))
               for i in range(len(prev) // 2)]
        levels.append(nxt)

    n_levels = len(levels)
    # Pair count sum across all populated levels (used for the size_field).
    sum_pairs = sum(len(lv) for lv in levels)

    # If L1 had only 1 pair, append an empty terminator level (pair_count=0,
    # no data) — matches reference behavior for tiny chunks.
    appended_empty = False
    if len(l1) == 1:
        n_levels = 2
        appended_empty = True

    raw_bytes = n * 2
    size_field = 848 + raw_bytes + 4 * sum_pairs

    out = bytearray()
    out += struct.pack("<7I", len(l1), size_field, 0, n_levels, 0, len(l1), 0)
    # L1 (no u64 header before it)
    for mn, mx in levels[0]:
        out += struct.pack("<hh", mn, mx)
    # Subsequent populated levels each preceded by their u64 pair count
    for lvl in levels[1:]:
        out += struct.pack("<Q", len(lvl))
        for mn, mx in lvl:
            out += struct.pack("<hh", mn, mx)
    # Empty terminator for L1=1 case
    if appended_empty:
        out += struct.pack("<Q", 0)
    return bytes(out)


def volts_to_int16(volts, voltage_range: tuple[float, float]):
    """Map an array of voltages to the int16 grid the .sal format uses.

    Logic 2 stores analog samples as int16 in [-2047, +2047]; the
    voltage interpretation comes from meta.json's
    `legacyDeviceCalibration.fullScaleVoltageRanges[ch]` (or, if absent,
    a device default — ±10 V for the Logic Pro 8 simulator profile we
    use as a meta template).

      raw = clip( (V - midpoint) / half_range * 2047, -2047, 2047 )
      midpoint   = (max_v + min_v) / 2
      half_range = (max_v - min_v) / 2

    `volts` may be a numpy array or any iterable of floats; returns a
    numpy int16 array.
    """
    import numpy as np
    min_v, max_v = voltage_range
    if max_v <= min_v:
        raise ValueError(f"voltage_range must have max > min, got {voltage_range}")
    midpoint = (max_v + min_v) / 2.0
    half = (max_v - min_v) / 2.0
    arr = np.asarray(volts, dtype=np.float64)
    scaled = (arr - midpoint) / half * 2047.0
    return np.clip(scaled, -2047, 2047).round().astype(np.int16)


def build_analog_bin(
    samples: bytes,
    sample_rate: float,
    samples_per_chunk: int = 417040,
    capture_unix_ms: int = 0,
) -> bytes:
    """Render a complete analog-N.bin from raw int16 LE samples.

    The reference file uses 39 chunks of ~417040 samples each. We
    follow that template but allow any chunk size via samples_per_chunk.
    """
    assert len(samples) % 2 == 0
    n_total = len(samples) // 2
    samples_list = list(struct.unpack(f"<{n_total}h", samples))

    # Pre-compute how many chunks we'll emit so we can write the right
    # chunk_count in the file header.
    n_chunks = (n_total + samples_per_chunk - 1) // samples_per_chunk or 1

    out = bytearray()
    out += MAGIC
    out += struct.pack("<I", VERSION)
    out += struct.pack("<I", TYPE_ANALOG)
    out += _channel_header(
        sample_rate,
        chunk_count=n_chunks,
        capture_unix_ms=capture_unix_ms,
        channel_time_offset_ms=0.0,
    )

    start = 0
    while start < n_total:
        n_in_chunk = min(samples_per_chunk, n_total - start)
        end = start + n_in_chunk
        out += struct.pack("<3Q", start, end, n_in_chunk)
        out += samples[start * 2 : end * 2]
        out += _build_pyramid(samples_list[start:end])
        start = end
    return bytes(out)


def runs_from_digital_bits(bits, sample_rate: int) -> list[int]:
    """Given a sequence of 0/1 bit values (one per sample), produce RLE runs."""
    if not bits:
        return []
    runs = []
    cur = bits[0]
    cnt = 0
    for b in bits:
        if b == cur:
            cnt += 1
        else:
            runs.append(cnt)
            cur = b
            cnt = 1
    runs.append(cnt)
    return runs


import copy

# A known-good meta.json structure recorded from a Logic 2.4.44 capture
# made against the built-in Logic Pro 8 simulator. We use it as a
# template and only patch the fields that vary per converted file:
# the enabled channels, sample rates, capture duration, binData, and
# rowsSettings. Everything else (analyzerTrigger, dataTable columns,
# digitalTriggerTime, etc.) is replayed verbatim so the meta passes
# Logic 2's schema validator.
_META_TEMPLATE = json.loads("""
{
  "version": 22,
  "data": {
    "renderViewState": {
      "type": "PanAndZoom",
      "leftEdgeTimeSec": 0,
      "timeScaleSeconds": 1
    },
    "captureStartTime": {
      "unixTimeMilliseconds": 1780395682876,
      "fractionalMilliseconds": 0.046
    },
    "timingMarkers": {"markers": {}, "pairs": {}},
    "measurements": [],
    "highLevelAnalyzers": [],
    "analyzers": [],
    "rowsSettings": [],
    "legacyDevice": {
      "deviceId": "1000004",
      "name": "Logic Pro 8",
      "deviceType": "LogicPro8",
      "isSimulation": true,
      "capabilities": {
        "channelCapabilities": [],
        "sampleRateOptions": [],
        "digitalThresholdOptions": [
          {"description": "1.2 Volts"},
          {"description": "1.8 Volts"},
          {"description": "3.3+ Volts"}
        ],
        "isPhysicalDevice": false
      }
    },
    "legacySettings": {
      "enabledChannels": [],
      "sampleRate": {"digital": 500000000, "analog": 50000000},
      "digitalThreshold": {"description": "1.2 Volts"},
      "glitchFilter": {"enabled": false, "channels": []}
    },
    "captureSettings": {
      "bufferSizeMb": 3072,
      "timerModeSettings": {"stopAfterSeconds": 1},
      "commonCaptureSettings": {
        "trimAfterCapture": false,
        "trimTimeSeconds": 0.001
      },
      "triggerSettings": {
        "eventChannel": {"category": "legacy", "type": "Digital", "deviceChannel": 0},
        "triggerSourceGeneration": 0,
        "scopeEventType": "Rising",
        "scopeThreshold": 1,
        "scopeHysteresisPercentage": 0.02,
        "digitalEventType": "Rising",
        "digitalLinkedChannels": [],
        "digitalLegacyPostTriggerBufferSeconds": 1,
        "mode": "Auto",
        "holdOffSeconds": 0.001,
        "pulseDuration": {"min": 0.001, "max": 0.01},
        "realTriggerTimeoutViewRatio": 4,
        "minRealTriggerTimeoutSeconds": 1,
        "autoTriggerTimeoutViewRatio": 2
      },
      "captureMode": "FreeRun",
      "captureTriggerType": "Signal"
    },
    "timeManager": {"t0": {"type": "startOfCapture"}},
    "captureNotes": "",
    "dataTable": {
      "columns": {
        "analyzerIdentifier": {"baseKey": "analyzerIdentifier", "excludeFromSearch": true, "isActive": true, "isDefault": true, "width": 18},
        "frameType":          {"baseKey": "frameType",          "excludeFromSearch": false, "isActive": true, "isDefault": true, "width": 75},
        "start":              {"baseKey": "start",              "excludeFromSearch": true, "isActive": true, "isDefault": true, "width": 110},
        "duration":           {"baseKey": "duration",           "excludeFromSearch": true, "isActive": true, "isDefault": true, "width": 80}
      }
    },
    "digitalTriggerTime": -1,
    "analyzerTrigger": {"settings": {"holdoffSeconds": 0.2, "searchQuery": ""}},
    "name": "Generated by sigrok2sal"
  },
  "binData": []
}
""")

# Fill out the capabilities.channelCapabilities array (LogicPro8 has 8 of each).
for _i in range(8):
    for _t in ("Digital", "Analog"):
        _META_TEMPLATE["data"]["legacyDevice"]["capabilities"]["channelCapabilities"].append(
            {"type": _t, "index": _i, "capability": "Toggleable"}
        )


def build_meta_json(
    *,
    digital_channels: list[int],
    analog_channels: list[int],
    digital_rate: int,
    analog_rate: int,
    n_digital_samples: int,
    n_analog_samples: int,
    capture_unix_ms: int = 0,
    analog_voltage_ranges: list[tuple[float, float]] | None = None,
) -> dict:
    """Build a meta.json dict.

    analog_voltage_ranges, if given, must be one (min_v, max_v) tuple per
    *analog* channel in `analog_channels` order. It populates the
    `legacyDeviceCalibration.fullScaleVoltageRanges` block so Logic 2
    maps int16 raw values to those voltage limits. If omitted, the
    Logic Pro 8 simulator default (±10 V) applies.
    """
    meta = copy.deepcopy(_META_TEMPLATE)
    d = meta["data"]
    d["captureStartTime"] = {
        "unixTimeMilliseconds": int(capture_unix_ms),
        "fractionalMilliseconds": 0.0,
    }
    if analog_voltage_ranges is not None:
        if len(analog_voltage_ranges) != len(analog_channels):
            raise ValueError(
                f"analog_voltage_ranges length ({len(analog_voltage_ranges)}) "
                f"must match analog_channels length ({len(analog_channels)})")
        # The deserializer indexes fullScaleVoltageRanges by the global
        # analog channel index (0..N-1 where N = total analog slots on
        # the device), not by enabled-channel position. So we must emit
        # one entry per device slot. The Logic Pro 8 profile used by
        # our template has 8 analog slots — channels we don't enable get
        # a benign default ±10 V range to keep the deserializer happy.
        n_slots = sum(1 for c in d["legacyDevice"]["capabilities"]["channelCapabilities"]
                      if c["type"] == "Analog")
        ranges_by_idx: dict[int, tuple[float, float]] = dict(zip(analog_channels, analog_voltage_ranges))
        d["legacyDeviceCalibration"] = {
            "fullScaleVoltageRanges": [
                {
                    "minimumVoltage": float(ranges_by_idx.get(i, (-10.0, 10.0))[0]),
                    "maximumVoltage": float(ranges_by_idx.get(i, (-10.0, 10.0))[1]),
                }
                for i in range(n_slots)
            ]
        }

    enabled = []
    rows = []
    bin_data = []

    for ch in digital_channels:
        enabled.append({"type": "Digital", "index": ch})
        rows.append({
            "id": f"00000000-0000-0000-0000-d00000000{ch:03d}",
            "height": 45,
            "isMarkedHidden": False,
            "type": "channel",
            "name": f"Channel {ch}",
            "channel": {"category": "legacy", "type": "Digital", "deviceChannel": ch},
        })
        bin_data.append({"category": "legacy", "type": "Digital", "deviceChannel": ch,
                         "file": f"./digital-{ch}.bin"})
    for ch in analog_channels:
        enabled.append({"type": "Analog", "index": ch})
        rows.append({
            "id": f"00000000-0000-0000-0000-a00000000{ch:03d}",
            "height": 100,
            "isMarkedHidden": False,
            "type": "channel",
            "name": f"Channel {ch}",
            "channel": {"category": "legacy", "type": "Analog", "deviceChannel": ch},
        })
        bin_data.append({"category": "legacy", "type": "Analog", "deviceChannel": ch,
                         "file": f"./analog-{ch}.bin"})

    d["legacySettings"]["enabledChannels"] = enabled
    d["legacySettings"]["sampleRate"] = {"digital": digital_rate, "analog": analog_rate}
    d["legacyDevice"]["capabilities"]["sampleRateOptions"] = [
        {"digital": digital_rate, "analog": analog_rate}
    ]
    d["rowsSettings"] = rows
    meta["binData"] = bin_data

    duration_seconds = max(
        n_digital_samples / digital_rate if digital_rate else 0,
        n_analog_samples / analog_rate if analog_rate else 0,
    )
    d["captureSettings"]["timerModeSettings"]["stopAfterSeconds"] = duration_seconds
    d["renderViewState"]["timeScaleSeconds"] = max(duration_seconds, 1e-3)
    return meta


def write_sal(out_path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample-rate", type=int, required=True,
                   help="Sample rate (Hz) for digital channels.")
    p.add_argument("--analog-rate", type=int, default=None,
                   help="Sample rate (Hz) for analog channels (default: same as --sample-rate / 10).")
    p.add_argument("--digital", action="append", default=[], metavar="FILE",
                   help="Raw digital channel file (1 byte per sample, 0 or 1). May be repeated.")
    p.add_argument("--analog", action="append", default=[], metavar="FILE",
                   help="Raw analog channel file (little-endian int16 samples). May be repeated.")
    p.add_argument("out", type=Path, help="Output .sal path")
    args = p.parse_args()

    digital_rate = args.sample_rate
    analog_rate = args.analog_rate if args.analog_rate else max(1, digital_rate // 10)

    files: dict[str, bytes] = {}
    n_digital_samples = 0
    n_analog_samples = 0

    for ch_idx, path in enumerate(args.digital):
        bits = Path(path).read_bytes()
        # Accept either 0/1 byte sequences or text '0'/'1' characters.
        bits = bytes(1 if b in (1, ord("1")) else 0 for b in bits)
        n_digital_samples = max(n_digital_samples, len(bits))
        runs = runs_from_digital_bits(bits, digital_rate)
        files[f"digital-{ch_idx}.bin"] = build_digital_bin(runs, sample_rate=digital_rate)

    for ch_idx, path in enumerate(args.analog):
        samples = Path(path).read_bytes()
        n_analog_samples = max(n_analog_samples, len(samples) // 2)
        files[f"analog-{ch_idx}.bin"] = build_analog_bin(samples, sample_rate=analog_rate)

    files["trigger-store.bin"] = TRIGGER_STORE_BIN
    files["meta.json"] = json.dumps(build_meta_json(
        digital_channels=list(range(len(args.digital))),
        analog_channels=list(range(len(args.analog))),
        digital_rate=digital_rate,
        analog_rate=analog_rate,
        n_digital_samples=n_digital_samples,
        n_analog_samples=n_analog_samples,
    )).encode()

    write_sal(args.out, files)
    print(f"Wrote {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
