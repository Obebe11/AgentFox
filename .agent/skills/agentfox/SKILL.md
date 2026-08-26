---
name: agentfox
description: AgentFox antidetect browser — API quick ref for LLM agents (profiles/sessions/goto/click/type/scroll/extract/snapshot/screenshot/evaluate)
---

# AgentFox — LLM Agent Quick Ref

> Lightweight antidetect browser for agents. Base `http://localhost:8000`. One profile = one identity + IP + fingerprint forever. Do NOT reuse profile across unrelated targets.

## 0 Base

- `uvicorn api.server:app --port 8000` → docs `/docs`
- All responses JSON. Errors `423` = warmup/cooldown lock, `404` = no profile/session.

## 1 API Quick Ref

| Method | Path | Body / Query | Desc |
|---|---|---|---|
| POST | `/profiles` | `{id, geo, os, locale, proxy, targets, engine}` | create pinned identity |
| POST | `/profiles/bulk` | `{count, geo, prefix}` | batch create (1..100) |
| GET | `/profiles/{id}` | — | meta + identity |
| GET | `/health/{id}` | — | warmup + health + lock |
| POST | `/sessions/{pid}/start` | `{headless:true}` | → `{session_id}` (checks lock/warmup/proxy health) |
| POST | `/sessions/{sid}/goto` | `{url, wait_until, read}` | nav; `read=true` → human scroll+pause |
| GET | `/sessions/{sid}/snapshot` | — | → `{tree:[{ref:@e1,role,name,selector}]}` |
| POST | `/sessions/{sid}/click` | `{selector, human}` | `@e1` or CSS; bezier 25 moves + overshoot |
| POST | `/sessions/{sid}/type` | `{selector,text,clear}` | human keystrokes 45-180ms + `human_pause` |
| POST | `/sessions/{sid}/scroll` | `{screens, detour}` | inertia scroll; `detour 0.2` random up |
| POST | `/sessions/{sid}/extract` | `{js, selector}` | `js:"()=>..."` or `selector` → `{data}` |
| POST | `/sessions/{sid}/evaluate` | `{code}` | JS; auto-wraps `await`; sync+async |
| POST | `/sessions/{sid}/cdp` | `{method,params}` | CDP escape hatch |
| GET | `/sessions/{sid}/screenshot` | `?selector&format&full_page` | → `{data:base64,size}` |
| GET | `/sessions/{sid}/pdf` | `?format&landscape` | chromium only |
| POST | `/sessions/{sid}/pause` | `?seconds` | `human_pause` (Gaussian) |
| POST | `/sessions/{sid}/stop` | — | close + release lock |
| GET | `/sessions` | — | list active |

Selectors: prefer `@e1` from `snapshot` (stable vs class churn). Fallback CSS.

## 2 Best Practices

- **human_pause**: auto in `goto/click/type` (0.7-0.8s mean, σ0.35). Never `sleep(2)` — Gaussian jitter. Use `read:true` for reading pauses 3-10s scaled to content.
- **warmup** stages gate actions (auto-regress on signals):
  | S | Days | Allowed | Session |
  |---|---|---|---|
  | 1 | 1-3 | browse, read | 5-10m, 1-2/d |
  | 2 | 4-7 | +search, extract_light | 10-20m |
  | 3 | 8-14 | +extract_deep, navigate | 20-40m |
  | 4 | 15+ | all | 5-40m |
  Check `/health/{id}`; `423` means not allowed yet. Need `min_sessions` + `health_ok` to advance.
- **health**: every `goto/click/type/scroll/extract` scans DOM for `captcha/rate_limit/blocked/logout/suspicious`. On hit → `cooldown` + `warmup.regress()`. Cooldowns: rate_limit 6h, login_wall 2h, captcha/logout 24h, blocked 48h, suspicious 72h. On 3x blocked→`banned`. Always `GET /health` before `start`.
- **scheduler**: `core/scheduler` → `jittered_interval` ± random, `should_run` gate, active window per proxy TZ. Do NOT start 5 profiles same second — use `next_run_after`. Inactivity >7d → auto-regress.
- **limits — X.com safe per session** (AGENTFOX.md §5.2):
  | Metric | Safe | Suspicious |
  |---|---|---|
  | Duration | 5-25m | 60+m |
  | Searches | 3-8 | 20+ |
  | Pause between searches | 20-90s random | flat 5s |
  | Tweets read | 200-500 | 2000+ |
  | Runs | 1 per 48h ±3h jitter | hourly |
  Killers: volume spike, IP country switch, one fingerprint→many accounts, dual login (VPS+phone).
- **proxy**: sticky per profile, `rotate_after 14d` auto. Health-gate on `start` — `423` if dead. Use residential $0.65-2/GB, mobile $3-8/GB for X/Google. `disable_non_proxied_udp` + DNS via proxy.
- **fingerprint**: pinned at create (`fingerprint_preset_id` 312 presets). UA↔platform↔timezone↔locale↔geo must match. Never change IP country for existing profile.

## 3 Trajectories (few-shot)

### A Search (X.com)
```
POST /profiles {id:"research_de", geo:"DE"}
POST /sessions/research_de/start → sid
POST /sessions/{sid}/goto {url:"https://x.com/search?q=topic%20lang:ru%20since:2026-08-21 -is:retweet", read:true}
GET  /sessions/{sid}/snapshot → pick @e
POST /sessions/{sid}/scroll {screens:2, detour:0.2}
POST /sessions/{sid}/extract {js:"()=>[...document.querySelectorAll('[data-testid=tweet]')].slice(0,20).map(e=>e.innerText)"}
POST /sessions/{sid}/pause?seconds=45
POST /sessions/{sid}/stop
```

### B Extract (generic site, CF-safe)
```
POST /sessions/{sid}/goto {url:"https://example.com/article", read:true}
GET  /sessions/{sid}/snapshot
POST /sessions/{sid}/extract {js:"()=>document.body.innerText.slice(0,5000)"}
# alt via selector
POST /sessions/{sid}/extract {selector:"article p"}
GET  /sessions/{sid}/screenshot?full_page=true → base64
GET  /health/{id} → check signals; if cooldown → backoff
```

### C Farm + Warmup
```
POST /profiles {id:"farm_de", geo:"DE"}  # auto seed_from_bank
POST /profiles/farm_de/farm              # visit top sites by geo, fills cookie bank
POST /sessions/farm_de/start
POST /sessions/{sid}/goto {url:"https://google.de", read:true}
POST /sessions/{sid}/scroll {screens:1}
POST /sessions/{sid}/stop
# repeat 1-2/day stage 1; stage 2+ allow search/extract_light
```

## 4 Checklist (pre-flight)

```
□ /health = ok, no cooldown, warmup allows action
□ proxy = residential/mobile sticky, health OK, geo matches timezone/locale
□ fingerprint stable (same @e across restarts), UA matches engine
□ bot.sannysoft green, creepjs trust >70%, browserleaks TLS/webrtc OK (live)
□ session 5-25m max, pauses Gaussian, scroll variable, maybe_detour 0.2
□ after extract check signals; on rate_limit/captcha → halve tempo, 24h pause
```

Tokens: use `snapshot`+`@e` instead of dumping HTML; `extract` with targeted JS; `read:true` auto-handles pacing.
