"""Draw assets/icon.png (512 px) and assets/icon-256.png. Needs Pillow:

    uv run --with pillow python tools/make_icon.py assets/icon.png
"""
import sys
from PIL import Image, ImageDraw

S = 4                      # supersampling
W = 512 * S
out = sys.argv[1]

def s(*v): return [int(x * S) for x in v]

# background: rounded square with vertical teal gradient
bg = Image.new("RGBA", (W, W), (0, 0, 0, 0))
grad = Image.new("RGBA", (W, W))
gd = ImageDraw.Draw(grad)
top, bot = (0, 166, 147), (0, 105, 92)
for y in range(W):
    t = y / (W - 1)
    gd.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle(s(16, 16, 496, 496), radius=110 * S, fill=255)
bg.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(bg)

WHITE = (255, 255, 255, 255)
SOFT = (255, 255, 255, 70)

# wifi arcs (top right)
cx, cy = 384, 150
for r, w in ((26, 14), (56, 14), (86, 14)):
    d.arc(s(cx - r, cy - r, cx + r, cy + r), start=225, end=315, fill=WHITE, width=w * S)
d.ellipse(s(cx - 11, cy - 11, cx + 11, cy + 11), fill=WHITE)

# printer frame: gantry uprights + top bar
d.rounded_rectangle(s(118, 238, 142, 392), radius=8 * S, fill=WHITE)      # left upright
d.rounded_rectangle(s(338, 238, 362, 392), radius=8 * S, fill=WHITE)      # right upright (shorter, wifi above)
d.rounded_rectangle(s(118, 226, 362, 250), radius=8 * S, fill=WHITE)      # X gantry bar
# print head with nozzle
d.rounded_rectangle(s(204, 212, 272, 282), radius=12 * S, fill=WHITE)
d.polygon(s(226, 282, 250, 282, 238, 302), fill=WHITE)

# printed part: 4 colour bands (the 4 filament slots)
bands = [(255, 138, 61), (255, 214, 79), (79, 195, 247), (236, 64, 122)]
x0, x1, y_bottom, h = 196, 280, 350, 11
for i, c in enumerate(bands):
    y = y_bottom - (i + 1) * h
    d.rectangle(s(x0, y, x1, y + h - 1), fill=c + (255,))
# bed (moves in Y) and base
d.rounded_rectangle(s(150, 352, 330, 368), radius=6 * S, fill=WHITE)
d.rounded_rectangle(s(96, 392, 384, 416), radius=10 * S, fill=WHITE)
d.rounded_rectangle(s(232, 368, 248, 392), radius=4 * S, fill=SOFT)        # Y carriage hint

img = bg.resize((512, 512), Image.LANCZOS)
img.save(out)
img.resize((256, 256), Image.LANCZOS).save(out.replace(".png", "-256.png"))
print("ok", out)
