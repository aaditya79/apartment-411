"""Measure a palette: text contrast, focus-ring contrast, and color distances. Run: python3 palettes/check.py

Reads the current palette from :root in index.html and each palettes/*.css override (which redefines tokens only).
"""
import math, re, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

def tokens(css: str) -> dict:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{3,6})\b", css))

def rgb(h):
    h = h.lstrip("#"); h = "".join(c * 2 for c in h) if len(h) == 3 else h
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

def lin(c): return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
def unlin(c): c = min(max(c, 0), 1); return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

def contrast(a, b):
    L = lambda h: sum(w * lin(c) for w, c in zip((0.2126, 0.7152, 0.0722), rgb(h)))
    hi, lo = sorted((L(a), L(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)

def lab(rgb3):
    r, g, b = (lin(c) for c in rgb3)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047; y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))

def de2000(c1, c2):
    L1, a1, b1 = c1; L2, a2, b2 = c2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2); Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p, a2p = a1 * (1 + G), a2 * (1 + G); C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1 = math.degrees(math.atan2(b1, a1p)) % 360; h2 = math.degrees(math.atan2(b2, a2p)) % 360
    dL, dC = L2 - L1, C2p - C1p
    dh = 0 if C1p * C2p == 0 else (h2 - h1 if abs(h2 - h1) <= 180 else h2 - h1 - 360 if h2 > h1 else h2 - h1 + 360)
    dH = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dh / 2))
    Lb, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    hb = h1 + h2 if C1p * C2p == 0 else ((h1 + h2) / 2 if abs(h1 - h2) <= 180 else (h1 + h2 + 360) / 2 if h1 + h2 < 360 else (h1 + h2 - 360) / 2)
    T = 1 - 0.17 * math.cos(math.radians(hb - 30)) + 0.24 * math.cos(math.radians(2 * hb)) + 0.32 * math.cos(math.radians(3 * hb + 6)) - 0.20 * math.cos(math.radians(4 * hb - 63))
    SL = 1 + 0.015 * (Lb - 50) ** 2 / math.sqrt(20 + (Lb - 50) ** 2); SC = 1 + 0.045 * Cbp; SH = 1 + 0.015 * Cbp * T
    RT = -2 * math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7)) * math.sin(math.radians(60 * math.exp(-((hb - 275) / 25) ** 2)))
    return math.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))

CVD = {  # Machado et al. 2009, severity 1.0, applied in linear RGB
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
}
def simulate(h, kind):
    l = [lin(c) for c in rgb(h)]
    return tuple(unlin(sum(m * v for m, v in zip(row, l))) for row in CVD[kind])

def de(a, b, kind=None):
    f = (lambda h: simulate(h, kind)) if kind else rgb
    return de2000(lab(f(a)), lab(f(b)))

OKABE_ITO = {"black/ink": "#1b1813", "blue": "#0072B2", "orange": "#E69F00", "green": "#009E73",
             "pink": "#CC79A7", "sky": "#56B4E9"}
INCIDENT_RED = "#b3261e"

def check(name, t):
    print(f"\n=== {name}")
    fails = []
    def need(label, value, minimum, unit=":1"):
        ok = value >= minimum
        if not ok: fails.append(label)
        return f"{value:5.1f}{unit}{'' if ok else '  << below ' + str(minimum)}"
    print("Text contrast (need 4.5:1)")
    grounds = ["paper", "panel", "surface", "accent-soft", "hover-bg", "code-bg"]
    for fg in ("ink", "ink-2", "muted"):
        cells = [f"{g} {need(f'{fg} on {g}', contrast(t[fg], t[g]), 4.5)}" for g in grounds]
        print(f"  {fg:6} " + " | ".join(cells))
    pairs = [("flag", "flag-bg"), ("caution", "caution-bg"), ("clear", "clear-bg"), ("flag", "surface"),
             ("caution", "surface"), ("clear", "surface"), ("on-accent", "accent"), ("on-accent", "accent-hover"),
             ("accent", "paper"), ("accent", "surface"), ("accent", "accent-soft"), ("ink", "brand"),
             ("json-key", "code-bg"), ("json-str", "code-bg"), ("json-num", "code-bg"), ("json-lit", "code-bg")]
    print("  " + " | ".join(f"{a} on {b} {need(f'{a} on {b}', contrast(t[a], t[b]), 4.5)}" for a, b in pairs[:6]))
    print("  " + " | ".join(f"{a} on {b} {need(f'{a} on {b}', contrast(t[a], t[b]), 4.5)}" for a, b in pairs[6:12]))
    print("  " + " | ".join(f"{a} on {b} {need(f'{a} on {b}', contrast(t[a], t[b]), 4.5)}" for a, b in pairs[12:]))
    print("Focus ring: accent against every surface it lands on (need 3:1)")
    print("  " + " | ".join(f"{g} {need(f'focus on {g}', contrast(t['accent'], t[g]), 3)}"
                           for g in grounds + ["flag-bg", "caution-bg", "clear-bg", "map-bg"]))
    print("Status colors and accent, ΔE2000 (need 15 normal vision; deutan/protan reported, labels carry meaning too)")
    roles = ["flag", "caution", "clear", "accent"]
    for i, a in enumerate(roles):
        for b in roles[i + 1:]:
            print(f"  {a:7} vs {b:7} normal {need(f'{a} vs {b}', de(t[a], t[b]), 15, '')}"
                  f"   deutan {de(t[a], t[b], 'deutan'):5.1f}   protan {de(t[a], t[b], 'protan'):5.1f}")
    print("Incident red #b3261e vs route colors, ΔE2000 (need 15) — map colors are fixed in JS, same in every palette")
    print("  " + " | ".join(f"{k} {need(f'incident vs {k}', de(INCIDENT_RED, v), 15, '')}" for k, v in OKABE_ITO.items()))
    print("  RESULT:", "all constraints hold" if not fails else "FAILS: " + ", ".join(fails))
    return not fails

if __name__ == "__main__":
    # The A-D palettes, where the accent is ink-blue text and the focus ring. The unified black + yellow system
    # (unify-*.css, and :root on the color-unify branch) has different roles: palettes/check_unified.py.
    base = tokens(re.search(r":root\s*\{(.*?)\n\}", (ROOT / "index.html").read_text(), re.S).group(1))
    ok = True
    for f in sorted((ROOT / "palettes").glob("[a-d]-*.css")):
        ok &= check(f.stem, {**base, **tokens(f.read_text())})
    sys.exit(0 if ok else 1)
