# Pure-stdlib BMP library (read / write / resize)

Zero-dependency Python 3 library that parses, writes and scales
uncompressed Windows BMP images. No image-processing libraries are used.

## Layout

- `bmp.py` — the library (`load`/`loads`, `save`/`dumps`,
  `resize_nearest`, `resize_bilinear`, `BMPError`)
- `tests/test_bmp.py` — self-tests (stdlib `unittest`)
- `make_samples.py` — regenerates the sample images in `samples/`
- `samples/` — sample BMPs (24/32-bit, both row orders, 1x1, wide, narrow)

## Supported format subset

- `BM` magic, BITMAPINFOHEADER (>= 40 bytes), `BI_RGB` (uncompressed) only
- 24-bit and 32-bit pixels (`B,G,R[,X]`); the 32-bit reserved/alpha byte
  is preserved verbatim on round-trip
- Rows padded to 4-byte boundaries; on `loads()` -> `dumps()` the
  original padding bytes (even if non-zero) are restored verbatim
- Bottom-up (positive height) and top-down (negative height) row order;
  orientation is preserved when re-writing

## Round-trip fidelity and normalisation

`loads()` retains the raw bytes of the file header, the complete DIB
header (and any gap/palette bytes before the pixels), every row's
padding bytes, and any trailing bytes after the pixel area. As long as
geometry (width/height/bpp/orientation) is unchanged, `dumps(img)` is
**byte-for-byte identical** to the input file. This covers non-zero row
padding, `biXPelsPerMeter`/`biYPelsPerMeter` (resolution / DPI), the
file-header reserved words, `biSizeImage`, extended DIB fields and
trailing data.

Fields are normalised (rewritten to canonical values) only when the
geometry changes (e.g. after `resize_*`) or for an image constructed
directly via `BMPImage(...)`:

| Field / region | Round-trip, same geometry | After resize / hand-built |
| --- | --- | --- |
| Pixel bytes | preserved | resampled / as given |
| Row padding bytes | preserved verbatim (may be non-zero) | zeroed |
| `bfSize` (file size) | preserved verbatim | recomputed |
| `bfReserved1/2` | preserved verbatim | carried from source, else 0 |
| `biWidth`/`biHeight` | preserved | new geometry |
| `biBitCount`, `biCompression`, `biPlanes` | preserved | 24/32, BI_RGB, 1 |
| `biSizeImage` | preserved verbatim | recomputed (`stride * height`) |
| `biXPelsPerMeter`/`biYPelsPerMeter` (DPI) | preserved verbatim | carried from source, else 2835 (~72 DPI) |
| `biClrUsed`/`biClrImportant`, DIB extensions, pre-pixel gap | preserved verbatim | preserved verbatim |
| Bytes after pixel area | preserved verbatim | preserved verbatim (from source) |

Limits: width/height <= 2^20 each, width*height <= 2^28
(`bmp.MAX_DIMENSION`, `bmp.MAX_PIXELS`). Larger images are rejected with
a `BMPError` stating the limit.

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

# Raw files round-trip byte-for-byte, non-zero padding included:
# assert bmp.dumps(bmp.loads(open("in.bmp","rb").read())) == open("in.bmp","rb").read()
```
