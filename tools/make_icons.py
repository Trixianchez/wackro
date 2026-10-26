"""Draws Wackro's pixel-art icons from the hand-placed pixel maps below.

    python tools/make_icons.py

writes into assets/:
    Wackro.ico        app / window / taskbar icon (16, 32, 48, 64, 128, 256 px, every size
                          made from the hand-drawn 16x16 or 32x32 sprite, never smoothed)
    tray_idle.png         system tray icon, gray dot (32x32 = the 16x16 sprite at 2x)
    tray_recording.png    red dot
    tray_playing.png      green dot

Edit a map (one character per pixel, see PAL) and run it again. Needs Pillow.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

PAL = {
    ".": (0, 0, 0, 0),
    "K": (21, 21, 24, 255),      # outline
    "H": (64, 70, 80, 255),      # hair
    "h": (118, 128, 142, 255),    # hair highlight
    "S": (247, 200, 162, 255),   # skin
    "s": (222, 150, 112, 255),   # skin shadow
    "W": (255, 255, 255, 255),   # lens / eye white
    "G": (214, 216, 220, 255),   # shirt
    "g": (150, 154, 162, 255),   # shirt shadow
    "R": (230, 40, 46, 255),     # record red
    "p": (255, 150, 150, 255),   # red highlight
}

S32 = [
    "." * 32,
    "." * 10 + "KK" + "." * 3 + "KKK" + "." * 3 + "KK" + "." * 9,
    "." * 8 + "KKHHK" + "KKHHHKK" + "KHHKK" + "." * 7,
    "." * 6 + "KK" + "H" * 16 + "KK" + "." * 6,
    "." * 5 + "K" + "HHh" + "H" * 7 + "hH" + "H" * 7 + "K" + "." * 6,
    "." * 4 + "K" + "H" * 21 + "K" + "." * 5,
    "." * 3 + "K" + "H" * 3 + "hh" + "H" * 18 + "K" + "." * 4,
    "." * 3 + "K" + "H" * 24 + "K" + "." * 3,
    "." * 2 + "K" + "H" * 25 + "K" + "." * 3,
    "." * 2 + "K" + "HHHH" + "SS" + "HHHHHH" + "SSS" + "H" * 10 + "K" + "." * 3,
    "." * 2 + "K" + "HHH" + "SSSS" + "HHH" + "S" * 7 + "H" * 8 + "K" + "." * 3,
    "." * 2 + "K" + "HH" + "S" * 19 + "HHHH" + "K" + "." * 3,
    "." * 2 + "K" + "H" + "S" * 21 + "HHH" + "K" + "." * 3,
    "..K" + "H" + "SS" + "SSKKKSS" + "SSS" + "SSKKKSS" + "SS" + "HHH" + "K" + "...",
    "..K" + "H" + "SS" + "SKWWWKS" + "SSS" + "SKWWWKS" + "SS" + "HHH" + "K" + "...",
    "..K" + "H" + "SS" + "KWWWWWK" + "KKK" + "KWWWWWK" + "SS" + "KSs" + "K" + "...",
    "..K" + "H" + "SS" + "KWWKWWK" + "SSS" + "KWWKWWK" + "SS" + "KsS" + "K" + "...",
    "..K" + "H" + "SS" + "KWWWWWK" + "SSS" + "KWWWWWK" + "SS" + "KSs" + "K" + "...",
    "..K" + "S" + "SS" + "SKWWWKS" + "SSS" + "SKWWWKS" + "SS" + "KSK" + "." + "...",
    "..K" + "S" + "SS" + "SSKKKSS" + "SSS" + "SSKKKSS" + "SS" + "KK." + "." + "...",
    "..K" + "S" * 22 + "K" + "." * 6,
    "...K" + "S" * 21 + "K" + "." * 6,
    "...K" + "S" * 7 + "sK" + "SSSS" + "Ks" + "S" * 6 + "K" + "." * 6,
    "....K" + "S" * 7 + "KKKKKK" + "S" * 6 + "K" + "." * 7,
    "....K" + "S" * 19 + "K" + "." * 7,
    "....." + "KK" + "S" * 15 + "KK" + "." * 8,
    "." * 9 + "KKK" + "ssssss" + "KKK" + "." * 11,
    "." * 5 + "KKKK" + "GGK" + "ssss" + "KGG" + "KKKK" + "." * 9,
    "." * 3 + "KK" + "G" * 5 + "GGK" + "ssss" + "KGG" + "G" * 5 + "KK" + "." * 5,
    "." * 2 + "K" + "G" * 9 + "GK" + "ss" + "KG" + "G" * 9 + "K" + "." * 4,
    "." * 1 + "K" + "G" * 10 + "gGKKGg" + "G" * 10 + "K" + "." * 3,
    "." * 1 + "K" + "G" * 26 + "K" + "." * 3,
]
DOT9 = [
    "..KKKKK..",
    ".KRRRRRK.",
    "KRpRRRRRK",
    "KRRRRRRRK",
    "KRRRRRRRK",
    "KRRRRRRRK",
    "KRRRRRRRK",
    ".KRRRRRK.",
    "..KKKKK..",
]
S16 = [
    "................",
    ".....KK..KK.....",
    "...KKHHKKHHKK...",
    "..KHHHHHHHHHHK..",
    ".KHHHHHHHHHHHHK.",
    ".KHHSHHHSSHHHHK.",
    ".KHSSSSSSSSSHHK.",
    ".KHKKKKSKKKKSHK.",
    ".KSKWWKKKWWKSSK.",
    ".KSKWWKSKWWKSSK.",
    ".KSKKKKSKKKKSSK.",
    ".KSSSSSSSSSSSSK.",
    "..KSSSKKKSSSSK..",
    "...KKSSSSSSKK...",
    "..KGGGKKKKGGGK..",
    ".KGGGGGGGGGGGGK.",
]
DOT6 = [
    ".KKK.",
    "KpRRK",
    "KRRRK",
    "KRRRK",
    ".KKK.",
]

def build(rows, size, dot, at):
    for i, r in enumerate(rows):
        assert len(r) == size, (size, i, len(r), r)
    img = Image.new("RGBA", (size, size)); p = img.load()
    for y, r in enumerate(rows):
        for x, c in enumerate(r):
            p[x, y] = PAL[c]
    ox, oy = at
    for y, r in enumerate(dot):
        for x, c in enumerate(r):
            if c != ".":
                p[ox + x, oy + y] = PAL[c]
    return img


DOT_COLORS = {
    "recording": ((230, 40, 46, 255), (255, 150, 150, 255)),
    "playing": ((46, 184, 92, 255), (150, 235, 175, 255)),
    "idle": ((128, 134, 144, 255), (196, 200, 206, 255)),
}


def recolor_dot(img, state):
    """The red record dot in another colour (only pixels of the dot change)."""
    red, highlight = PAL["R"], PAL["p"]
    body, light = DOT_COLORS[state]
    out = img.copy()
    p = out.load()
    for y in range(out.height):
        for x in range(out.width):
            if p[x, y] == red:
                p[x, y] = body
            elif p[x, y] == highlight:
                p[x, y] = light
    return out


def scaled(img, factor):
    return img.resize((img.width * factor, img.height * factor), Image.NEAREST)


def main(out_dir=ROOT / "assets"):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    i32 = build(S32, 32, DOT9, (23, 23))
    i16 = build(S16, 16, DOT6, (11, 11))
    frames = {16: i16, 32: i32, 48: scaled(i16, 3), 64: scaled(i32, 2), 128: scaled(i32, 4), 256: scaled(i32, 8)}
    frames[256].save(out_dir / "Wackro.ico", format="ICO", sizes=[(s, s) for s in frames],
                     append_images=[frames[s] for s in frames if s != 256])
    for state in DOT_COLORS:
        scaled(recolor_dot(i16, state), 2).save(out_dir / f"tray_{state}.png")


if __name__ == "__main__":
    main()
