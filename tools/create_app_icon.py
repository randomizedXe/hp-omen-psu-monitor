"""Generate a small original vector-style bolt icon as a Windows ICO."""
from pathlib import Path
import struct

SIZE = 64
POLYGON = [(37, 6), (16, 35), (31, 35), (23, 59), (50, 25), (35, 25)]


def inside(x, y):
    result = False
    previous = POLYGON[-1]
    for current in POLYGON:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            result = not result
        previous = current
    return result


pixels = bytearray()
for y in range(SIZE - 1, -1, -1):
    for x in range(SIZE):
        color = (85, 222, 197) if inside(x + 0.5, y + 0.5) else (24, 31, 43)
        pixels += bytes((color[2], color[1], color[0], 255))
mask = bytes(((SIZE + 31) // 32) * 4 * SIZE)
dib = struct.pack('<IiiHHIIiiII', 40, SIZE, SIZE * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0) + pixels + mask
ico = struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', SIZE, SIZE, 0, 0, 1, 32, len(dib), 22) + dib
path = Path(__file__).resolve().parents[1] / 'assets' / 'omen_psu.ico'
path.parent.mkdir(exist_ok=True)
path.write_bytes(ico)
print(path)
