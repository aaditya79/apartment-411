"""Measure the unified system (black ink + one yellow) for W (:root in index.html) and B (palettes/unify-b-warm.css).
Run: python3 palettes/check_unified.py"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from check import contrast, de, tokens, OKABE_ITO, INCIDENT_RED  # noqa: E402

ROOT = Path(__file__).parent.parent

def check(name, t):
    print(f"\n=== {name}")
    fails = []
    def need(label, value, minimum, unit=":1"):
        if value < minimum: fails.append(label)
        return f"{value:5.1f}{unit}{'' if value >= minimum else '  << below ' + str(minimum)}"
    grounds = ["paper", "panel", "surface", "hover-bg", "code-bg"]
    print("Text contrast (need 4.5:1)")
    for fg in ("ink", "ink-2", "muted"):
        print(f"  {fg:6} " + " | ".join(f"{g} {need(f'{fg} on {g}', contrast(t[fg], t[g]), 4.5)}" for g in grounds))
    pairs = [("on-accent", "accent"), ("on-accent", "accent-hover"), ("flag", "flag-bg"), ("caution", "caution-bg"),
             ("clear", "clear-bg"), ("flag", "surface"), ("caution", "surface"), ("clear", "surface"),
             ("json-key", "code-bg"), ("json-str", "code-bg"), ("json-num", "code-bg")]
    for i in range(0, len(pairs), 4):
        print("  " + " | ".join(f"{a} on {b} {need(f'{a} on {b}', contrast(t[a], t[b]), 4.5)}" for a, b in pairs[i:i + 4]))
    print("Yellow is never text or a lone edge: yellow against paper (why it always gets black text or a black edge)")
    print(f"  yellow on paper {contrast(t['accent'], t['paper']):.1f}:1 · on surface {contrast(t['accent'], t['surface']):.1f}:1"
          f"  -> black edge on the yellow {need('black edge on yellow', contrast(t['ink'], t['accent']), 3)} · black edge on paper "
          f"{need('black edge on paper', contrast(t['ink'], t['paper']), 3)}")
    print("Focus ring: black (yellow is under 3:1 here), against every surface it lands on (need 3:1)")
    print("  " + " | ".join(f"{g} {need(f'focus on {g}', contrast(t['focus'], t[g]), 3)}"
                           for g in grounds + ["accent", "flag-bg", "caution-bg", "clear-bg", "map-bg"]))
    print("Status colors vs each other and the yellow accent, ΔE2000 (need 15; deutan/protan reported)")
    roles = ["flag", "caution", "clear", "accent"]
    for i, a in enumerate(roles):
        for b in roles[i + 1:]:
            print(f"  {a:7} vs {b:7} normal {need(f'{a} vs {b}', de(t[a], t[b]), 15, '')}"
                  f"   deutan {de(t[a], t[b], 'deutan'):5.1f}   protan {de(t[a], t[b], 'protan'):5.1f}")
    print("Pale tints: the amber verdict background vs the yellow hover tint (need 5, a visible difference)")
    print(f"  caution-bg vs hover tint {need('caution-bg vs hover', de(t['caution-bg'], t['accent-soft']), 5, '')}"
          f" · flag-bg vs hover tint {de(t['flag-bg'], t['accent-soft']):.1f} · clear-bg vs hover tint {de(t['clear-bg'], t['accent-soft']):.1f}")
    print("Incident red vs route colors, ΔE2000 (need 15); map colors are fixed in JS")
    print("  " + " | ".join(f"{k} {need(f'incident vs {k}', de(INCIDENT_RED, v), 15, '')}" for k, v in OKABE_ITO.items()))
    print("  RESULT:", "all constraints hold" if not fails else "FAILS: " + ", ".join(fails))
    return not fails

w = tokens(re.search(r":root\s*\{(.*?)\n\}", (ROOT / "index.html").read_text(), re.S).group(1))
ok = check("W · near-white paper", w)
ok &= check("B · warm cream paper", {**w, **tokens((ROOT / "palettes" / "unify-b-warm.css").read_text())})
sys.exit(0 if ok else 1)
