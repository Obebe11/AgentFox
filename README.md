# AgentFox 🦊

**Lightweight anti-detect browser for AI agents.** AdsPower-class profile isolation, driven entirely through an HTTP API — no GUI, built for LLM agents.

[English](./README.md) | [Русский](./README_RU.md)

![verify](https://github.com/Obebe11/AgentFox/actions/workflows/verify.yml/badge.svg)

| Component | Repository |
|---|---|
| **Engine** | [CloverLabsAI/camoufox](https://github.com/CloverLabsAI/camoufox) (PyPI: `cloverlabs-camoufox`) |
| **AgentFox layer** | This repository — profile manager, anti-fraud core, HTTP API |

## Why AgentFox

One profile = one identity: fingerprint + IP + cookies pinned forever. The agent never touches the browser directly — it talks to a local HTTP API that enforces pacing, warmup gates, health signals and cooldowns automatically.

- **Pinned identity** — deterministic fingerprint seeds derived from `profile_id`; 312 validated presets; UA ↔ platform ↔ timezone ↔ locale ↔ geo consistency
- **Human behavior layer** — Bezier mouse curves (25 moves + overshoot), Gaussian typing 45–180 ms, inertia scroll with back-detours, human pauses
- **Warmup engine** — staged automation (4 stages over ~15 days) with action gates and regression on signals
- **Health monitoring** — every action scans DOM for captcha / rate-limit / blocked / logout signals → cooldown, escalation to banned
- **Cookie Farmer** — seeds realistic cookie history per geo so a fresh profile isn't sterile
- **Scheduler** — jittered intervals, timezone-aware active windows, inactivity auto-regress
- **Snapshot + `@e` refs** — stable element references made for LLM agents instead of raw HTML dumps
- **Observability** — SQLite metrics + `/metrics` per profile/target
- **AdsPower parity for agents** — bulk create, export/import (`tar.zst`), trash/restore, cloud sync (S3), team RBAC + TOTP

## Install

```bash
pip install -e ".[all]"          # API + Camoufox (PyPI build) + Patchright fallback
python -m camoufox fetch         # browser binary (~150 MB, done once)
```

## Quick start (agent mode — headless)

```bash
uvicorn api.server:app --port 8000     # interactive docs: http://localhost:8000/docs

# create a profile (identity gets pinned permanently, seeds are stable)
curl -X POST http://localhost:8000/profiles \
  -H "Content-Type: application/json" \
  -d '{"id":"research_de","geo":"DE","targets":["x.com"]}'

# start a session (headless, shared binary, 50 MB cache cap)
curl -X POST http://localhost:8000/sessions/research_de/start -d '{"headless":true}'

# agent work — API only, never touches the browser
curl -X POST http://localhost:8000/sessions/<sid>/goto    -d '{"url":"https://x.com/search?q=...","read":true}'
curl -X POST http://localhost:8000/sessions/<sid>/snapshot
curl -X POST http://localhost:8000/sessions/<sid>/click   -d '{"selector":"@e1"}'
curl -X POST http://localhost:8000/sessions/<sid>/extract -d '{"js":"() => document.body.innerText.slice(0,5000)"}'
curl -X POST http://localhost:8000/sessions/<sid>/stop

# batch create profiles
curl -X POST http://localhost:8000/profiles/bulk -d '{"count":10,"geo":"DE","prefix":"farm"}'

# farm cookies (Cookie Robot)
curl -X POST http://localhost:8000/profiles/research_de/farm
```

An MCP server is also included (`mcp_server.py`) for agents that speak Model Context Protocol.

## Docs

- [.agent/skills/agentfox/SKILL.md](.agent/skills/agentfox/SKILL.md) — **LLM agent quick ref**: full API table, best practices, few-shot trajectories

## Verification

```bash
pytest -q                                  # 96 tests
python -m tools.verify_optimization        # 32 checks, 8 domains, ~1.2 s
python -m tools.verify_optimization --live # + bot.sannysoft / creepjs (needs network + binary)
```

CI runs verification on every push ([workflow](./.github/workflows/verify.yml)).

## Docker (slim, headless)

```bash
docker build -f docker/Dockerfile -t agentfox:runtime .
docker run -p 8000:8000 -v ./profiles:/app/profiles agentfox:runtime
```

The Camoufox binary is a single read-only image shared across all profiles; each profile is a COW overlay. 10 profiles ≈ 1.7 GB disk, 5 parallel ≈ 550 MB RAM.

## Engine

Uses public `cloverlabs-camoufox` from PyPI. Patches live in `core/patches.py` (monkey-patch layer). A dedicated fork is only needed if you have >3 deep `pythonlib` fixes — see `tools/fork_init.sh` for Level 2 instructions.

## Secrets hygiene

Proxy lists, mail tokens and other credentials must stay out of version control: `*.token`, `mail.json`, `proxies.json` and `profiles/` are `.gitignore`d by default. Use the provided examples (`tools/benchmark/mail.json.example`, `tools/benchmark/proxies.json.example`) as templates.

## Responsible use

AgentFox is intended for legitimate automation, QA testing, privacy research and ad verification against services you are authorized to access. You are responsible for complying with applicable laws and the terms of service of the sites you visit.

## License

MPL-2.0 (same as Camoufox).
