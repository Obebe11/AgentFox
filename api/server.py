"""
FastAPI — единственная точка входа агента. Агент никогда не касается браузера напрямую.
"""
from __future__ import annotations

import base64
import os
import random
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.cookie_farmer import farm_profile, seed_from_bank
from core.health import detect_signals
from core.metrics import get_health_series, get_overall_stats, get_per_profile, get_per_target, get_recent_events, get_success_rate, record_event
from core.profile_manager import Profile, auto_fallback_if_needed, create_profile, delete_profile, list_profiles, list_trash, restore_profile, switch_profile_engine
from core.proxy_pool import check_proxy_health, rotate_proxy_if_needed
from core.scheduler import BASE_INTERVAL_BY_STAGE, _rng_for, check_inactivity, next_run_after, should_run
from core.session import get_engine
from behavior.persona import maybe_detour, warmup_visit
from behavior.scroll import natural_scroll
from behavior.timing import human_pause

app = FastAPI(title="AgentFox", version="0.1.0", description="AdsPower for AI agents — anti-detect browser API")


def _log_history(p: Profile, action: str, target: str = "", extra: dict | None = None) -> None:
    try:
        p.append_history(action, target, extra)
    except Exception:
        pass

# In-memory sessions: sid -> {profile_id, engine, page}
_sessions: dict[str, dict[str, Any]] = {}


# --- models ---

class CreateProfileIn(BaseModel):
    id: str
    os: Optional[str] = None
    locale: Optional[str] = None
    geo: str = "DE"
    proxy: Optional[dict] = None
    targets: Optional[list[str]] = None
    engine: str = "firefox"


class BulkCreateIn(BaseModel):
    count: int = Field(..., ge=1, le=100, description="number of profiles to create (1..100)")
    geo: str = "DE"
    os: Optional[str] = None
    locale: Optional[str] = None
    proxy: Optional[dict] = None
    targets: Optional[list[str]] = None
    engine: str = "firefox"
    prefix: str = "auto"


class SessionStartIn(BaseModel):
    headless: bool = True


class GotoIn(BaseModel):
    url: str
    wait_until: str = "domcontentloaded"
    timeout: int = 30000
    read: bool = False
    snapshot: bool = False  # агентский: вернуть snapshot в том же вызове (1 токен вместо 2)
    compact: bool = False  # 30×3 поля vs 80×5 (60% экономия)


class ClickIn(BaseModel):
    selector: str
    human: bool = True
    timeout: int = 10000


class TypeIn(BaseModel):
    selector: str
    text: str
    clear: bool = True


class ScrollIn(BaseModel):
    screens: int = 2
    depth: str = "light"
    detour: float = 0.2


class ExtractIn(BaseModel):
    js: Optional[str] = None
    selector: Optional[str] = None


class EvaluateIn(BaseModel):
    code: str


class CdpIn(BaseModel):
    method: str
    params: Optional[dict] = None


class SwitchEngineIn(BaseModel):
    engine: str  # firefox | chromium
    reset_warmup: bool = True


# --- profiles ---

@app.get("/profiles")
def api_list_profiles():
    return list_profiles()


@app.post("/profiles", status_code=201)
def api_create_profile(inp: CreateProfileIn):
    try:
        p = create_profile(
            pid=inp.id,
            os=inp.os,
            locale=inp.locale,
            geo=inp.geo,
            proxy=inp.proxy,
            targets=inp.targets,
            engine=inp.engine,
        )
        seeded = seed_from_bank(p)
        return {**p.to_dict(), "seeded_cookies": seeded}
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/profiles/bulk", status_code=201)
def api_bulk_create(inp: BulkCreateIn):
    """
    Batch Profiles — ADS Power: 100 профилей 1 кликом для агента.
    Creates `count` profiles with ids f"{prefix}_{i}" (plus random suffix on collision).
    Atomic per profile: if one fails, continue others, return errors.
    Uses seed_from_bank for each, respects existing ids and locks.
    """
    from core.profile_manager import PROFILES_ROOT as _ROOT

    created: list[dict] = []
    errors: list[dict] = []

    for i in range(1, inp.count + 1):
        base_pid = f"{inp.prefix}_{i}"
        pid = base_pid
        # handle existing id collision — try random suffix up to 5 attempts
        attempts = 0
        while (_ROOT / pid).exists() and (_ROOT / pid / "meta.json").exists():
            if attempts >= 5:
                break
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            pid = f"{base_pid}_{suffix}"
            attempts += 1
        if (_ROOT / pid).exists() and (_ROOT / pid / "meta.json").exists():
            errors.append({"index": i, "id": pid, "error": f"profile {pid} already exists"})
            continue

        # respect locks: if a profile with same id somehow locked (zombie), treat as error
        # per-profile proxy handling is delegated to create_profile (sticky injection)
        try:
            p = create_profile(
                pid=pid,
                os=inp.os,
                locale=inp.locale,
                geo=inp.geo,
                proxy=inp.proxy,
                targets=inp.targets,
                engine=inp.engine,
            )
            # respect locks after creation (should be unlocked); seed cookies
            try:
                seeded = seed_from_bank(p)
            except Exception:
                seeded = 0
            created.append({**p.to_dict(), "seeded_cookies": seeded})
        except FileExistsError as e:
            errors.append({"index": i, "id": pid, "error": str(e)})
        except Exception as e:
            errors.append({"index": i, "id": pid, "error": str(e)})

    return {"created": created, "errors": errors, "total": len(created)}


