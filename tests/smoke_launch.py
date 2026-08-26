"""
Smoke test 0.7: реальный launch Camoufox через AgentFox EngineAdapter.
Проверяет: create_profile → launch → goto → extract → close → locks.
Запуск: python3 tests/smoke_launch.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm
import tempfile

pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_smoke_"))
print(f"[1] isolated profiles root: {pm.PROFILES_ROOT}")

from core.profile_manager import create_profile, Profile
from core.session import get_engine

# --- 1. создать профиль
p = create_profile("smoke_001", geo="DE", targets=["example.com"])
print(f"[2] profile created: os={p.identity.os} locale={p.identity.locale} tz={p.identity.timezone}")
assert p.identity.canvas_seed, "canvas seed missing"

# --- 2. acquire lock
assert p.acquire("smoke_test"), "acquire failed"
locked, why = p.is_locked(ignore_owner="smoke_test")
print(f"[3] locked={locked}")

# --- 3. launch
engine = get_engine(p)
t0 = time.time()
page = engine.launch(p, headless=True)
print(f"[4] launched in {time.time()-t0:.1f}s")

try:
    # --- 4. goto
    t0 = time.time()
    page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
    print(f"[5] goto example.com in {time.time()-t0:.1f}s")

    # --- 5. extract
    title = page.title()
    h1 = page.evaluate("document.querySelector('h1')?.innerText || ''")
    ua = page.evaluate("navigator.userAgent")
    webdriver = page.evaluate("navigator.webdriver")
    print(f"[6] title: {title!r}")
    print(f"    h1: {h1!r}")
    print(f"    UA: {ua[:80]}...")
    print(f"    navigator.webdriver: {webdriver}")
    assert "Example Domain" in title, f"unexpected title {title!r}"
    assert webdriver is False or webdriver is None, f"webdriver leak: {webdriver}"

    # --- 6. fingerprint smoke (стабильность сидов между двумя launch не проверяем тут — это долгий тест)
    canvas_hash = page.evaluate("""
        () => {
            const c = document.createElement('canvas');
            const ctx = c.getContext('2d');
            ctx.textBaseline = 'top';
            ctx.font = '14px Arial';
            ctx.fillText('agentfox🦊', 2, 2);
            return c.toDataURL().slice(-64);
        }
    """)
    print(f"[7] canvas hash tail: {canvas_hash[-32:]}")

    print("SMOKE OK")
finally:
    engine.close()
    p.release()
    p.warmup.record_session()
    p.save()
    print("[8] closed, released, saved")

# --- 7. reload из диска — персистентность
p2 = Profile.load("smoke_001")
print(f"[9] reloaded from disk: sessions_total={p2.health.total_sessions}, warmup_sessions={p2.warmup.total_sessions}")
assert p2.warmup.total_sessions == 1
print("PERSISTENCE OK")
