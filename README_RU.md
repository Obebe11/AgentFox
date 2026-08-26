# AgentFox 🦊

**Легковесный антидетект-браузер для агентов.** Изоляция профилей уровня ADS Power, управляется полностью через HTTP API — без GUI, заточен под LLM-агентов.

[English](./README.md) | **Русский**

![verify](https://github.com/Obebe11/AgentFox/actions/workflows/verify.yml/badge.svg)

| Компонент | Репозиторий |
|---|---|
| **Движок** | [CloverLabsAI/camoufox](https://github.com/CloverLabsAI/camoufox) (PyPI: `cloverlabs-camoufox`) |
| **Слой AgentFox** | Этот репозиторий — менеджер профилей, антифрод-ядро, HTTP API |

## Зачем AgentFox

Один профиль = одна личность: отпечаток + IP + куки пиннятся навсегда. Агент не трогает браузер напрямую — ходит в локальный HTTP API, который сам следит за темпом, прогревом, сигналами здоровья и кулдаунами.

- **Пиннинг личности** — детерминированные сиды отпечатка из `profile_id`; 312 пресетов; UA ↔ платформа ↔ таймзона ↔ локаль ↔ geo консистентны
- **Человеческое поведение** — клики по Безье (25 движений + перелёт), печать 45–180 мс с Гауссом, инерционный скролл с отмотками
- **Прогрев** — 4 стадии на ~15 дней с гейтами и регрессом при сигналах
- **Мониторинг здоровья** — каждый шаг сканирует DOM на `captcha / rate_limit / blocked` → кулдаун, эскалация до banned
- **Cookie Farmer** — сеет реалистичную историю куков по geo
- **Планировщик** — джиттер интервалов, активные окна по таймзоне прокси
- **Snapshot + `@e` refs** — стабильные ссылки на элементы для LLM вместо дампа HTML
- **Наблюдаемость** — SQLite метрики + `/metrics` по профилям/таргетам
- **Паритет ADS Power для агентов** — bulk-создание, экспорт/импорт (`tar.zst`), корзина, cloud sync (S3), team RBAC + TOTP

## Установка

```bash
pip install -e ".[all]"          # API + Camoufox (PyPI) + Patchright fallback
python -m camoufox fetch         # бинарь браузера (~150 MB, один раз)
```

## Быстрый старт (headless)

```bash
uvicorn api.server:app --port 8000  # доки: http://localhost:8000/docs

# создать профиль (личность пиннится навсегда)
curl -X POST http://localhost:8000/profiles \
  -H "Content-Type: application/json" \
  -d '{"id":"research_de","geo":"DE","targets":["x.com"]}'

# запустить сессию (headless, shared binary, лимит кэша 50 MB)
curl -X POST http://localhost:8000/sessions/research_de/start -d '{"headless":true}'

# работа агента — только API
curl -X POST http://localhost:8000/sessions/<sid>/goto    -d '{"url":"https://x.com/search?q=...","read":true}'
curl -X POST http://localhost:8000/sessions/<sid>/snapshot
curl -X POST http://localhost:8000/sessions/<sid>/click   -d '{"selector":"@e1"}'
curl -X POST http://localhost:8000/sessions/<sid>/extract -d '{"js":"() => document.body.innerText.slice(0,5000)"}'
curl -X POST http://localhost:8000/sessions/<sid>/stop

# массовое создание
curl -X POST http://localhost:8000/profiles/bulk -d '{"count":10,"geo":"DE","prefix":"farm"}'

# фарм куков
curl -X POST http://localhost:8000/profiles/research_de/farm
```

Для MCP-агентов есть `mcp_server.py`.

## Документы

- [.agent/skills/agentfox/SKILL.md](.agent/skills/agentfox/SKILL.md) — шпаргалка для LLM-агента: таблица API, best practices, траектории

## Верификация

```bash
pytest -q                                  # 95 тестов
python -m tools.verify_optimization        # 32 проверки, 8 доменов, ~1.2 с
python -m tools.verify_optimization --live # + bot.sannysoft / creepjs (нужна сеть + бинарь)
```

CI гоняет верификацию на каждый push ([workflow](./.github/workflows/verify.yml)).

## Docker (slim, headless)

```bash
docker build -f docker/Dockerfile -t agentfox:runtime .
docker run -p 8000:8000 -v ./profiles:/app/profiles agentfox:runtime
```

Бинарь один RO на всех профилях, профили — COW-оверлеи. 10 профилей ≈ 1.7 GB диска, 5 параллельно ≈ 550 MB RAM.

## Движок

Используется публичный `cloverlabs-camoufox` с PyPI. Патчи — `core/patches.py` (monkey-patch). Отдельный форк нужен только при >3 глубоких правках `pythonlib` — см. `tools/fork_init.sh` для Level 2.

## Гигиена секретов

Списки прокси, токены почты и креды не коммитятся: `*.token`, `mail.json`, `proxies.json` и `profiles/` в `.gitignore` по умолчанию. Используйте примеры (`tools/benchmark/mail.json.example`, `tools/benchmark/proxies.json.example`).

## Лицензия

MPL-2.0 (как Camoufox).
