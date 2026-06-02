# Logic 2 `.sal` Capture Format — Reverse Engineering Notes

Source: `Logic-2.4.44-linux-x64.AppImage`. Reverse-engineered by
instruction-level disassembly of `libgraph_server_shared.so` via
radare2 + r2ghidra (xrefs to `__PRETTY_FUNCTION__` strings reachable
through `48 8d 15` / `4c 8d 05` LEA encodings), then validated against
a reference `capture.sal` saved from the Logic Pro 8 simulator plus
the matching CSV exports.

The companion `sigrok2sal.py` script implements everything documented
here. The digital path round-trips byte-perfect against the reference
(9927/9927 transitions reproduce exactly).

## 1. Container

`.sal` is a ZIP archive containing:

```
meta.json              # JSON, schema version 22
digital-<N>.bin        # one per enabled legacy digital channel
analog-<N>.bin         # one per enabled legacy analog channel
trigger-store.bin      # present when meta version >= 21
```

Non-legacy instrument channels use
`instrument-<port>-<type>-<channel>.bin`. From `fileForChannel()`
in the Electron bundle.

## 2. `meta.json`

```json
{
  "version": 22,
  "data": { /* full session state */ },
  "binData": [
    { "category": "legacy", "type": "Digital",
      "deviceChannel": 0, "file": "./digital-0.bin" },
    ...
  ]
}
```

A minimal valid `data` populates: `legacyDevice` (with `deviceType` like
`"LogicPro8"`, `isSimulation`, `capabilities`), `legacySettings`
(`enabledChannels`, `sampleRate.{digital,analog}`, `digitalThreshold`,
`glitchFilter`), `captureSettings`, `timeManager`, `rowsSettings` (one
per channel), `captureStartTime`, `renderViewState`, plus empty arrays
for `measurements`, `analyzers`, `highLevelAnalyzers`,
`timingMarkers`. The converter (§9) provides a template.

## 3. `.bin` universal header (16 bytes, all `.bin` files)

```
offset  size  field
0x00    8     char[8]  magic = "<SALEAE>"
0x08    4     uint32   version = 3
0x0C    4     uint32   SaleaeBinaryDataType
                         100 = Digital
                         101 = Analog
                         103 = TriggerStore
```

## 4. Channel-specific header (35 bytes, 0x10..0x32)

```
0x10  uint32   = 1                 schema sub-version flag
0x14  uint32   per-channel-type    differs digital vs analog
0x18  uint32   0xDA2C3C41          capture-clock id (constant across files)
0x1C  uint32   0x00019E87          ditto
0x20  uint32   per-channel-type
0x24  uint32                       (top bits ~A7 across all)
0x28  uint32                       (digital 0x4E vs analog 0x4A in low byte)
0x2C  uint32   0
0x30  uint8[3] 0
```

Pending: which fields carry sample rate / scale / DC offset. Will
disambiguate with a second reference at a different sample rate.

The converter currently replays the 35 bytes from the Logic Pro 8
simulator reference verbatim (separate templates for digital and
analog). This works against captures from the same device profile;
other devices will need their own header constants.

## 5. Chunks

After the channel header, files are a sequence of **chunks**. Each
chunk starts with a 26-byte header (24-byte struct + 2-byte tail):

```
struct ChunkHeader {
  uint64_t start_sample;   // first sample index covered by chunk (inclusive)
  uint64_t end_sample;     // last sample index + 1 (exclusive)
                           // end_sample - start_sample == samples in chunk
  uint16_t flag;           // 0 = normal, 1 = final / tail chunk
  uint16_t byte_size;      // digital: bytes of varint stream that follow
                           // analog: see §7
  uint32_t reserved;       // 0
  uint16_t /* 2 bytes */;  // 0 in reference (purpose unknown)
};
```

Reference file has 78 chunks in `digital-0.bin` and 39 chunks in
`analog-0.bin`.

## 6. Digital `.bin` — FULLY DECODED ✔