@app.get("/profiles/trash/list")
def api_list_trash():
    return {"trash": list_trash()}


@app.get("/profiles/{pid}")
def api_get_profile(pid: str):
    try:
        return Profile.load(pid).to_dict()
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")


@app.delete("/profiles/{pid}")
def api_delete_profile(pid: str, purge: bool = True):
    # закрыть сессии профиля
    for sid, sess in list(_sessions.items()):
        if sess["profile_id"] == pid:
            try:
                sess["engine"].close()
            except Exception:
                pass
            _sessions.pop(sid, None)
    try:
        delete_profile(pid, purge_data=purge)
        return {"ok": True, "purged": purge, "trash": list_trash() if not purge else []}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/profiles/{pid}/restore")
def api_restore_profile(pid: str):
    try:
        p = restore_profile(pid)
        return p.to_dict()
    except FileNotFoundError:
        raise HTTPException(404, "profile not found in trash")
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/profiles/{pid}/engine")
def api_switch_engine(pid: str, inp: SwitchEngineIn):
    try:
        p = switch_profile_engine(pid, inp.engine, reset_warmup=inp.reset_warmup)
        return p.to_dict()
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(423, str(e))


@app.post("/profiles/{pid}/farm")
def api_farm(pid: str):
    try:
        p = Profile.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")
    stats = farm_profile(p)
    return stats


@app.get("/health/{pid}")
def api_health(pid: str):
    try:
        p = Profile.load(pid)
        return {"warmup": p.warmup.to_dict(), "health": p.health.to_dict(), "locked": p.is_locked()[0]}
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")


# --- sessions ---

