import re
from PIL import Image, ImageOps, ImageEnhance

COLS, ROWS = 36, 25
CELL_W, CELL_H = 9.6, 20.0  # worst-case monospace fallback cell size in the SVG

img = Image.open('avatar.png').convert('L')
w, h = img.size

# crop a vertical strip matching the art column's aspect ratio, centered on the face
target_aspect = (COLS * CELL_W) / (ROWS * CELL_H)  # ~0.74
crop_w = int(h * target_aspect)
face_cx = int(w * 0.58)
x0 = max(0, min(w - crop_w, face_cx - crop_w // 2))
img = img.crop((x0, 0, x0 + crop_w, h))

img = ImageOps.autocontrast(img, cutoff=2)
img = ImageEnhance.Contrast(img).enhance(1.25)
img = img.resize((COLS, ROWS), Image.LANCZOS)

RAMP = " .':;li|jkW%8@"  # sparse -> dense

def to_lines(invert):
    lines = []
    for y in range(ROWS):
        line = ''
        for x in range(COLS):
            v = img.getpixel((x, y)) / 255
            if invert:
                # gamma-compress so only true blacks get dense ink on a light background
                v = (1 - v) ** 2.2
            line += RAMP[min(int(v * len(RAMP)), len(RAMP) - 1)]
        lines.append(line)
    return lines

def block(lines):
    out = []
    for i, line in enumerate(lines):
        y = 30 + i * 20
        out.append(f'<tspan x="15" y="{y}">{line}</tspan>')
    return '\n'.join(out)

for fname, invert in (('dark_mode.svg', False),
                      ('light_mode.svg', True)):
    with open(fname) as f:
        svg = f.read()
    svg = re.sub(r'(<text x="15" y="30"[^>]*>\n).*?(\n</text>)',
                 lambda m: m.group(1) + block(to_lines(invert)) + m.group(2),
                 svg, count=1, flags=re.DOTALL)
    with open(fname, 'w') as f:
        f.write(svg)
    print('updated', fname)
