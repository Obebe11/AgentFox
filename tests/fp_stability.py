"""
Fingerprint guarantees test — что AgentFox реально обеспечивает на beta.28:

СТАБИЛЬНО между сессиями одного профиля (наш слой):
  - User-Agent / platform / oscpu      (fingerprint preset)
  - WebGL vendor/renderer              (preset)
  - Screen / DPR                       (preset)
  - Fonts list / Voices                (наши стабильные subsets)
  - Locale / Timezone                  (identity + geoip)

НЕ стабильно на beta.28 (ограничение движка, см. ARCHITECTURE.md §7.1):
  - Canvas/Audio шум: setCanvasSeed отсутствует в бинарнике,
    шум per-launch by design. Детерминирован ВНУТРИ сессии.

РАЗЛИЧИЯ между профилями — обязательно.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm

pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_fp_"))

from core.profile_manager import create_profile
from core.session import get_engine

PROBE_JS = """
() => ({
    ua: navigator.userAgent,
    platform: navigator.platform,
    webgl: (() => {
        try {
            const gl = document.createElement('canvas').getContext('webgl');
            const dbg = gl.getExtension('WEBGL_debug_renderer_info');
            return dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : 'n/a';
        } catch (e) { return 'err'; }
    })(),
    languages: navigator.languages.join(','),
    hwConc: navigator.hardwareConcurrency,
})
"""

CANVAS_JS = """
() => {
    const c = document.createElement('canvas');
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#f00';
    ctx.fillRect(0, 0, 50, 50);
    ctx.font = '14px Arial';
    ctx.fillText('abc', 2, 2);
    return c.toDataURL();
}
"""

md5 = lambda s: __import__("hashlib").md5(s.encode()).hexdigest()[:12]


def probe(name: str) -> dict:
    try:
        p = pm.Profile.load(name)
    except FileNotFoundError:
        p = pm.create_profile(name, geo="DE")
    p.acquire("fp_test")
    engine = get_engine(p)
    try:
        page = engine.launch(p, headless=True)
        page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
        r = page.evaluate(PROBE_JS)
        # canvas детерминирован внутри сессии?
        r["canvas_inpage_stable"] = page.evaluate(CANVAS_JS) == page.evaluate(CANVAS_JS)
        r["canvas_hash"] = md5(page.evaluate(CANVAS_JS))
        return r
    finally:
        engine.close()
        p.release()


print("[A] профиль A, сессия 1")
a1 = probe("prof_a")
print("[B] профиль A, сессия 2")
a2 = probe("prof_a")
print("[C] профиль B, сессия 1")
b1 = probe("prof_b")

checks = [
    ("UA стабилен между сессиями", a1["ua"] == a2["ua"]),
    ("platform стабилен", a1["platform"] == a2["platform"]),
    ("WebGL стабилен", a1["webgl"] == a2["webgl"]),
    ("languages стабилен", a1["languages"] == a2["languages"]),
    ("canvas детерминирован внутри сессии A#1", a1["canvas_inpage_stable"]),
    ("canvas детерминирован внутри сессии A#2", a2["canvas_inpage_stable"]),
    ("профили различаются (canvas)", a1["canvas_hash"] != b1["canvas_hash"]),
    ("профили различаются (webgl)", a1["webgl"] != b1["webgl"] or a1["ua"] != b1["ua"]),
]

print()
failed = []
for name, ok in checks:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}")
    if not ok:
        failed.append(name)

if failed:
    print(f"\nFINGERPRINT GUARANTEES FAIL ({len(failed)})")
    sys.exit(1)
print("\nFINGERPRINT GUARANTEES OK")