@app.post("/sessions/{pid}/start")
def api_start_session(pid: str, inp: SessionStartIn = SessionStartIn()):
    try:
        p = Profile.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")

    # H3: auto-fallback before gates — если health degraded/banned на firefox, переключаем на chromium
    try:
        auto_fallback_if_needed(p)
    except Exception:
        pass

    # гейты
    locked, reason = p.is_locked()
    if locked:
        raise HTTPException(423, f"profile locked: {reason}")

    # H8: scheduler gate — jitter / active-window / min_gap + inactivity regress
    now = datetime.now(timezone.utc)
    try:
        check_inactivity(p, now)
    except Exception:
        pass
    ok, gate_reason = should_run(p, now)
    if not ok:
        raise HTTPException(423, gate_reason)
    # next_run_after jitter gate (deterministic) — дополнительно к should_run min_gap
    if p.warmup.last_session_at:
        try:
            last = datetime.fromisoformat(p.warmup.last_session_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            base = BASE_INTERVAL_BY_STAGE.get(p.warmup.stage, BASE_INTERVAL_BY_STAGE[4])
            salt = p.warmup.last_session_at or p.warmup.created_at or now.isoformat()
            rng = _rng_for(p.id, salt)
            nxt = next_run_after(last, base, rng=rng)
            from core.scheduler import next_active_time

            nxt = next_active_time(nxt, p.identity.timezone)
            if now < nxt:
                raise HTTPException(423, f"too soon: next run at {nxt.isoformat()} (jitter)")
        except HTTPException:
            raise
        except Exception:
            pass

    # warmup гейт — разрешаем browse для старта (should_run уже проверил, оставляем для совместимости)
    if not p.warmup.is_allowed("browse"):
        raise HTTPException(423, f"warmup stage {p.warmup.stage} — browsing not yet allowed")

    sid = f"sess_{pid}_{int(time.time()*1000)}"
    if not p.acquire(sid):
        raise HTTPException(423, "profile busy")

    # proxy auto-rotation (rotate_after 14d) + health-gate
    if p.proxy:
        try:
            if rotate_proxy_if_needed(p):
                p.save()
        except Exception as e:
            print(f"[proxy] rotate failed: {e}")
        try:
            ok = check_proxy_health(p.proxy)
            if not ok:
                p.release()
                raise HTTPException(423, f"proxy health check failed for {p.proxy.server}")
        except HTTPException:
            raise
        except Exception as e:
            print(f"[proxy] health check error (ignored): {e}")

    engine = get_engine(p)
    try:
        page = engine.launch(p, headless=inp.headless)
    except Exception as e:
        p.release()
        raise HTTPException(500, f"launch failed: {e}")

    _sessions[sid] = {"profile_id": pid, "engine": engine, "page": page, "profile": p}
    # warmup учёт
    p.warmup.record_session()
    p.health.total_sessions += 1
    _log_history(p, "session_start", "", {"session_id": sid, "engine": p.engine})
    p.save()
    return {"session_id": sid, "profile_id": pid, "engine": p.engine}


def _check_signals(p: Profile, page, url: str = "") -> list[str]:
    """Общий детект сигналов палева по DOM. При находке — cooldown + регрессия warmup."""
    try:
        text = page.content()[:8000]
        hits = detect_signals(text, url)
        for sig in hits:
            p.health.record_signal(sig, url)
            if sig in ("captcha", "blocked", "suspicious", "rate_limit"):
                p.warmup.regress()
        if hits:
            p.save()
            # H3: если есть blocked/suspicious и health degraded/banned — auto fallback firefox->chromium
            if any(h in ("blocked", "suspicious") for h in hits):
                try:
                    auto_fallback_if_needed(p)
                except Exception:
                    pass
        return hits
    except Exception:
        return []


def _resolve_selector(sess: dict, selector: str) -> str:
    """Если selector == @e123 — маппит в CSS из последнего snapshot, иначе как есть."""
    if selector.startswith("@e"):
        refs = sess.get("snapshot_refs", {})
        css = refs.get(selector)
        if css:
            return css
    return selector


def _generate_snapshot(page, compact: bool = False) -> tuple[list[dict], str, str]:
    """Возвращает (tree, url, title). Tree: [{ref, role, name, selector, tag}] compact: только ref/role/name 30 шт (60% токенов)."""
    url = ""
    title = ""
    try:
        url = page.url if isinstance(getattr(page, "url", ""), str) else page.evaluate("() => location.href") or ""
    except Exception:
        pass
    try:
        title = page.title() if callable(getattr(page, "title", None)) else page.evaluate("() => document.title") or ""
    except Exception:
        try:
            title = page.evaluate("() => document.title") or ""
        except Exception:
            pass
    # пробуем accessibility snapshot если есть (Playwright)
    try:
        acc = getattr(page, "accessibility", None)
        if acc and callable(getattr(acc, "snapshot", None)):
            snap = page.accessibility.snapshot()
            if isinstance(snap, dict):
                # flatten? fallback to JS
                pass
    except Exception:
        pass
    # JS сбор интерактивных элементов — работает и на FakePage
    js = r"""
() => {
  const out = [];
  let idx = 1;
  const seen = new Set();
  const push = (el, role) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    let name = (el.getAttribute("aria-label") || el.innerText || el.textContent || el.placeholder || el.name || "").trim().slice(0,80).replace(/\s+/g," ");
    if (!name) name = el.id ? "#"+el.id : tag;
    let selector = "";
    if (el.id) selector = "#"+CSS.escape(el.id);
    else if (el.getAttribute("data-testid")) selector = `[data-testid="${el.getAttribute("data-testid")}"]`;
    else if (tag && el.className && typeof el.className === "string" && el.className.trim()) {
      const cls = el.className.trim().split(/\s+/)[0];
      if (cls) selector = `${tag}.${CSS.escape(cls)}`;
      else selector = tag;
    } else selector = tag || "*";
    // уточняем селектор nth если неуникален
  try {
      if (document.querySelectorAll(selector).length > 1) {
        const all = Array.from(document.querySelectorAll(selector));
        const pos = all.indexOf(el) + 1;
        if (pos) selector = `${selector}:nth-of-type(${pos})`;
      }
    } catch(e) {}
    out.push({ref: "@e"+(idx++), role: role || el.getAttribute("role") || tag, name, selector, tag});
  };
  document.querySelectorAll('button, a[href], input, textarea, select, [contenteditable="true"], [role="button"], [role="link"], [role="textbox"]').forEach(el => push(el, ""));
  if (out.length === 0) {
    const b = document.body;
    if (b) push(b, "main");
  }
  return {tree: out.slice(0,80), url: location.href, title: document.title};
}
"""
    try:
        data = page.evaluate(js)
        if isinstance(data, dict) and "tree" in data:
            tree = data.get("tree") or []
            u = data.get("url") or url
            t = data.get("title") or title
            if compact:
                # compact: 30 элементов, только ref/role/name (60% экономия токенов)
                tree = [{k: v for k, v in item.items() if k in ("ref","role","name")} for item in tree[:30]]
            return tree, u, t
    except Exception:
        pass
    return [], url, title


@app.post("/sessions/{sid}/goto")
def api_goto(sid: str, inp: GotoIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    page = sess["page"]
    # пауза до действия как у человека
    human_pause(0.8, 0.4, 0.3)
    try:
        page.goto(inp.url, wait_until=inp.wait_until, timeout=inp.timeout)
        # дать трекерам отработать
        time.sleep(1.5)
        _log_history(p, "goto", inp.url, {"read": inp.read})
        hits = _check_signals(p, page, inp.url)
        if hits:
            _log_history(p, "goto_signal", inp.url, {"signals": hits})
            return {"ok": True, "url": inp.url, "signals": hits, "health": p.health.to_dict()}
        if inp.read:
            warmup_visit(page)
        p.health.record_success()
        p.save()
        if inp.snapshot:
            tree, url2, title = _generate_snapshot(page, compact=inp.compact)
            refs = {item["ref"]: item["selector"] for item in tree if item.get("ref") and item.get("selector")}
            # если compact — selector нет, маппим через полный snapshot fallback
            if inp.compact:
                # для compact генерим полный маппинг отдельно (не отдаём агенту)
                full_tree, _, _ = _generate_snapshot(page, compact=False)
                refs = {item["ref"]: item["selector"] for item in full_tree if item.get("ref") and item.get("selector")}
            sess["snapshot_refs"] = refs
            sess["snapshot_tree"] = tree
            return {"ok": True, "url": inp.url, "snapshot": {"tree": tree, "title": title, "url": url2}}
        return {"ok": True, "url": inp.url}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/click")
def api_click(sid: str, inp: ClickIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    page = sess["page"]
    selector = _resolve_selector(sess, inp.selector)
    try:
        if inp.human:
            from behavior.mouse import human_click

            human_click(page, selector, timeout=inp.timeout)
        else:
            page.click(selector, timeout=inp.timeout)
        human_pause(0.7, 0.35, 0.2)
        _log_history(p, "click", selector, {"human": inp.human})
        hits = _check_signals(p, page)
        if hits:
            _log_history(p, "click_signal", selector, {"signals": hits})
            return {"ok": True, "signals": hits}
        p.health.record_success()
        p.save()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/type")
def api_type(sid: str, inp: TypeIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    page = sess["page"]
    selector = _resolve_selector(sess, inp.selector)
    try:
        from behavior.mouse import human_type

        human_type(page, selector, inp.text, clear=inp.clear)
        human_pause(0.7, 0.35, 0.2)
        _log_history(p, "type", selector, {"text_len": len(inp.text), "clear": inp.clear})
        hits = _check_signals(p, page)
        if hits:
            _log_history(p, "type_signal", selector, {"signals": hits})
            return {"ok": True, "signals": hits}
        p.health.record_success()
        p.save()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/fill")
def api_fill(sid: str, inp: TypeIn):
    """A2 contenteditable-aware alias to /type — работает и для <input> и для ProseMirror/Lexical.

    Отличается от /type тем что для contenteditable делает фокус через click + keyboard,
    а не page.fill (который не триггерит input events в rich editors).
    Для агента: используй /fill когда snapshot показывает tag contenteditable или role textbox внутри div.
    """
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    page = sess["page"]
    selector = _resolve_selector(sess, inp.selector)
    try:
        from behavior.mouse import human_type

        # human_type уже contenteditable-aware (click + keyboard.type с джиттером)
        # дополнительно пробуем очистить contenteditable через evaluate если clear=True
        if inp.clear:
            try:
                # если это contenteditable — очищаем innerText перед вводом
                page.evaluate(f"""() => {{
                    const el = document.querySelector('{selector}');
                    if (el && el.getAttribute && el.getAttribute('contenteditable') === 'true') {{
                        el.focus();
                        document.execCommand('selectAll', false, null);
                    }}
                }}""")
            except Exception:
                pass
        human_type(page, selector, inp.text, clear=inp.clear)
        human_pause(0.7, 0.35, 0.2)
        _log_history(p, "fill", selector, {"text_len": len(inp.text)})
        hits = _check_signals(p, page)
        if hits:
            _log_history(p, "fill_signal", selector, {"signals": hits})
            return {"ok": True, "signals": hits}
        p.health.record_success()
        p.save()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/scroll")
def api_scroll(sid: str, inp: ScrollIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    page = sess["page"]
    try:
        natural_scroll(page, screens=inp.screens, depth=inp.depth)
        maybe_detour(page, inp.detour)
        _log_history(p, "scroll", "", {"screens": inp.screens, "detour": inp.detour})
        hits = _check_signals(p, page)
        if hits:
            _log_history(p, "scroll_signal", "", {"signals": hits})
            return {"ok": True, "signals": hits}
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sessions/{sid}/snapshot")
def api_snapshot(sid: str, compact: bool = False):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    try:
        tree, url, title = _generate_snapshot(page, compact=compact)
        # сохранить маппинг @e -> selector для click/type
        refs = {item["ref"]: item["selector"] for item in tree if item.get("ref") and item.get("selector")}
        sess["snapshot_refs"] = refs
        sess["snapshot_tree"] = tree
        return {"url": url, "title": title, "tree": tree}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sessions/{sid}/screenshot")
def api_screenshot(sid: str, selector: Optional[str] = None, format: str = "png", full_page: bool = False):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    # resolve @e refs if selector provided
    if selector:
        selector = _resolve_selector(sess, selector)
    try:
        data: Optional[bytes] = None
        if selector:
            # element-level screenshot via locator
            try:
                locator = page.locator(selector)
                target = locator.first if hasattr(locator, "first") else locator
                if hasattr(target, "screenshot"):
                    try:
                        data = target.screenshot(type=format)  # type: ignore[call-arg]
                    except TypeError:
                        # FakePage or older signature without type
                        try:
                            data = target.screenshot()  # type: ignore[call-arg]
                        except Exception as e:
                            raise HTTPException(500, f"screenshot failed: {e}")
                    except Exception as e:
                        raise HTTPException(500, f"screenshot failed: {e}")
                else:
                    # fallback to full page
                    try:
                        data = page.screenshot(type=format, full_page=full_page)  # type: ignore[call-arg]
                    except TypeError:
                        data = page.screenshot()  # type: ignore[call-arg]
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(500, f"screenshot failed: {e}")
        else:
            try:
                data = page.screenshot(type=format, full_page=full_page)  # type: ignore[call-arg]
            except TypeError:
                # FakePage may not accept kwargs
                try:
                    data = page.screenshot()  # type: ignore[call-arg]
                except Exception as e:
                    raise HTTPException(500, f"screenshot failed: {e}")
            except Exception as e:
                raise HTTPException(500, f"screenshot failed: {e}")
        if data is None:
            raise HTTPException(500, "screenshot returned empty")
        if isinstance(data, str):
            data = data.encode()
        b64 = base64.b64encode(data).decode()
        return {"format": format, "data": b64, "size": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sessions/{sid}/pdf")
def api_pdf(sid: str, format: str = "a4", landscape: bool = False):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    try:
        if not hasattr(page, "pdf"):
            raise HTTPException(501, "pdf not supported on this engine (firefox)")
        try:
            data = page.pdf(format=format, landscape=landscape)  # type: ignore[call-arg]
        except TypeError:
            try:
                data = page.pdf()  # type: ignore[call-arg]
            except Exception as e:
                raise HTTPException(501, f"pdf not supported: {e}")
        except Exception as e:
            # Playwright raises if pdf not supported (e.g., firefox)
            raise HTTPException(501, f"pdf not supported: {e}")
        if data is None:
            raise HTTPException(500, "pdf returned empty")
        if isinstance(data, str):
            data = data.encode()
        b64 = base64.b64encode(data).decode()
        return {"format": format, "landscape": landscape, "data": b64, "size": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/evaluate")
def api_evaluate(sid: str, inp: EvaluateIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    code = inp.code.strip() if isinstance(inp.code, str) else inp.code
    # support async: if code contains await, ensure wrapped in async function
    if code and "await" in code:
        # already async?
        if not code.lstrip().startswith("async"):
            # arrow function without async -> prefix async
            if "=>" in code:
                code = "async " + code.lstrip()
            else:
                # plain JS with await but not a function -> wrap into async IIFE
                # check if it looks like a function definition
                stripped = code.lstrip()
                is_func = stripped.startswith("(") or stripped.startswith("function")
                if not is_func:
                    # try to return value if single expression without explicit return
                    if "return" not in stripped and ";" not in stripped:
                        code = f"async () => {{ return ({stripped}) }}"
                    else:
                        code = f"async () => {{ {stripped} }}"
                else:
                    code = "async " + stripped
    try:
        data = page.evaluate(code)
        return {"result": data}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/cdp")
def api_cdp(sid: str, inp: CdpIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    method = inp.method
    params = inp.params or {}
    # FakePage simulation: detect by class name containing Fake
    if "Fake" in type(page).__name__:
        return {"method": method, "params": params, "result": "fake"}
    # also check engine page if FakeEngine
    try:
        engine = sess.get("engine")
        # if engine's page is fake, already returned; check context fakiness
        # Try real CDP via Playwright
        ctx = None
        # page.context is common
        if hasattr(page, "context"):
            try:
                ctx = page.context  # may be property or attribute
                if callable(ctx):
                    ctx = ctx()
            except Exception:
                ctx = None
        if ctx is None and engine is not None:
            ctx = getattr(engine, "_context", None) or getattr(engine, "context", None) or getattr(engine, "_ctx", None)
            if callable(ctx):
                try:
                    ctx = ctx()
                except Exception:
                    ctx = None
        if ctx is not None:
            # try new_cdp_session (sync_api)
            cdp = None
            if hasattr(ctx, "new_cdp_session"):
                try:
                    cdp = ctx.new_cdp_session(page)
                except Exception:
                    cdp = None
            elif hasattr(ctx, "newCDPSession"):
                try:
                    cdp = ctx.newCDPSession(page)  # type: ignore[attr-defined]
                except Exception:
                    cdp = None
            if cdp is not None:
                try:
                    result = cdp.send(method, params)  # type: ignore[attr-defined]
                    return {"method": method, "params": params, "result": result}
                except Exception as e:
                    raise HTTPException(500, f"CDP send failed: {e}")
            # try _do_cdp or _cdp etc.
            for attr in ("_do_cdp", "_cdp", "cdp_session"):
                if hasattr(ctx, attr):
                    try:
                        fn = getattr(ctx, attr)
                        if callable(fn):
                            result = fn(method, params)
                            return {"method": method, "params": params, "result": result}
                    except Exception:
                        pass
        # try page itself CDP
        for attr in ("_cdp", "cdp", "_do_cdp"):
            if hasattr(page, attr):
                try:
                    fn = getattr(page, attr)
                    if callable(fn):
                        result = fn(method, params)
                        return {"method": method, "params": params, "result": result}
                except Exception:
                    pass
    except HTTPException:
        raise
    except Exception:
        pass
    # fallback: CDP not available
    raise HTTPException(501, "CDP not available for this engine, use evaluate instead")


@app.post("/sessions/{sid}/extract")
def api_extract(sid: str, inp: ExtractIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    # гейт
    if not p.warmup.is_allowed("extract_light"):
        raise HTTPException(423, f"stage {p.warmup.stage} — extract not allowed yet")
    page = sess["page"]
    try:
        if inp.js:
            data = page.evaluate(inp.js)
        elif inp.selector:
            data = page.evaluate(f"""() => [...document.querySelectorAll('{inp.selector}')].map(e => e.innerText.slice(0,1000))""")
        else:
            data = page.content()
        p.health.total_extracts += 1
        _log_history(p, "extract", inp.selector or "", {"js_len": len(inp.js) if inp.js else 0})
        hits = _check_signals(p, page)
        if hits:
            _log_history(p, "extract_signal", "", {"signals": hits})
            return {"data": data, "signals": hits}
        p.health.record_success()
        p.warmup.try_advance(health_ok=p.health.status == "ok")
        p.save()
        return {"data": data}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{sid}/pause")
def api_pause(sid: str, seconds: float = 3.0):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    human_pause(seconds, seconds * 0.4, 0.5)
    return {"ok": True, "slept": seconds}


@app.post("/sessions/{sid}/stop")
def api_stop(sid: str):
    sess = _sessions.pop(sid, None)
    if not sess:
        raise HTTPException(404, "session not found")
    p: Profile = sess["profile"]
    try:
        sess["engine"].close()
    except Exception:
        pass
    p.release()
    p.save()
    return {"ok": True}


@app.get("/sessions")
def api_list_sessions():
    return [{"session_id": k, "profile_id": v["profile_id"], "engine": v["profile"].engine} for k, v in _sessions.items()]


# --- metrics (3.4) ---

class MetricsRecordIn(BaseModel):
    target: Optional[str] = None
    event_type: str = "generic"
    success: bool = True
    duration_ms: Optional[float] = None
    error: Optional[str] = None


@app.post("/metrics/{pid}/record")
def api_metrics_record(pid: str, inp: MetricsRecordIn):
    try:
        Profile.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")
    eid = record_event(pid, target=inp.target, event_type=inp.event_type, success=inp.success, duration_ms=inp.duration_ms, error=inp.error)
    return {"id": eid}


@app.get("/metrics")
def api_metrics_overall(days: int = 7):
    return {"overall": get_overall_stats(days=days), "per_target": get_per_target(days=days), "per_profile": get_per_profile(days=days), "recent": get_recent_events(limit=20)}


@app.get("/metrics/{pid}")
def api_metrics_profile(pid: str, days: int = 7):
    try:
        Profile.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")
    return {
        "profile_id": pid,
        "rate": get_success_rate(profile_id=pid, days=days),
        "health_series": get_health_series(pid, days=days),
        "recent": get_recent_events(limit=20, profile_id=pid),
    }


# --- ADS Power Local API compat (P2) ---
# GET /api/v1/browser/start?serial_number=pid&ip_tab=...  -> compat shim
@app.get("/api/v1/browser/start")
def api_adspower_start(serial_number: str = Query(None), user_id: str = Query(None), ip_tab: bool = Query(False), headless: bool = Query(True)):
    pid = serial_number or user_id
    if not pid:
        raise HTTPException(400, "serial_number or user_id required")
    # delegate to POST /sessions/{pid}/start logic without re-creating profile
    try:
        p = Profile.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, f"profile {pid} not found")
    # reuse existing start flow (simplified to avoid scheduler gate duplication for compat)
    # Use main api_start_session via internal call
    from fastapi.testclient import TestClient  # not used; direct logic below — duplicate minimal gate
    # quick gates
    locked, reason = p.is_locked()
    if locked:
        raise HTTPException(423, f"profile locked: {reason}")
    sid = f"sess_{pid}_{int(time.time()*1000)}"
    if not p.acquire(sid):
        raise HTTPException(423, "profile busy")
    engine = get_engine(p)
    try:
        page = engine.launch(p, headless=headless)
    except Exception as e:
        p.release()
        raise HTTPException(500, f"launch failed: {e}")
    _sessions[sid] = {"profile_id": pid, "engine": engine, "page": page, "profile": p}
    p.warmup.record_session()
    p.health.total_sessions += 1
    _log_history(p, "session_start", "", {"session_id": sid, "engine": p.engine, "via": "adspower_compat"})
    p.save()
    ws = f"ws://127.0.0.1:9222/{sid}"
    return {"code": 0, "msg": "success", "data": {"ws": {"selenium": ws, "puppeteer": ws}, "serial_number": pid, "session_id": sid, "wsEndpoint": ws}}


@app.get("/api/v1/browser/stop")
def api_adspower_stop(serial_number: str = Query(None), user_id: str = Query(None)):
    pid = serial_number or user_id
    if not pid:
        raise HTTPException(400, "serial_number or user_id required")
    # find session for pid
    for sid, sess in list(_sessions.items()):
        if sess["profile_id"] == pid:
            try:
                sess["engine"].close()
            except Exception:
                pass
            _sessions.pop(sid, None)
            sess["profile"].release()
            sess["profile"].save()
            return {"code": 0, "msg": "stopped", "data": {"serial_number": pid, "session_id": sid}}
    return {"code": 404, "msg": "no active session", "data": {}}


@app.get("/api/v1/browser/list")
def api_adspower_list(page: int = Query(0), page_size: int = Query(100)):
    profiles = list_profiles()
    total = len(profiles)
    start = page * page_size
    chunk = profiles[start : start + page_size]
    return {"code": 0, "msg": "success", "data": {"list": chunk, "total": total, "page": page, "page_size": page_size}}


@app.get("/api/v1/browser/active")
def api_adspower_active():
    return {"code": 0, "msg": "success", "data": {"list": [{"serial_number": v["profile_id"], "session_id": k, "engine": v["profile"].engine} for k, v in _sessions.items()]}}


# --- Team / RBAC (P1) ---

class TeamMemberIn(BaseModel):
    name: str
    role: str = "member"
    targets: Optional[list[str]] = None


@app.get("/team/status")
def api_team_status():
    from core.team import is_team_enabled, list_members

    return {"enabled": is_team_enabled(), "members": len(list_members())}


@app.get("/team/members")
def api_team_list():
    from core.team import list_members

    return {"members": list_members()}


@app.post("/team/members", status_code=201)
def api_team_create(inp: TeamMemberIn):
    from core.team import create_member

    try:
        m = create_member(inp.name, role=inp.role, targets=inp.targets)
        return m
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/team/members/{mid}")
def api_team_delete(mid: str):
    from core.team import delete_member

    if not delete_member(mid):
        raise HTTPException(404, "member not found")
    return {"ok": True}


@app.post("/team/members/{mid}/rotate_key")
def api_team_rotate(mid: str):
    from core.team import rotate_api_key

    try:
        return rotate_api_key(mid)
    except FileNotFoundError:
        raise HTTPException(404, "member not found")


@app.post("/team/members/{mid}/totp/setup")
def api_team_totp_setup(mid: str):
    from core.team import setup_totp

    try:
        return setup_totp(mid)
    except FileNotFoundError:
        raise HTTPException(404, "member not found")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/team/members/{mid}/totp/verify")
def api_team_totp_verify(mid: str, code: str = Query(...)):
    from core.team import verify_totp

    return {"valid": verify_totp(mid, code)}


@app.get("/team/audit")
def api_team_audit(limit: int = Query(50), actor: Optional[str] = Query(None)):
    from core.team import get_audit

    return {"audit": get_audit(limit=limit, actor=actor)}


# --- Cloud Sync (P1) ---

@app.post("/cloud/{pid}/push")
def api_cloud_push(pid: str):
    from core.cloud_sync import push_profile

    try:
        Profile.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "profile not found")
    try:
        res = push_profile(pid)
        from core.team import audit_log

        try:
            audit_log("system", "cloud.push", pid, res)
        except Exception:
            pass
        return res
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/cloud/{pid}/pull")
def api_cloud_pull(pid: str, new_id: Optional[str] = Query(None), overwrite: bool = Query(False)):
    from core.cloud_sync import pull_profile

    try:
        res = pull_profile(pid, new_id=new_id, overwrite=overwrite)
        return res
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/cloud/list")
def api_cloud_list():
    from core.cloud_sync import list_cloud

    return {"cloud": list_cloud()}


@app.delete("/cloud/{pid}")
def api_cloud_delete(pid: str):
    from core.cloud_sync import delete_cloud

    ok = delete_cloud(pid)
    return {"ok": ok}


# --- System: crypto + fonts (H9/H5) ---

@app.get("/system/crypto")
def api_system_crypto():
    from core.crypto import is_encryption_enabled, _get_key

    key_set = _get_key() is not None
    # also check cryptography installed
    try:
        import cryptography  # noqa: F401

        has_crypto = True
    except ImportError:
        has_crypto = False
    return {"encryption_enabled": is_encryption_enabled(), "key_set": key_set, "cryptography_installed": has_crypto, "env": "AGENTFOX_MASTER_KEY" if key_set else "none"}


@app.get("/system/fonts")
def api_system_fonts(os_name: str = Query("windows")):
    from core.fonts import ensure_fonts_available, get_fonts_subset

    # need a dummy profile_id for subset demo
    status = ensure_fonts_available(os_name)
    subset = get_fonts_subset(os_name, "demo_profile")
    status["subset_count"] = len(subset)
    status["subset_sample"] = subset[:3]
    return status


@app.post("/system/fonts/install")
def api_system_fonts_install(os_name: str = Query("windows")):
    from core.fonts import install_fonts_subset

    return install_fonts_subset(os_name)


# --- A5: network intercept + upload (P2) ---

class NetworkIn(BaseModel):
    action: str = Field(..., description="start|stop|list|clear")
    url_pattern: Optional[str] = None


@app.post("/sessions/{sid}/network")
def api_network(sid: str, inp: NetworkIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    sess.setdefault("network_logs", [])
    sess.setdefault("network_enabled", False)
    action = inp.action.lower()
    if action == "start":
        sess["network_enabled"] = True
        # hook page.route if available (real browser)
        try:
            if hasattr(page, "route"):
                def _handler(route):
                    req = route.request
                    if inp.url_pattern and inp.url_pattern not in req.url:
                        route.continue_()
                        return
                    sess["network_logs"].append({"url": req.url, "method": req.method, "resourceType": getattr(req, "resource_type", ""), "ts": time.time()})
                    route.continue_()

                page.route("**/*", _handler)
        except Exception:
            pass
        return {"ok": True, "enabled": True}
    elif action == "stop":
        sess["network_enabled"] = False
        try:
            if hasattr(page, "unroute"):
                page.unroute("**/*")
        except Exception:
            pass
        return {"ok": True, "enabled": False}
    elif action == "list":
        return {"logs": sess["network_logs"][-100:], "total": len(sess["network_logs"]), "enabled": sess["network_enabled"]}
    elif action == "clear":
        sess["network_logs"] = []
        return {"ok": True, "cleared": True}
    else:
        raise HTTPException(400, "action must be start|stop|list|clear")


@app.get("/sessions/{sid}/network")
def api_network_list(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    return {"logs": sess.get("network_logs", [])[-100:], "total": len(sess.get("network_logs", [])), "enabled": sess.get("network_enabled", False)}


class UploadIn(BaseModel):
    selector: str
    files: list[str] = Field(..., description="list of file paths or base64 data uris")
    # for API compat: if files are server-side paths, we setInputFiles directly
    # if base64, we write temp files


@app.post("/sessions/{sid}/upload")
def api_upload(sid: str, inp: UploadIn):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    selector = _resolve_selector(sess, inp.selector)
    # Try real upload via set_input_files
    tmp_files = []
    try:
        resolved_files = []
        for f in inp.files:
            if f.startswith("data:"):
                import base64 as _b64
                import tempfile as _tf
                import urllib.parse as _up

                header, data = f.split(",", 1)
                ext = ".bin"
                if "image/png" in header:
                    ext = ".png"
                elif "image/jpeg" in header:
                    ext = ".jpg"
                fd, path = _tf.mkstemp(suffix=ext)
                import os as _os

                if ";base64" in header:
                    # pad base64
                    data += "=" * (-len(data) % 4)
                    _os.write(fd, _b64.b64decode(data))
                else:
                    _os.write(fd, _up.unquote(data).encode())
                _os.close(fd)
                tmp_files.append(path)
                resolved_files.append(path)
            elif Path(f).exists():
                resolved_files.append(f)
            else:
                # treat as content -> temp file
                import tempfile as _tf

                fd, path = _tf.mkstemp(suffix=".txt")
                import os as _os

                _os.write(fd, f.encode())
                _os.close(fd)
                tmp_files.append(path)
                resolved_files.append(path)
        # attempt Playwright set_input_files
        try:
            loc = page.locator(selector).first
            if hasattr(loc, "set_input_files"):
                loc.set_input_files(resolved_files)
            elif hasattr(page, "set_input_files"):
                page.set_input_files(selector, resolved_files)
            else:
                # fallback: evaluate
                page.evaluate(f"() => document.querySelector('{selector}')?.click()")
        except Exception as e:
            raise HTTPException(500, f"upload failed: {e}")
        _log_history(sess["profile"], "upload", selector, {"files": len(resolved_files)})
        return {"ok": True, "files": len(resolved_files)}
    finally:
        for tf in tmp_files:
            try:
                Path(tf).unlink(missing_ok=True)
            except Exception:
                pass


# --- A6: multi-tab (P2) ---

@app.post("/sessions/{sid}/tabs")
def api_tabs_create(sid: str, url: Optional[str] = Query(None)):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    page = sess["page"]
    # sess["tabs"] dict: tabId -> page
    sess.setdefault("tabs", {})
    sess.setdefault("tab_counter", 0)
    sess["tab_counter"] += 1
    tab_id = f"tab_{sess['tab_counter']}"
    # try real new_page via context
    try:
        ctx = None
        if hasattr(page, "context"):
            try:
                ctx = page.context() if callable(page.context) else page.context
            except Exception:
                ctx = None
        if ctx and hasattr(ctx, "new_page"):
            new_page = ctx.new_page()
            if url:
                try:
                    new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            sess["tabs"][tab_id] = {"page": new_page, "url": url or "", "created_at": datetime.now(timezone.utc).isoformat()}
        else:
            # fallback: simulate tab as same page with url
            sess["tabs"][tab_id] = {"page": page, "url": url or getattr(page, "url", ""), "created_at": datetime.now(timezone.utc).isoformat()}
            if url:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
    except Exception as e:
        sess["tabs"][tab_id] = {"page": page, "url": url or "", "error": str(e)[:200]}
    return {"tab_id": tab_id, "url": url, "tabs": len(sess["tabs"]) + 1}


@app.get("/sessions/{sid}/tabs")
def api_tabs_list(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    tabs = sess.get("tabs", {})
    # include main page as tab_0
    main_url = ""
    try:
        main_url = sess["page"].url if isinstance(getattr(sess["page"], "url", ""), str) else ""
    except Exception:
        pass
    out = [{"tab_id": "main", "url": main_url, "active": sess.get("active_tab", "main") == "main"}]
    for tid, info in tabs.items():
        out.append({"tab_id": tid, "url": info.get("url", ""), "created_at": info.get("created_at", ""), "active": sess.get("active_tab") == tid})
    return {"tabs": out, "total": len(out), "active": sess.get("active_tab", "main")}


@app.post("/sessions/{sid}/tabs/{tab_id}/activate")
def api_tabs_activate(sid: str, tab_id: str):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    if tab_id == "main":
        sess["active_tab"] = "main"
        return {"ok": True, "active": "main"}
    tabs = sess.get("tabs", {})
    if tab_id not in tabs:
        raise HTTPException(404, "tab not found")
    sess["active_tab"] = tab_id
    # switch sess["page"] to that tab's page
    try:
        sess["page"] = tabs[tab_id]["page"]
    except Exception:
        pass
    return {"ok": True, "active": tab_id}


@app.post("/sessions/{sid}/tabs/{tab_id}/close")
def api_tabs_close(sid: str, tab_id: str):
    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    if tab_id == "main":
        raise HTTPException(400, "cannot close main tab, use /stop")
    tabs = sess.get("tabs", {})
    info = tabs.pop(tab_id, None)
    if not info:
        raise HTTPException(404, "tab not found")
    try:
        pg = info.get("page")
        if pg and hasattr(pg, "close"):
            pg.close()
    except Exception:
        pass
    if sess.get("active_tab") == tab_id:
        sess["active_tab"] = "main"
        # restore main page if we have it
    return {"ok": True, "closed": tab_id, "remaining": len(tabs) + 1}


@app.get("/")
def root():
    return {"service": "AgentFox", "version": "0.1.0", "docs": "/docs", "profiles": len(list_profiles()), "sessions": len(_sessions)}
