"""Pure-standard-library BMP reader/writer and rescaler.

Supported subset (a common uncompressed bitmap format):
  - Windows BMP ("BM" magic), BITMAPINFOHEADER (>= 40 bytes)
  - 24-bit and 32-bit pixels, BI_RGB (uncompressed) only
  - Row stride padded to 4 bytes (padding is always emitted as zero)
  - Bottom-up (positive height) and top-down (negative height) row order

Pixel storage: `BMPImage.pixels` is a bytearray in *logical top-down*
row-major order, tightly packed (no padding), channels in file order
(B, G, R [, X]).  For 32-bit images the 4th byte (reserved/alpha) is
carried through verbatim on round-trip and interpolated like any other
channel when scaling.

Scaling boundary rules
----------------------
Nearest neighbour: destination pixel (x, y) samples source pixel
(floor(x * src_w / dst_w), floor(y * src_h / dst_h)).

Bilinear: destination pixel centre is mapped into source coordinates
with the "centre-aligned" convention
    src = (dst + 0.5) * src_size / dst_size - 0.5
then clamped to [0, size - 1] (border pixels are replicated, i.e. the
sample window never leaves the image).  Fractional weights use
round-half-up to integers.  With a scale factor of exactly 1 the mapped
coordinate is an integer with zero fraction, so the output is
bit-identical to the input.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

__all__ = [
    "BMPError",
    "BMPImage",
    "MAX_DIMENSION",
    "MAX_PIXELS",
    "loads",
    "load",
    "dumps",
    "save",
    "resize_nearest",
    "resize_bilinear",
]

_MAGIC = b"BM"
_FILE_HEADER_SIZE = 14
_MIN_INFO_HEADER_SIZE = 40
_BI_RGB = 0
_SUPPORTED_BPP = (24, 32)

#: Maximum width or height accepted, in pixels.
MAX_DIMENSION = 1 << 20  # 1_048_576
#: Maximum total pixel count accepted (width * height).
MAX_PIXELS = 1 << 28  # 268_435_456


class BMPError(ValueError):
    """Raised when BMP data is invalid, truncated or unsupported."""


@dataclass
class BMPImage:
    """Decoded BMP image.

    pixels: tightly packed, logical top-down row order, bpp/8 bytes per
    pixel in B, G, R [, X] channel order.
    top_down: row order to use when serialising (preserved from source).
    """

    width: int
    height: int
    bpp: int
    pixels: bytearray
    top_down: bool = False

    @property
    def bytes_per_pixel(self) -> int:
        return self.bpp // 8


def _row_stride(width: int, bpp: int) -> int:
    """Bytes per row in the file, including 4-byte alignment padding."""
    return ((width * bpp + 31) // 32) * 4


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise BMPError(f"尺寸为零或负值不支持：{width}x{height}")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise BMPError(
            f"尺寸超限：{width}x{height}，单边上限为 {MAX_DIMENSION} 像素"
        )
    if width * height > MAX_PIXELS:
        raise BMPError(
            f"像素总数超限：{width}x{height}={width * height}，"
            f"上限为 {MAX_PIXELS} 像素"
        )


def loads(data: bytes) -> BMPImage:
    """Decode a BMP image from bytes, raising BMPError on any defect."""
    if len(data) < _FILE_HEADER_SIZE + _MIN_INFO_HEADER_SIZE:
        raise BMPError(
            f"文件头非法：文件仅 {len(data)} 字节，"
            f"不足最小头部 {_FILE_HEADER_SIZE + _MIN_INFO_HEADER_SIZE} 字节"
        )
    if data[0:2] != _MAGIC:
        raise BMPError(f"文件头非法：魔数为 {data[0:2]!r}，应为 b'BM'")

    declared_file_size, _r1, _r2, pixel_offset = struct.unpack_from("<IHHI", data, 2)

    (header_size,) = struct.unpack_from("<I", data, 14)
    if header_size < _MIN_INFO_HEADER_SIZE:
        raise BMPError(f"文件头非法：DIB 头大小 {header_size}，应 >= 40")
    if len(data) < _FILE_HEADER_SIZE + header_size:
        raise BMPError("文件头非法：DIB 头被截断")

    (width, height_raw, planes, bpp, compression, image_size) = struct.unpack_from(
        "<iiHHII", data, 18
    )

    if planes != 1:
        raise BMPError(f"文件头非法：planes={planes}，应为 1")
    if bpp not in _SUPPORTED_BPP:
        raise BMPError(f"位深不支持：{bpp} 位，仅支持 24/32 位")
    if compression != _BI_RGB:
        raise BMPError(f"压缩方式不支持：compression={compression}，仅支持 BI_RGB(0)")
    if width <= 0:
        raise BMPError(f"文件头非法：宽度 {width} 必须为正整数")
    if height_raw == 0:
        raise BMPError("尺寸为零不支持：高度为 0")

    top_down = height_raw < 0
    height = abs(height_raw)
    _check_dimensions(width, height)

    stride = _row_stride(width, bpp)
    needed = stride * height
    if pixel_offset < _FILE_HEADER_SIZE + header_size:
        raise BMPError(
            f"文件头非法：像素数据偏移 {pixel_offset} 与头部大小矛盾"
        )
    if image_size not in (0, needed):
        raise BMPError(
            f"尺寸与数据长度不符：头部长度字段为 {image_size}，"
            f"按 {width}x{height}x{bpp} 计算应为 {needed}"
        )
    if declared_file_size != 0 and declared_file_size < pixel_offset + needed:
        raise BMPError(
            f"尺寸与数据长度不符：头部声明文件大小 {declared_file_size}，"
            f"像素数据需要 {pixel_offset + needed}"
        )
    if len(data) < pixel_offset + needed:
        raise BMPError(
            f"像素数据不足：需要 {pixel_offset + needed} 字节，"
            f"实际 {len(data)} 字节（文件可能被截断）"
        )

    row_bytes = width * (bpp // 8)
    pixels = bytearray(row_bytes * height)
    for row in range(height):
        src = pixel_offset + row * stride
        dst = row * row_bytes
        pixels[dst : dst + row_bytes] = data[src : src + row_bytes]
    if not top_down:
        # File stores rows bottom-up; normalise to logical top-down.
        flipped = bytearray(len(pixels))
        for row in range(height):
            src = (height - 1 - row) * row_bytes
            flipped[row * row_bytes : (row + 1) * row_bytes] = pixels[src : src + row_bytes]
        pixels = flipped

    return BMPImage(width=width, height=height, bpp=bpp, pixels=pixels, top_down=top_down)


def load(path: str) -> BMPImage:
    """Decode a BMP image from a file path."""
    with open(path, "rb") as fh:
        return loads(fh.read())


def dumps(img: BMPImage) -> bytes:
    """Serialise a BMPImage, preserving its row-order orientation."""
    _check_dimensions(img.width, img.height)
    if img.bpp not in _SUPPORTED_BPP:
        raise BMPError(f"位深不支持：{img.bpp} 位，仅支持 24/32 位")
    row_bytes = img.width * img.bytes_per_pixel
    if len(img.pixels) != row_bytes * img.height:
        raise BMPError(
            f"尺寸与数据长度不符：像素缓冲 {len(img.pixels)} 字节，"
            f"按 {img.width}x{img.height}x{img.bpp} 应为 {row_bytes * img.height}"
        )

    stride = _row_stride(img.width, img.bpp)
    image_size = stride * img.height
    pixel_offset = _FILE_HEADER_SIZE + _MIN_INFO_HEADER_SIZE
    file_size = pixel_offset + image_size

    out = bytearray()
    out += struct.pack("<2sIHHI", _MAGIC, file_size, 0, 0, pixel_offset)
    out += struct.pack(
        "<IiiHHIIiiII",
        _MIN_INFO_HEADER_SIZE,
        img.width,
        -img.height if img.top_down else img.height,
        1,  # planes
        img.bpp,
        _BI_RGB,
        image_size,
        2835,  # ~72 DPI, informational only
        2835,
        0,
        0,
    )
    pad = b"\x00" * (stride - row_bytes)
    rows = range(img.height) if img.top_down else range(img.height - 1, -1, -1)
    for row in rows:
        start = row * row_bytes
        out += img.pixels[start : start + row_bytes]
        out += pad
    return bytes(out)


def save(img: BMPImage, path: str) -> None:
    """Serialise a BMPImage to a file path."""
    with open(path, "wb") as fh:
        fh.write(dumps(img))


def _check_target_size(img: BMPImage, new_width: int, new_height: int) -> None:
    if not isinstance(new_width, int) or not isinstance(new_height, int):
        raise BMPError("目标尺寸必须为整数")
    _check_dimensions(new_width, new_height)


def resize_nearest(img: BMPImage, new_width: int, new_height: int) -> BMPImage:
    """Nearest-neighbour resample: dst(x, y) <- src(floor(x*W/w), floor(y*H/h))."""
    _check_target_size(img, new_width, new_height)
    bpp = img.bytes_per_pixel
    src, w, h = img.pixels, img.width, img.height
    out = bytearray(new_width * new_height * bpp)
    for y in range(new_height):
        sy = y * h // new_height
        src_row = sy * w * bpp
        dst_row = y * new_width * bpp
        for x in range(new_width):
            sx = x * w // new_width
            s = src_row + sx * bpp
            d = dst_row + x * bpp
            out[d : d + bpp] = src[s : s + bpp]
    return BMPImage(new_width, new_height, img.bpp, out, img.top_down)


def resize_bilinear(img: BMPImage, new_width: int, new_height: int) -> BMPImage:
    """Bilinear resample with centre-aligned mapping and clamped borders.

    src_coord = (dst + 0.5) * src_size / dst_size - 0.5, clamped to
    [0, size - 1]; out-of-range taps replicate the border pixel.
    """
    _check_target_size(img, new_width, new_height)
    bpp = img.bytes_per_pixel
    src, w, h = img.pixels, img.width, img.height
    out = bytearray(new_width * new_height * bpp)

    # Precompute per-axis tap indices and 8.8 fixed-point weights.
    def axis(src_size: int, dst_size: int):
        taps = []
        for d in range(dst_size):
            pos = (d + 0.5) * src_size / dst_size - 0.5
            pos = min(max(pos, 0.0), src_size - 1.0)
            i0 = int(math.floor(pos))
            i1 = min(i0 + 1, src_size - 1)
            frac = pos - i0
            w1 = int(frac * 256 + 0.5)
            taps.append((i0, i1, 256 - w1, w1))
        return taps

    x_taps = axis(w, new_width)
    y_taps = axis(h, new_height)

    for y in range(new_height):
        y0, y1, wy0, wy1 = y_taps[y]
        row0 = y0 * w * bpp
        row1 = y1 * w * bpp
        dst_row = y * new_width * bpp
        for x in range(new_width):
            x0, x1, wx0, wx1 = x_taps[x]
            d = dst_row + x * bpp
            p00 = row0 + x0 * bpp
            p01 = row0 + x1 * bpp
            p10 = row1 + x0 * bpp
            p11 = row1 + x1 * bpp
            for c in range(bpp):
                top = src[p00 + c] * wx0 + src[p01 + c] * wx1
                bot = src[p10 + c] * wx0 + src[p11 + c] * wx1
                value = (top * wy0 + bot * wy1 + 32768) >> 16
                out[d + c] = value
    return BMPImage(new_width, new_height, img.bpp, out, img.top_down)
