"""Generate sample BMP images into samples/ (stdlib only)."""

import os

from bmp import BMPImage, dumps, save

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def gradient(width, height, bpp, top_down=False, reserved=0xFF):
    px = bytearray(width * height * (bpp // 8))
    i = 0
    for y in range(height):
        for x in range(width):
            px[i] = (x * 255) // max(width - 1, 1)      # B
            px[i + 1] = (y * 255) // max(height - 1, 1)  # G
            px[i + 2] = ((x + y) * 255) // max(width + height - 2, 1)  # R
            if bpp == 32:
                px[i + 3] = reserved if not isinstance(reserved, str) else (x * 7 + y * 3) & 0xFF
            i += bpp // 8
    return BMPImage(width, height, bpp, px, top_down)


def main():
    os.makedirs(OUT, exist_ok=True)
    save(gradient(64, 48, 24), os.path.join(OUT, "gradient_24_bottomup.bmp"))
    save(gradient(64, 48, 32, top_down=True, reserved="pattern"),
         os.path.join(OUT, "gradient_32_topdown.bmp"))
    save(gradient(1, 1, 24), os.path.join(OUT, "tiny_1x1_24.bmp"))
    save(gradient(1, 1, 32, reserved=0xAB), os.path.join(OUT, "tiny_1x1_32.bmp"))
    save(gradient(511, 1, 24), os.path.join(OUT, "wide_511x1_24.bmp"))
    save(gradient(1, 511, 32, reserved=0x01), os.path.join(OUT, "narrow_1x511_32.bmp"))
    # Quirky file: non-zero row-padding bytes and a custom asymmetric DPI.
    # Exercises the byte-exact round-trip (padding/resolution preservation).
    img = gradient(7, 4, 24)
    img.xpels_per_meter = 96
    img.ypels_per_meter = 300
    img.row_padding = b"\xA5" * 3  # 7px*3bpp = 21 -> 3 pad bytes per row
    with open(os.path.join(OUT, "quirky_7x4_24_nonzero_pad.bmp"), "wb") as fh:
        fh.write(dumps(img))
    print("wrote", len(os.listdir(OUT)), "samples to", OUT)


if __name__ == "__main__":
    main()
