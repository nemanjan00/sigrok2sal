# sigrok2sal

Write [Saleae Logic 2](https://www.saleae.com/pages/downloads) `.sal` capture
files from Python, including from [sigrok](https://sigrok.org/)
`.sr` session files.

Saleae intentionally does not document the `.sal` format, and
[libsigrok cannot read or write it](https://sigrok.org/wiki/File_format:saleae).
This project reverse-engineered the format from `libgraph_server_shared.so`
(shipped in Logic 2.4.44) and provides:

- a Python writer for `.sal` files (digital + analog channels, single device);
- a `.sr → .sal` converter;
- the full format specification in [`SAL_FORMAT.md`](SAL_FORMAT.md).

Output produced by this project has been verified to load and render
correctly in Logic 2.4.44.

## Status

| Feature | Status |
| --- | --- |
| `.sal` container (zip + `meta.json`) | ✅ |
| Universal `<SALEAE>` header | ✅ |
| Per-channel file header (sample rate, capture time, chunk count, …) | ✅ |
| Digital RLE varint encoding | ✅ byte-exact against reference |
| Analog `int16` samples + (min, max) pyramid | ✅ byte-exact against reference |
| `trigger-store.bin` (no triggers) | ✅ |
| Triggers / MSO instruments / multi-device captures | ❌ not implemented |
| Per-channel voltage scale other than ±10 V | ❌ uses the simulator's `10 V / 2047` |

## Install

Requirements: Python ≥ 3.9, NumPy.

```sh
python3 -m pip install numpy
```

Drop the two `.py` files anywhere on your `PATH` (or `sys.path`); there's
no setup yet.

## Usage

### `.sr → .sal`

```sh
./sr2sal.py input.sr output.sal
```

The converter reads the sigrok `metadata` block to figure out:
- digital sample rate and channel count;
- analog channel count and per-channel float32 voltages.

Voltage range is fixed at ±10 V (matching Logic 2's Logic Pro 8
simulator profile); analog samples outside that range clip.

### Library

```python
from sigrok2sal import (
    build_digital_bin, build_analog_bin, build_meta_json,
    runs_from_digital_bits, TRIGGER_STORE_BIN,
)
import json, zipfile

# Digital: one byte per sample, value 0 or 1.
bits = b"\x00\x00\x01\x01\x01\x00\x00\x01" * 1000
runs = runs_from_digital_bits(bits, sample_rate=10_000_000)
dig_bin = build_digital_bin(runs, sample_rate=10_000_000)

# Analog: little-endian int16 samples scaled so ±2047 == ±10 V.
import numpy as np
samples = (2000 * np.sin(2 * np.pi * 1_000 * np.arange(2_500_000) / 12_500_000)
           ).astype(np.int16).tobytes()
ana_bin = build_analog_bin(samples, sample_rate=12_500_000)

meta = build_meta_json(
    digital_channels=[0], analog_channels=[0],
    digital_rate=10_000_000, analog_rate=12_500_000,
    n_digital_samples=len(bits), n_analog_samples=2_500_000,
)

with zipfile.ZipFile("out.sal", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("meta.json", json.dumps(meta))
    zf.writestr("digital-0.bin", dig_bin)
    zf.writestr("analog-0.bin", ana_bin)
    zf.writestr("trigger-store.bin", TRIGGER_STORE_BIN)
```

## Format

See [`SAL_FORMAT.md`](SAL_FORMAT.md). Brief structure:

```
out.sal (ZIP archive)
├── meta.json            JSON, schema version 22
├── digital-<N>.bin      RLE varint stream per channel
├── analog-<N>.bin       int16 samples + multi-level (min,max) pyramid
└── trigger-store.bin    32 bytes (no triggers)
```

Every `.bin` file begins with `<SALEAE>` magic, a `version=3` u32, a
type code (`100` digital / `101` analog / `103` trigger), and a 35-byte
per-channel header carrying sample rate, capture timestamp, and chunk
count. Both the digital RLE encoding and the analog (min, max)
pyramid are described in detail in `SAL_FORMAT.md`.

## Caveats

- **Single device only.** `meta.json` describes one device with a
  Logic Pro 8 simulator profile. Multi-device or instrument captures
  would need additional fields decoded.
- **No triggers.** `trigger-store.bin` is a fixed 32-byte "no triggers"
  stub. Real trigger metadata isn't decoded.
- **Voltage scaling is fixed.** Analog samples are written as int16
  in Logic 2's ±2047-maps-to-±10 V grid. Inputs outside that range clip.
  Per-channel scale info is not yet decoded in the binary header.
- **Format may change.** Saleae explicitly does not commit to a stable
  `.sal` format; output verified against Logic 2.4.44 only.

## Layout

```
sigrok2sal.py        Library: SAL writer, meta builder, varint + pyramid encoders.
sr2sal.py            CLI: read a .sr archive and emit .sal.
SAL_FORMAT.md        Reverse-engineering notes (the format spec).
samples/             Test fixtures (gitignored).
```

## License

[MIT](LICENSE).

This project is independent of Saleae Inc. and sigrok. It does not
redistribute any Saleae software; users wishing to verify generated
files need to obtain Logic 2 separately from Saleae.
