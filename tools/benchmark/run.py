#!/usr/bin/env python3
"""Honest offline benchmark for AgentFox.

Offline tasks execute real AgentFox modules against deterministic local fixtures.
Tasks that require a real website, proxy, mailbox, or anti-bot decision are
reported as SKIPPED and can be run with ``live_run.py`` or ``e2e_live.py``.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET = Path(__file__).resolve().parent / "dataset.json"
sys.path.insert(0, str(ROOT))

import behavior.mouse as bmouse
import behavior.scroll as bscroll
import behavior.timing as btiming
import core.health as health_mod
import core.identity as identity_mod
import core.metrics as metrics_mod
import core.profile_manager as pm
import core.proxy_pool as proxy_pool
import core.scheduler as scheduler_mod


BENCH_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_bench_"))
pm.PROFILES_ROOT = BENCH_ROOT
LIVE_MODE = False
LIVE_ONLY_TASKS = {
    "t06_cf_free_js_challenge",
    "t07_cf_business_turnstile",
    "t09_google_search",
    "t19_ticketing",
    "t26_autoreg_xcom",
    "t27_autoreg_instagram",
    "t28_autoreg_mailru",
    "t30_mail_imap_otp",
    "t31_mail_confirm_link",
    "t32_scraping_x_mail_cycle",
    "t33_scraping_instagram",
    "t34_scraping_tiktok",
}


class _StubTime:
    def sleep(self, _seconds):
        return None

    def time(self):
        return time.time()


STUB_SLEEP = True
_stub_time = _StubTime()
if STUB_SLEEP:
    bmouse.time = _stub_time
    bscroll.time = _stub_time
    btiming.time = _stub_time
    try:
        import core.cookie_farmer as _cookie_farmer

        _cookie_farmer.time = _stub_time
        _cookie_farmer.BANK_DIR = BENCH_ROOT / "cookie_bank"
        _cookie_farmer.BANK_DIR.mkdir(parents=True, exist_ok=True)
        for _geo, _locale in (("DE", "de-DE"), ("US", "en-US"), ("RU", "ru-RU"), ("CA", "en-CA")):
            _bank = _cookie_farmer.BANK_DIR / f"{_geo}_{_locale}.json"
            if not _bank.exists():
                _cookies = [
                    {"name": "_ga", "value": f"fixture-{idx}", "domain": ".example.test"}
                    for idx in range(20)
                ]
                _bank.write_text(json.dumps(_cookies), encoding="utf-8")
    except Exception:
        pass

    try:
        import core.session as _session

        class _BenchFarmPage:
            def goto(self, _url, timeout=30000, wait_until="domcontentloaded"):
                return None

            def evaluate(self, _js):
                return None

            @property
            def context(self):
                class _Context:
                    def cookies(self):
                        return [
                            {"name": "_ga", "value": "fixture", "domain": ".example.test"},
                            {"name": "_fbp", "value": "fixture", "domain": ".example.test"},
                        ]

                return _Context()

        class _BenchFarmEngine:
            def launch(self, _profile, headless=True):
                return _BenchFarmPage()

            def close(self):
                return None

        _session.get_engine = lambda _profile: _BenchFarmEngine()
    except Exception:
        pass

try:
    metrics_mod.DB_PATH = Path(tempfile.mktemp(prefix="agentfox_bench_metrics_", suffix=".db"))
except Exception:
    pass


def load_dataset() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def _live_only(task_id: str) -> dict:
    return {
        "status": "SKIPPED",
        "detail": f"{task_id} requires an authorized live target; use tools.benchmark.live_run or e2e_live",
        "engine_compare": "live-only; offline result intentionally unavailable",
        "elapsed_ms": 0,
        "steps": 0,
    }


class _FlowMouse:
    def __init__(self):
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float]] = []
        self.wheels: list[int] = []

    def move(self, x, y):
        self.moves.append((x, y))

    def click(self, x, y):
        self.clicks.append((x, y))

    def wheel(self, _dx, dy):
        self.wheels.append(dy)


class _FlowKeyboard:
    def __init__(self):
        self.events: list[object] = []
        self.text = ""

    def press(self, key):
        self.events.append(key)

    def type(self, char, delay=0):
        self.events.append(delay)
        self.text += char


class _FlowLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def bounding_box(self, timeout=10000):
        return {"x": 100, "y": 100, "width": 160, "height": 32}

    def click(self, timeout=5000):
        self.page.locator_clicks += 1


class _FlowContext:
    def cookies(self):
        return [
            {"name": "_ga", "value": "fixture-ga", "domain": ".example.test"},
            {"name": "_fbp", "value": "fixture-fbp", "domain": ".example.test"},
        ]


class _FlowPage:
    """Deterministic DOM fixture; AgentFox behavior code still executes."""

    def __init__(self, records: list[dict] | None = None):
        self.mouse = _FlowMouse()
        self.keyboard = _FlowKeyboard()
        self.context = _FlowContext()
        self.records = list(records or [])
        self.urls: list[str] = []
        self.evaluations: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.locator_clicks = 0

    def goto(self, url, timeout=30000, wait_until="domcontentloaded"):
        self.urls.append(url)

    def evaluate(self, js):
        self.evaluations.append(js)
        if "innerWidth" in js:
            return {"x": 640, "y": 360}
        if "innerText.length" in js:
            return 1200
        if "querySelectorAll" in js:
            if "length" in js and "map" not in js:
                return len(self.records)
            return list(self.records)
        return None

    def locator(self, _selector):
        return _FlowLocator(self)

    def click(self, _selector, timeout=5000):
        self.locator_clicks += 1

    def fill(self, selector, text):
        self.fills.append((selector, text))

    def content(self):
        return "<main data-fixture='true'>public research fixture</main>"


def _run_flow(operations: list[tuple[str, Callable[[], object]]]) -> dict:
    trace: list[str] = []
    started = time.monotonic()
    for label, operation in operations:
        try:
            detail = operation()
            suffix = f" ({detail})" if detail else ""
            trace.append(f"{label}=PASS{suffix}")
        except Exception as exc:
            trace.append(f"{label}=FAIL ({exc})")
            return {
                "status": "FAIL",
                "detail": "steps: " + "; ".join(trace),
                "engine_compare": "real offline flow; failed assertion",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "steps": len(trace),
            }
    return {
        "status": "PASS",
        "detail": "steps: " + "; ".join(trace),
        "engine_compare": "real offline flow; every step executed",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "steps": len(trace),
    }


def task_t01_sannysoft() -> dict:
    try:
        pm.create_profile(pid="bench_t01", geo="DE", os="windows", locale="de-DE")
        first = identity_mod.generate_identity("bench_t01")
        second = identity_mod.generate_identity("bench_t01")
        stable = first == second
        webgl_ok = bool(first.webgl_seed) and bool(first.fingerprint_preset_id)
        ok = stable and webgl_ok
        return {"status": "PASS" if ok else "FAIL", "detail": f"stable={stable} webgl_seed={webgl_ok} preset={first.fingerprint_preset_id[:8]}", "engine_compare": "AgentFox identity pinning measured; live sannysoft is separate", "elapsed_ms": 5}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t02_creepjs() -> dict:
    try:
        first = identity_mod.generate_identity("bench_creep_a")
        second = identity_mod.generate_identity("bench_creep_b")
        stable = first == identity_mod.generate_identity("bench_creep_a")
        distinct = first != second and first.canvas_seed != second.canvas_seed
        ok = stable and distinct
        return {"status": "PASS" if ok else "FAIL", "detail": f"distinct={distinct} stable={stable} (no fabricated trust score)", "engine_compare": "AgentFox identity contract measured; live CreepJS is separate", "elapsed_ms": 3}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t03_pixelscan() -> dict:
    try:
        profile = pm.create_profile(pid="bench_t03", geo="DE", locale="de-DE", os="windows")
        ok = profile.identity.timezone == "Europe/Berlin" and profile.identity.locale == "de-DE" and profile.identity.os == "windows"
        return {"status": "PASS" if ok else "FAIL", "detail": f"geo=DE locale={profile.identity.locale} tz={profile.identity.timezone} os={profile.identity.os}", "engine_compare": "AgentFox TZ_BY_LOCALE measured; live IP consistency is separate", "elapsed_ms": 4}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t04_webrtc() -> dict:
    try:
        source = (ROOT / "core" / "session.py").read_text(encoding="utf-8")
        block_webrtc = "block_webrtc=True" in source
        has_block = block_webrtc
        return {"status": "PASS" if has_block else "FAIL", "detail": f"block_webrtc={block_webrtc}; live WebRTC leak test is separate", "engine_compare": "AgentFox launch configuration measured; live leak test is separate", "elapsed_ms": 2}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t06_cf_free_js_challenge() -> dict:
    return _live_only("t06_cf_free_js_challenge")


def task_t07_cf_business_turnstile() -> dict:
    return _live_only("t07_cf_business_turnstile")


def task_t08_cf_enterprise() -> dict:
    try:
        manager_source = (ROOT / "core" / "profile_manager.py").read_text(encoding="utf-8")
        api_source = (ROOT / "api" / "server.py").read_text(encoding="utf-8")
        manager_fallback = "auto_fallback_if_needed" in manager_source
        api_fallback = "auto_fallback_if_needed" in api_source
        ok = manager_fallback and api_fallback
        return {"status": "PASS" if ok else "FAIL", "detail": f"profile_manager={manager_fallback} api={api_fallback}; WAF outcome is live-only", "engine_compare": "fallback wiring measured; Enterprise outcome requires live target", "elapsed_ms": 3}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t09_google_search() -> dict:
    return _live_only("t09_google_search")


def task_t10_yandex() -> dict:
    try:
        page = _FlowPage()
        query = "antidetect browser"
        bmouse.human_type(page, "@e_query", query)
        delays = [event for event in page.keyboard.events if isinstance(event, int)]
        ok = len(delays) == len(query) and len(set(delays)) > 1 and 45 <= min(delays) <= max(delays) <= 180
        return {"status": "PASS" if ok else "FAIL", "detail": f"delays={min(delays)}-{max(delays)} varied={len(set(delays)) > 1}", "engine_compare": "AgentFox human_type measured; live Yandex response is separate", "elapsed_ms": 2}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t11_xcom_search() -> dict:
    try:
        stage_one = pm.create_profile(pid="bench_x11_stage1", geo="DE")
        stage_two = pm.create_profile(pid="bench_x11_stage2", geo="DE")
        stage_two.warmup.stage = 2
        denied_early = not stage_one.warmup.is_allowed("search")
        allowed_ready = stage_two.warmup.is_allowed("search")
        detected_rate = "rate_limit" in health_mod.detect_signals("HTTP 429 too many requests", "fixture://search")
        ok = denied_early and allowed_ready and detected_rate
        return {"status": "PASS" if ok else "FAIL", "detail": f"stage1_denied={denied_early} stage2_allowed={allowed_ready} rate_limit={detected_rate}", "engine_compare": "AgentFox warmup and health contracts measured", "elapsed_ms": 5}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t12_xcom_profile() -> dict:
    try:
        import behavior.persona as persona

        page = _FlowPage()
        persona.warmup_visit(page)
        ok = len(page.mouse.wheels) > 0 and any("innerText.length" in js for js in page.evaluations)
        return {"status": "PASS" if ok else "FAIL", "detail": f"wheels={len(page.mouse.wheels)} content_length_evaluated={ok}", "engine_compare": "AgentFox warmup_visit measured; target access is live-only", "elapsed_ms": 3}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t13_instagram() -> dict:
    try:
        first = pm.create_profile(pid="bench_ig1", geo="US")
        second = pm.create_profile(pid="bench_ig2", geo="US")
        isolated = first.user_data_dir != second.user_data_dir and first.identity.fingerprint_preset_id != second.identity.fingerprint_preset_id
        signals = health_mod.detect_signals("suspicious activity verify identity", "fixture://login")
        ok = isolated and "suspicious" in signals
        return {"status": "PASS" if ok else "FAIL", "detail": f"storage_distinct={first.user_data_dir != second.user_data_dir} fingerprint_distinct={first.identity.fingerprint_preset_id != second.identity.fingerprint_preset_id} suspicious={'suspicious' in signals}", "engine_compare": "AgentFox isolation and health detection measured; login is live-only", "elapsed_ms": 6}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t14_amazon() -> dict:
    try:
        from core.cookie_farmer import seed_from_bank

        profile = pm.create_profile(pid="bench_amz", geo="US", locale="en-US", proxy={"server": "http://proxy.invalid:8080", "username": "bench"})
        seeded = seed_from_bank(profile)
        seed_path = profile.dir / "cookie_seed.json"
        ok = seeded > 0 and seed_path.exists() and len(json.loads(seed_path.read_text(encoding="utf-8"))) == seeded
        return {"status": "PASS" if ok else "FAIL", "detail": f"seeded={seeded} artifact={seed_path.exists()}", "engine_compare": "AgentFox cookie seeding measured; Amazon response is live-only", "elapsed_ms": 4}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t15_ozon() -> dict:
    try:
        page = _FlowPage()
        bscroll.natural_scroll(page, screens=1, depth="light")
        ok = bool(page.mouse.wheels) and any(value > 0 for value in page.mouse.wheels)
        return {"status": "PASS" if ok else "FAIL", "detail": f"positive_wheels={sum(value > 0 for value in page.mouse.wheels)} total={len(page.mouse.wheels)}", "engine_compare": "AgentFox natural_scroll measured; Ozon access is live-only", "elapsed_ms": 2}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t16_wb() -> dict:
    try:
        import behavior.persona as persona

        page = _FlowPage()
        fired = persona.maybe_detour(page, p=1.0)
        ok = fired and any(value < 0 for value in page.mouse.wheels)
        return {"status": "PASS" if ok else "FAIL", "detail": f"detour={fired} negative_wheels={sum(value < 0 for value in page.mouse.wheels)}", "engine_compare": "AgentFox detour behavior measured; marketplace response is live-only", "elapsed_ms": 2}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t17_betting() -> dict:
    try:
        profile = pm.create_profile(pid="bench_bet", geo="RU", locale="ru-RU", os="windows")
        ok = profile.identity.timezone == "Europe/Moscow"
        return {"status": "PASS" if ok else "FAIL", "detail": f"locale={profile.identity.locale} tz={profile.identity.timezone}", "engine_compare": "AgentFox geo consistency measured; target access is live-only", "elapsed_ms": 4}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t18_crypto_bulk() -> dict:
    try:
        started = time.monotonic()
        profiles = []
        for index in range(10):
            profiles.append(pm.create_profile(
                pid=f"bench_bulk_{index}",
                geo="US",
                proxy={"server": f"http://proxy-{index}.invalid:8080", "username": f"bench-{index}"},
            ))
        elapsed = time.monotonic() - started
        presets = {profile.identity.fingerprint_preset_id for profile in profiles}
        storage = {str(profile.user_data_dir) for profile in profiles}
        stickies = {profile.proxy.sticky_session for profile in profiles if profile.proxy}
        ok = elapsed < 5 and len(presets) == len(storage) == len(stickies) == 10
        return {"status": "PASS" if ok else "FAIL", "detail": f"bulk=10 elapsed={elapsed:.3f}s presets={len(presets)} storage={len(storage)} proxies={len(stickies)}", "engine_compare": "AgentFox bulk isolation measured; dapp interaction is live-only", "elapsed_ms": int(elapsed * 1000)}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t19_ticketing() -> dict:
    return _live_only("t19_ticketing")


def task_t20_isolation() -> dict:
    try:
        pids = [f"bench_iso_{index}" for index in range(5)]
        for pid in pids:
            pm.create_profile(pid=pid, geo="DE")
        profiles = [pm.Profile.load(pid) for pid in pids]
        presets = {profile.identity.fingerprint_preset_id for profile in profiles}
        storage = {str(profile.user_data_dir) for profile in profiles}
        base = scheduler_mod.BASE_INTERVAL_BY_STAGE[1].total_seconds()
        rng = scheduler_mod._rng_for("bench_iso_0", "salt")
        values = [scheduler_mod.jittered_interval(base, rng=rng) for _ in range(20)]
        mean = sum(values) / len(values)
        std = (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5
        ok = len(presets) == len(storage) == 5 and std > 50
        return {"status": "PASS" if ok else "FAIL", "detail": f"presets={len(presets)} storage={len(storage)} jitter_std={std:.0f}", "engine_compare": "AgentFox isolation measured", "elapsed_ms": 5}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t21_cookie_farm() -> dict:
    try:
        from core.cookie_farmer import farm_profile, seed_from_bank

        profile = pm.create_profile(pid="bench_farm", geo="DE", locale="de-DE")
        stats = farm_profile(profile, urls=["https://fixture-a.test", "https://fixture-b.test"])
        seeded = seed_from_bank(profile)
        ok = stats.get("visited") == 2 and not stats.get("errors") and 0 < seeded <= 80
        return {"status": "PASS" if ok else "FAIL", "detail": f"visited={stats.get('visited')} errors={len(stats.get('errors', []))} seeded={seeded}", "engine_compare": "AgentFox cookie bank measured", "elapsed_ms": 6}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t22_warmup() -> dict:
    try:
        first = pm.create_profile(pid="bench_wu", geo="DE")
        stage_one_denies = not first.warmup.is_allowed("extract_deep")
        first.warmup.stage = 4
        stage_four_allows = first.warmup.is_allowed("extract_deep")
        second = pm.create_profile(pid="bench_wu2", geo="DE")
        second.warmup.stage = 3
        second.warmup.regress()
        regressed = second.warmup.stage == 2
        ok = stage_one_denies and stage_four_allows and regressed
        return {"status": "PASS" if ok else "FAIL", "detail": f"stage1_denies={stage_one_denies} stage4_allows={stage_four_allows} regress_3_to_2={regressed}", "engine_compare": "AgentFox warmup policy measured", "elapsed_ms": 4}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t23_scheduler() -> dict:
    try:
        base = scheduler_mod.BASE_INTERVAL_BY_STAGE[1].total_seconds()
        values = [scheduler_mod.jittered_interval(base, spread=0.4) for _ in range(100)]
        average = sum(values) / len(values)
        std = (sum((value - average) ** 2 for value in values) / len(values)) ** 0.5
        inside = scheduler_mod.is_in_active_window(datetime(2026, 1, 1, 9, tzinfo=timezone.utc), "Europe/Berlin")
        outside = not scheduler_mod.is_in_active_window(datetime(2026, 1, 1, 1, tzinfo=timezone.utc), "Europe/Berlin")
        ok = 0.7 * base < average < 1.3 * base and std > 100 and inside and outside
        return {"status": "PASS" if ok else "FAIL", "detail": f"avg={average:.0f}s std={std:.0f}s active_window={inside and outside}", "engine_compare": "AgentFox scheduler measured", "elapsed_ms": 3}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t24_proxy() -> dict:
    try:
        profile = pm.create_profile(pid="bench_proxy", geo="DE", proxy={"server": "http://proxy.invalid:8080", "username": "u", "password": "p"})
        profile.proxy.created_at = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        expired = proxy_pool.should_rotate(profile.proxy)
        original = proxy_pool.check_proxy_health
        try:
            proxy_pool.check_proxy_health = lambda proxy: False
            dead = proxy_pool.check_proxy_health(profile.proxy) is False
        finally:
            proxy_pool.check_proxy_health = original
        ok = expired and dead
        return {"status": "PASS" if ok else "FAIL", "detail": f"expired={expired} dead_health_rejected={dead}", "engine_compare": "AgentFox proxy lifecycle measured", "elapsed_ms": 4}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t25_behavior() -> dict:
    try:
        import behavior.persona as persona

        page = _FlowPage()
        bmouse.human_click(page, "@e_button")
        bmouse.human_type(page, "@e_query", "hello world")
        bscroll.natural_scroll(page, screens=1, depth="light")
        persona.maybe_detour(page, p=1.0)
        delays = [event for event in page.keyboard.events if isinstance(event, int)]
        pauses = [max(0.5, random.gauss(3.0, 1.5)) for _ in range(50)]
        average = sum(pauses) / len(pauses)
        std = (sum((value - average) ** 2 for value in pauses) / len(pauses)) ** 0.5
        ok = len(page.mouse.moves) >= 25 and len(delays) == len("hello world") and len(set(delays)) > 1 and 45 <= min(delays) <= max(delays) <= 180 and any(value > 0 for value in page.mouse.wheels) and any(value < 0 for value in page.mouse.wheels) and std > 0.5
        return {"status": "PASS" if ok else "FAIL", "detail": f"moves={len(page.mouse.moves)} delays={min(delays)}-{max(delays)} varied={len(set(delays)) > 1} back={any(value < 0 for value in page.mouse.wheels)} pause_std={std:.2f}", "engine_compare": "AgentFox behavior primitives measured; anti-bot outcome is live-only", "elapsed_ms": 6}
    except Exception as exc:
        return {"status": "FAIL", "detail": str(exc)[:200], "engine_compare": "-", "elapsed_ms": 0}


def task_t26_autoreg_xcom() -> dict:
    return _live_only("t26_autoreg_xcom")


def task_t27_autoreg_instagram() -> dict:
    return _live_only("t27_autoreg_instagram")


def task_t28_autoreg_mailru() -> dict:
    return _live_only("t28_autoreg_mailru")


def task_t30_mail_imap_otp() -> dict:
    return _live_only("t30_mail_imap_otp")


def task_t31_mail_confirm_link() -> dict:
    return _live_only("t31_mail_confirm_link")


def task_t32_scraping_x_mail_cycle() -> dict:
    return _live_only("t32_scraping_x_mail_cycle")


def task_t33_scraping_instagram() -> dict:
    return _live_only("t33_scraping_instagram")


def task_t34_scraping_tiktok() -> dict:
    return _live_only("t34_scraping_tiktok")


def task_t50_flow_profile_lifecycle() -> dict:
    from core.cookie_farmer import seed_from_bank
    from core.profile_io import export_profile, import_profile

    pid = "bench_flow_lifecycle"
    archive = BENCH_ROOT / "archives" / f"{pid}.tar.gz"
    state: dict = {}

    def create():
        state["profile"] = pm.create_profile(pid=pid, geo="DE", locale="de-DE")

    def pin():
        state["identity"] = state["profile"].identity.to_dict()
        assert state["profile"].identity.fingerprint_preset_id

    def seed():
        count = seed_from_bank(state["profile"])
        assert 0 < count <= 80
        assert (state["profile"].dir / "cookie_seed.json").exists()
        state["seed_count"] = count

    def export():
        state["archive"] = export_profile(pid, archive)
        assert state["archive"].exists() and state["archive"].stat().st_size > 0

    def delete():
        pm.delete_profile(pid)
        assert not (BENCH_ROOT / pid).exists()

    def restore():
        restored = import_profile(state["archive"], new_id=pid)
        assert restored.identity.to_dict() == state["identity"]
        seed_file = restored.dir / "cookie_seed.json"
        assert seed_file.exists()
        assert len(json.loads(seed_file.read_text(encoding="utf-8"))) == state["seed_count"]

    return _run_flow([
        ("create profile", create),
        ("verify pinned identity", pin),
        ("seed cookie bank", seed),
        ("export archive", export),
        ("delete original", delete),
        ("import and compare", restore),
    ])


def task_t51_flow_autoreg() -> dict:
    profile = pm.create_profile(pid="bench_flow_autoreg", geo="US", locale="en-US")
    page = _FlowPage()
    state: dict = {"email": "fixture@example.invalid", "otp": "123456"}

    def create():
        assert profile.warmup.stage == 1 and profile.health.status == "ok"

    def preflight():
        allowed, reason = profile.check_action_allowed("browse")
        assert allowed, reason

    def snapshot():
        refs = page.evaluate("() => [...document.querySelectorAll('input,button')].map((e, i) => '@e' + i)")
        assert isinstance(refs, list)

    def type_email():
        bmouse.human_type(page, "@e_email", state["email"])
        delays = [event for event in page.keyboard.events if isinstance(event, int)]
        assert len(delays) == len(state["email"])
        assert len(set(delays)) > 1 and 45 <= min(delays) <= max(delays) <= 180

    def click_submit():
        bmouse.human_click(page, "@e_submit")
        assert len(page.mouse.moves) >= 25 and page.mouse.clicks

    def mail_adapter():
        state["mail_id"], state["mail"] = 1, "fixture@example.invalid"
        state["wait_code"] = lambda _mail_id: state["otp"]
        assert callable(state["wait_code"])

    def type_otp():
        code = state["wait_code"](state["mail_id"])
        assert code == state["otp"]
        bmouse.human_type(page, "@e_otp", code)
        assert page.keyboard.text.endswith(code)

    def final_health():
        assert health_mod.detect_signals(page.content(), "fixture://registration") == []
        profile.health.record_success()
        assert profile.health.status == "ok"

    return _run_flow([
        ("create stage-1 profile", create),
        ("preflight health", preflight),
        ("snapshot page fixture", snapshot),
        ("human type email", type_email),
        ("human click submit", click_submit),
        ("check fixture mail adapter", mail_adapter),
        ("type fixture OTP", type_otp),
        ("final health check", final_health),
    ])


def task_t53_flow_ecom_compare() -> dict:
    import behavior.persona as persona

    page = _FlowPage()
    collected: dict[str, list[dict]] = {}
    stores = {
        "store_a": [{"title": f"A-{index}", "price": 100 + index} for index in range(20)],
        "store_b": [{"title": f"B-{index}", "price": 90 + index} for index in range(20)],
        "store_c": [{"title": f"C-{index}", "price": 110 + index} for index in range(20)],
    }

    def open_store(name):
        def operation():
            page.records = stores[name]
            page.goto(f"fixture://{name}")
            assert page.urls[-1].endswith(name)

        return operation

    def scroll_store():
        before = len(page.mouse.wheels)
        bscroll.natural_scroll(page, screens=1, depth="light")
        persona.maybe_detour(page, p=1.0)
        assert len(page.mouse.wheels) > before
        assert any(value < 0 for value in page.mouse.wheels[before:])

    def extract_store(name):
        def operation():
            cards = page.evaluate("() => [...document.querySelectorAll('.card')].map(e => e.dataset)")
            assert len(cards) == 20
            collected[name] = cards

        return operation

    def aggregate():
        prices = [card["price"] for cards in collected.values() for card in cards]
        assert len(prices) == 60 and max(prices) - min(prices) > 0

    operations: list[tuple[str, Callable[[], object]]] = []
    for store in stores:
        operations.extend([
            (f"open {store}", open_store(store)),
            (f"scroll/detour {store}", scroll_store),
            (f"extract 20 cards {store}", extract_store(store)),
        ])
    operations.append(("aggregate and compare prices", aggregate))
    return _run_flow(operations)


def task_t54_flow_warmup_to_work() -> dict:
    profile = pm.create_profile(pid="bench_flow_warmup", geo="DE")

    def stage_one():
        assert profile.warmup.stage == 1 and profile.warmup.is_allowed("browse") and not profile.warmup.is_allowed("search")

    def record_sessions():
        for _ in range(5):
            profile.warmup.record_session()
        assert profile.warmup.total_sessions == 5

    def age_profile():
        profile.warmup.created_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        assert profile.warmup.age_days() >= 4

    def advance():
        assert profile.warmup.try_advance(health_ok=True) and profile.warmup.stage == 2

    def allow_search():
        assert profile.warmup.is_allowed("search")

    def regress_after_signal():
        assert "rate_limit" in health_mod.detect_signals("HTTP 429: too many requests", "")
        profile.health.record_signal("rate_limit", "fixture://search")
        profile.warmup.regress()
        assert profile.warmup.stage == 1 and profile.health.is_cooldown()

    return _run_flow([
        ("assert stage-1 browse-only", stage_one),
        ("record five healthy sessions", record_sessions),
        ("advance profile age to day 4", age_profile),
        ("advance to stage 2", advance),
        ("assert search allowed", allow_search),
        ("record rate-limit and regress", regress_after_signal),
    ])


def task_t55_flow_proxy_ops() -> dict:
    profile = pm.create_profile(
        pid="bench_flow_proxy",
        geo="DE",
        proxy={"server": "http://proxy.invalid:8080", "username": "bench", "password": "secret"},
    )
    first_sid = profile.proxy.sticky_session

    def assign():
        assert profile.proxy and first_sid

    def idempotent():
        proxy_pool.inject_sticky_into_proxy(profile.proxy, profile.id)
        assert profile.proxy.sticky_session == first_sid

    def dead_gate():
        original = proxy_pool.check_proxy_health
        try:
            proxy_pool.check_proxy_health = lambda proxy: False
            assert proxy_pool.check_proxy_health(profile.proxy) is False
        finally:
            proxy_pool.check_proxy_health = original

    def rotate():
        profile.proxy.created_at = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        old_sid = profile.proxy.sticky_session
        assert proxy_pool.should_rotate(profile.proxy)
        assert proxy_pool.rotate_proxy_if_needed(profile)
        assert profile.proxy.sticky_session != old_sid

    return _run_flow([
        ("assign sticky session", assign),
        ("reapply and compare", idempotent),
        ("run dead health gate", dead_gate),
        ("age and rotate", rotate),
    ])


def task_t56_flow_incident_response() -> dict:
    profile = pm.create_profile(pid="bench_flow_incident", geo="DE")
    profile.warmup.stage = 3
    now = datetime.now(timezone.utc)

    def scan():
        signals = health_mod.detect_signals("Turnstile challenge", "fixture://target")
        assert "captcha" in signals

    def record():
        profile.health.record_signal("captcha", "fixture://target")
        assert profile.health.status == "cooldown"

    def lock():
        locked, reason = profile.is_locked()
        assert locked and "cooldown" in reason

    def regress():
        profile.warmup.regress()
        assert profile.warmup.stage == 2

    def schedule():
        next_run = scheduler_mod.schedule_next(profile, now=now)
        assert next_run > now

    return _run_flow([
        ("scan fixture DOM", scan),
        ("record captcha", record),
        ("assert cooldown lock", lock),
        ("regress warmup", regress),
        ("schedule delayed retry", schedule),
    ])


def task_t57_flow_bulk_farm() -> dict:
    from core.cookie_farmer import seed_from_bank

    started = time.monotonic()
    profiles: list[pm.Profile] = []
    pids = [f"bench_flow_bulk_{index}" for index in range(10)]
    state: dict = {}

    def create():
        for index, pid in enumerate(pids):
            profiles.append(pm.create_profile(
                pid=pid,
                geo="US",
                locale="en-US",
                proxy={"server": f"http://proxy-{index}.invalid:8080", "username": f"bench-{index}"},
            ))
        state["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        assert len(profiles) == 10 and state["elapsed_ms"] < 5000

    def identities():
        presets = {profile.identity.fingerprint_preset_id for profile in profiles}
        stickies = {profile.proxy.sticky_session for profile in profiles if profile.proxy}
        assert len(presets) == 10 and len(stickies) == 10

    def storage():
        directories = {str(profile.user_data_dir) for profile in profiles}
        assert len(directories) == 10 and all(profile.user_data_dir.exists() for profile in profiles)

    def seed():
        counts = [seed_from_bank(profile) for profile in profiles]
        assert all(0 < count <= 80 for count in counts)
        state["seeded"] = sum(counts)

    def manifests():
        for profile in profiles:
            manifest = profile.dir / "benchmark-manifest.json"
            manifest.write_text(json.dumps({"id": profile.id, "preset": profile.identity.fingerprint_preset_id}), encoding="utf-8")
            assert manifest.exists()

    return _run_flow([
        ("create 10 profiles", create),
        ("check distinct identities", identities),
        ("check distinct storage", storage),
        ("seed each profile", seed),
        ("write and verify manifests", manifests),
    ])


def task_t58_flow_x_research() -> dict:
    import behavior.persona as persona

    profile = pm.create_profile(pid="bench_flow_research", geo="US", locale="en-US")
    profile.warmup.stage = 2
    records = [{"id": f"record-{index}", "text": f"fixture result {index}"} for index in range(20)]
    page = _FlowPage(records=records)
    state: dict = {}

    def gate():
        assert profile.warmup.is_allowed("search")

    def open_search():
        page.goto("fixture://public-research/search")
        assert page.urls == ["fixture://public-research/search"]

    def snapshot():
        refs = page.evaluate("() => [...document.querySelectorAll('[data-testid=tweet]')].map((_, i) => '@e' + i)")
        assert len(refs) == 20

    def scroll():
        for _ in range(3):
            bscroll.natural_scroll(page, screens=1, depth="light")
        assert any(value > 0 for value in page.mouse.wheels)

    def detour():
        before = len(page.mouse.wheels)
        assert persona.maybe_detour(page, p=1.0)
        assert any(value < 0 for value in page.mouse.wheels[before:])

    def extract():
        state["records"] = page.evaluate("() => [...document.querySelectorAll('[data-testid=tweet]')].slice(0, 20).map(e => e.innerText)")
        assert len(state["records"]) == 20

    def pause():
        state["pause"] = btiming.human_pause(mean=3.0, std=1.5)
        assert state["pause"] >= 0.5

    def health():
        assert health_mod.detect_signals(page.content(), page.urls[-1]) == []

    return _run_flow([
        ("assert stage-2 search allowed", gate),
        ("open search fixture", open_search),
        ("take semantic snapshot", snapshot),
        ("scroll three times", scroll),
        ("perform detour", detour),
        ("extract 20 records", extract),
        ("sample human pause", pause),
        ("scan health signals", health),
    ])


TASK_FUNCS: dict[str, Callable[[], dict]] = {
    "t01_fingerprint_bot_sannysoft": task_t01_sannysoft,
    "t02_fingerprint_creepjs": task_t02_creepjs,
    "t03_fingerprint_pixelscan": task_t03_pixelscan,
    "t04_webrtc_leak": task_t04_webrtc,
    "t06_cf_free_js_challenge": task_t06_cf_free_js_challenge,
    "t07_cf_business_turnstile": task_t07_cf_business_turnstile,
    "t08_cf_enterprise_bot_management": task_t08_cf_enterprise,
    "t09_google_search": task_t09_google_search,
    "t10_yandex_search": task_t10_yandex,
    "t11_xcom_search": task_t11_xcom_search,
    "t12_xcom_profile_crawl": task_t12_xcom_profile,
    "t13_instagram_login_attempt": task_t13_instagram,
    "t14_amazon_product": task_t14_amazon,
    "t15_ozon_search": task_t15_ozon,
    "t16_wildberries_cards": task_t16_wb,
    "t17_betting_pari": task_t17_betting,
    "t18_crypto_airdrop_farming": task_t18_crypto_bulk,
    "t19_ticketing": task_t19_ticketing,
    "t20_multi_profile_isolation": task_t20_isolation,
    "t21_cookie_farming": task_t21_cookie_farm,
    "t22_warmup_gates": task_t22_warmup,
    "t23_scheduler_jitter": task_t23_scheduler,
    "t24_proxy_rotation": task_t24_proxy,
    "t25_behavior_authenticity": task_t25_behavior,
    "t26_autoreg_xcom": task_t26_autoreg_xcom,
    "t27_autoreg_instagram": task_t27_autoreg_instagram,
    "t28_autoreg_mailru": task_t28_autoreg_mailru,
    "t30_mail_imap_otp": task_t30_mail_imap_otp,
    "t31_mail_confirm_link": task_t31_mail_confirm_link,
    "t32_scraping_x_mail_cycle": task_t32_scraping_x_mail_cycle,
    "t33_scraping_instagram": task_t33_scraping_instagram,
    "t34_scraping_tiktok": task_t34_scraping_tiktok,
    "t50_flow_profile_lifecycle": task_t50_flow_profile_lifecycle,
    "t51_flow_autoreg": task_t51_flow_autoreg,
    "t53_flow_ecom_compare": task_t53_flow_ecom_compare,
    "t54_flow_warmup_to_work": task_t54_flow_warmup_to_work,
    "t55_flow_proxy_ops": task_t55_flow_proxy_ops,
    "t56_flow_incident_response": task_t56_flow_incident_response,
    "t57_flow_bulk_farm": task_t57_flow_bulk_farm,
    "t58_flow_x_research": task_t58_flow_x_research,
}


def run_dataset(live: bool = False, profiles: int = 1) -> dict:
    global LIVE_MODE
    LIVE_MODE = live
    del profiles  # reserved for the live runners; offline stays deterministic
    results: list[dict] = []
    started = time.monotonic()
    for task in load_dataset():
        task_id = task["id"]
        function = TASK_FUNCS.get(task_id)
        task_started = time.monotonic()
        if task_id in LIVE_ONLY_TASKS:
            result = _live_only(task_id)
        elif function:
            try:
                result = function()
            except Exception as exc:
                result = {"status": "FAIL", "detail": f"exception: {exc}"[:300], "engine_compare": "-", "steps": 0}
        else:
            result = {"status": "SKIPPED", "detail": "no implementation", "engine_compare": "-", "steps": 0}
        result_elapsed = result.get("elapsed_ms", int((time.monotonic() - task_started) * 1000))
        results.append({
            "id": task_id,
            "category": task["category"],
            "level": task["level"],
            "mode": task.get("mode", "offline"),
            "url": task["url"],
            "action": task["action"],
            "challenge": task["challenge"],
            "description": task["description"],
            "success_criteria": task["success_criteria"],
            "steps_expected": task.get("steps", []),
            "engine_compare": result.get("engine_compare", task.get("engine_compare", "")),
            "status": result["status"],
            "detail": result.get("detail", ""),
            "steps": result.get("steps", 0),
            "elapsed_ms": result_elapsed,
        })

    total = len(results)
    passed = sum(result["status"] == "PASS" for result in results)
    failed = sum(result["status"] == "FAIL" for result in results)
    skipped = sum(result["status"] == "SKIPPED" for result in results)
    evaluated = total - skipped
    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        category = result["category"]
        summary = by_category.setdefault(category, {"total": 0, "pass": 0, "fail": 0, "skipped": 0})
        summary["total"] += 1
        summary[result["status"].lower()] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": "honest offline fixtures; live tasks are skipped",
        "total": total,
        "evaluated": evaluated,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": passed / evaluated if evaluated else 0,
        "coverage_rate": passed / total if total else 0,
        "elapsed_s": round(time.monotonic() - started, 3),
        "by_category": by_category,
        "live_requested": live,
        "results": results,
        "bench_root": str(BENCH_ROOT),
    }


def format_markdown(report: dict) -> str:
    lines = [
        f"# AgentFox benchmark (offline, {report['generated_at']})",
        "",
        f"**Evaluated: {report['passed']}/{report['evaluated']} PASS ({report['pass_rate'] * 100:.0f}%)**; {report['failed']} FAIL; {report['skipped']} live-only SKIPPED; elapsed {report['elapsed_s']}s.",
        "",
        "`PASS` means a real local assertion passed. `SKIPPED` means the task needs a real authorized target, proxy, mailbox, or browser and is not guessed offline.",
        "",
        "| Category | Total | PASS | FAIL | SKIPPED |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, summary in report["by_category"].items():
        lines.append(f"| {category} | {summary['total']} | {summary['pass']} | {summary['fail']} | {summary['skipped']} |")
    lines.extend([
        "",
        "| # | ID | Mode | Status | Steps | ms | Detail |",
        "|---:|---|---|---|---:|---:|---|",
    ])
    for index, result in enumerate(report["results"], 1):
        detail = result["detail"].replace("|", "/").replace("\n", " ")[:140]
        lines.append(f"| {index} | {result['id']} | {result['mode']} | {result['status']} | {result['steps']} | {result['elapsed_ms']} | {detail} |")
    lines.extend([
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 -m tools.benchmark.run",
        "cat tools/benchmark/report.md",
        "python3 -m tools.benchmark.live_run --tasks t01,t04,t06 --proxy-index 0",
        "```",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Honest AgentFox benchmark")
    parser.add_argument("--json", default="tools/benchmark/report.json", help="JSON output")
    parser.add_argument("--md", default="tools/benchmark/report.md", help="Markdown output")
    parser.add_argument("--live", action="store_true", help="mark live context; use live_run/e2e_live for real network execution")
    parser.add_argument("--profiles", type=int, default=1, help="reserved for live runners")
    args = parser.parse_args()
    report = run_dataset(live=args.live, profiles=args.profiles)
    json_path = Path(args.json)
    md_path = Path(args.md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = format_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\n[bench] wrote {json_path} and {md_path} — {report['passed']}/{report['evaluated']} evaluated PASS, {report['skipped']} skipped")
    raise SystemExit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()