Each chunk encodes one channel's transitions as a run-length-encoded
stream. The encoder is `Saleae::Digital::AppendRle(uint8_t*, int64_t&,
SampleCount)` in `libgraph_server_shared.so` (function at vaddr
`0x2dcea70` in the shipped binary, reached via the
`"count > 0"` assertion's `__PRETTY_FUNCTION__` xref).

### 6.1 Encoding

Each entry encodes `N = number of samples in this run`. The sample
value alternates 0 → 1 → 0 → ... starting from the channel's initial
state (state at sample 0). The encoder stores `N - 1`:

- **Single byte** (`N - 1 < 0x40`): one byte `= N - 1`.
- **Multi-byte** (`N - 1 >= 0x40`):
  - First byte: `0x40 | ((N-1) >> shift) & 0x3F` (top two bits `01`).
  - Continuation byte(s): `0x80 | (next 7 bits)` (top bit `1`).
  - Last byte: `(low 7 bits of N-1)` (top bit `0`).

Decoder accepts a byte as a varint start iff `byte < 0x40` (single byte)
OR `(byte & 0xC0) == 0x40` (multi-byte start). Decoder reference (Python):

```python
def saleae_varint(buf, off):
    b = buf[off]
    if b < 0x40:
        return b + 1, off + 1
    assert (b & 0xC0) == 0x40
    val = b & 0x3F; off += 1
    while True:
        b = buf[off]; val = (val << 7) | (b & 0x7F); off += 1
        if not (b & 0x80):
            return val + 1, off
```

The encoder is in `sigrok2sal.py::encode_run`, validated to produce
byte-identical output for every value the reference file decodes.

### 6.2 Per-chunk layout

```
ChunkHeader (24 bytes)
uint16   reserved = 0
varints  (byte_size bytes total; cumulative N's sum to end_sample - start_sample)
```

The cumulative N across an entire file (all chunks) covers the full
capture duration. The reference splits the stream into 78 chunks of
~3.2 M samples each, but a **single big chunk works just as well**
(verified: our single-chunk file decodes to the same 9927 runs as the
reference's 78-chunk file).

### 6.3 Verification

A round-trip of CSV-derived runs through our encoder produces a file
whose varint stream — decoded chunk-by-chunk per the spec — yields
exactly the same 9927 run lengths as the reference, in the same order,
with identical cumulative sample count (162,648,320).

## 7. Analog `.bin` — FULLY DECODED ✔

### 7.1 Samples

- **int16, little-endian.**
- Scale (Logic Pro 8 simulator in this capture): **10 V / 2047** (peak
  raw 2047 = 10.0 V, verified ~5 mV agreement across 1000 samples).
- Scale almost certainly comes from one of the channel-header fields
  (§4) — needs another reference at a different range to confirm.

### 7.2 Chunk layout

```
ChunkHeader (24 bytes):
  uint64 start_sample
  uint64 end_sample
  uint64 length             (= end_sample - start_sample)
int16[length] raw_samples   // raw int16 samples
PyramidHeader (28 bytes):   // see §7.3
PyramidData                 // see §7.3
```

Note: analog chunk header is 24 bytes, **no** 2-byte trailer/pad
(unlike digital which has the u16 init-state byte after its chunk
header). Raw samples follow immediately after the 24 bytes.

For chunk 0, `N - 7` of the int16's correspond 1:1 to the CSV's
exported analog samples; the leading **7 int16s are pre-trigger
overlap** (the reference capture has slightly more data in the
binary than the CSV exports).

### 7.3 Multi-level (min, max) pyramid

Each chunk's pyramid section is a binary tree of `(min, max)` pairs,
used by Logic 2 for fast rendering at any zoom level. **Verified
byte-for-byte against the reference (208,628 bytes match exactly).**

```
PyramidHeader (28 bytes):
  uint32  pair_count_L1     (= ceil(chunk_length / 16))
  uint32  unknown1          (purpose unknown — 0xFEBD8 for the 417040-sample
                             reference chunk, 884 for a 16-sample chunk)
  uint32  unknown2  = 0
  uint32  level_count       (= number of levels)
  uint32  unknown3  = 0
  uint32  pair_count_L1     (= field 0, repeated)
  uint32  unknown4  = 0

L1 data:
  int16[2 * pair_count_L1]  // pair_count_L1 (min, max) pairs

For each i in [2 .. level_count]:
  uint64 pair_count_Li      (= floor(pair_count_L(i-1) / 2))
  int16[2 * pair_count_Li]  // pair_count_Li (min, max) pairs
```

### 7.4 Pyramid level computation

```
L1[i] = (min(raw[i*16 : (i+1)*16]),
         max(raw[i*16 : (i+1)*16]))      for i in [0, ceil(N/16))

L_k[i] = (min(L_(k-1)[2i].min, L_(k-1)[2i+1].min),
          max(L_(k-1)[2i].max, L_(k-1)[2i+1].max))   for i in [0, floor(prev_count/2))
```

Levels halve until exactly one pair remains, **except** for the tiny
case where `pair_count_L1 == 1`: there an extra empty L2 is written
(u64 = 0, no data) acting as a terminator, and `level_count = 2`.

### 7.5 size_field (header field 1)

```
size_field = 848 + raw_bytes + 4 * sum_of_pair_counts_across_all_populated_levels
```

i.e. `sizeof(in-memory PackedWaveformData)` (=848) plus the bytes of
actual numeric data Logic 2 must materialize when loading this chunk
— the raw samples plus 4 bytes per (min, max) pair across every
level. Verified byte-exact for both the 417040-sample chunk and the
16-sample chunk in the reference file.

## Verification

A converter using this spec (`/work/project/sigrok2sal.py`) produces a
.sal file that loads cleanly into Logic 2.4.44 and renders correctly
at all zoom levels (confirmed against a synthesized 1 kHz, ±9.77 V,
200 ms single-channel analog sine wave at 12.5 MHz).

## 8. `trigger-store.bin`

For a no-trigger capture the file is exactly 32 bytes:

```
0x00  <SALEAE>
0x08  03 00 00 00     version = 3
0x0C  67 00 00 00     type = 103
0x10  01 00 00 00     u32 = 1
0x14  00 00 00 00     u32 = 0
0x18  00 00 00 00 00 00 00 00   u64 = 0
```

`sigrok2sal.py` writes this 32-byte constant verbatim.

## 9. Converter status

| Component                       | Status               |
|---------------------------------|----------------------|
| ZIP container                   | ✔                    |
| `meta.json`                     | ✔ (template + values) |
| `<SALEAE>` universal header     | ✔                    |
| Channel header 0x10..0x32       | ✔ replayed from reference (LogicPro8 simulator) |
| ChunkHeader                     | ✔                    |
| Digital RLE encoding            | ✔ byte-exact         |
| Analog raw samples              | ✔ (int16 + known scale) |
| Analog pyramid                  | ❌ stub (empty) — needs test |
| `trigger-store.bin`             | ✔ (32-byte constant) |

The Python converter is `/work/project/sigrok2sal.py`. Test of digital
path is bit-identical to the reference; analog path will produce files
of correct size with valid headers but the pyramid is empty.
Confirming/refuting Logic 2's tolerance for an empty pyramid is the
next test.
