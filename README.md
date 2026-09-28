# Pure-stdlib BMP library (read / write / resize)

Zero-dependency Python 3 library that parses, writes and scales
uncompressed Windows BMP images. No image-processing libraries are used.

## Layout

- `bmp.py` — the library (`load`/`loads`, `save`/`dumps`,
  `resize_nearest`, `resize_bilinear`, `BMPError`)
- `tests/test_bmp.py` — self-tests (stdlib `unittest`)
- `make_samples.py` — regenerates the sample images in `samples/`
- `samples/` — sample BMPs (24/32-bit, both row orders, 1x1, wide, narrow,
  plus one file with non-zero row padding and a custom DPI)

## Supported format subset

- `BM` magic, BITMAPINFOHEADER (>= 40 bytes), `BI_RGB` (uncompressed) only
- 24-bit and 32-bit pixels (`B,G,R[,X]`); the 32-bit reserved/alpha byte
  is preserved verbatim on round-trip
- Rows padded to 4-byte boundaries; the bytes found in each row's
  alignment gap are preserved verbatim (images created in code default
  to zero padding)
- Bottom-up (positive height) and top-down (negative height) row order;
  orientation is preserved when re-writing
- `biXPelsPerMeter` / `biYPelsPerMeter` (print resolution / DPI) are
  preserved as-is; newly constructed images default to 2835 (~72 DPI)

Limits: width/height <= 2^20 each, width*height <= 2^28
(`bmp.MAX_DIMENSION`, `bmp.MAX_PIXELS`). Larger images are rejected with
a `BMPError` stating the limit.

## Round-trip contract

For every file the reader accepts, writing it back reproduces it exactly:

```python
assert bmp.dumps(bmp.loads(raw)) == raw  # byte-for-byte
```

The table lists what happens to each part of the file:

| File region / field | Round-trip behaviour |
| --- | --- |
| Pixel bytes (`B,G,R[,X]`) | Preserved exactly, incl. the 32-bit reserved/alpha byte |
| Row order (sign of height) | Preserved (bottom-up / top-down) |
| Row-padding bytes (4-byte alignment gap) | Preserved exactly, including non-zero bytes |
| `biXPelsPerMeter`, `biYPelsPerMeter` (DPI) | Preserved exactly (any 32-bit value, incl. 0) |
| `bfSize`, `bfOffBits`, `biSizeImage` | Recomputed from geometry (identical when valid) |
| `bfReserved1/2`, `biClrUsed`, `biClrImportant` | Emitted as zero (unused in 24/32-bit BI_RGB) |
| DIB header larger than 40 bytes (V4/V5, ICC), palettes | Not retained; a 40-byte BITMAPINFOHEADER is written |
| Per-row gaps differing between rows | Rejected by the reader (`BMPError`) instead of silently normalised |

Images produced by `resize_*` keep the source resolution fields and row
order; their padding is normalised to zero (pixel content is new anyway).

## Scaling rules

- Nearest neighbour: `dst(x,y) = src(floor(x*W/w), floor(y*H/h))`.
- Bilinear: centre-aligned mapping
  `src = (dst + 0.5) * src_size / dst_size - 0.5`, clamped to
  `[0, size-1]` (border pixels are replicated); round-half-up.
- Scale factor exactly 1 reproduces the input bit-for-bit in both modes.
- Output keeps the input's bit depth (channels) and row orientation.

## Corrupt input handling

`loads` raises `BMPError` with a specific reason for: bad magic /
malformed header, truncated pixel data, size-vs-data-length mismatch,
unsupported bit depth, unsupported compression, zero/negative
dimensions, and over-limit dimensions.

## Run

```sh
python3 make_samples.py                 # generate samples/
python3 -m unittest discover -s tests -v  # run the self-tests
```

## Usage

```python
import bmp
img = bmp.load("samples/gradient_24_bottomup.bmp")
small = bmp.resize_bilinear(img, 32, 24)
bmp.save(small, "out.bmp")
assert bmp.dumps(bmp.loads(bmp.dumps(img))) == bmp.dumps(img)  # lossless
```
