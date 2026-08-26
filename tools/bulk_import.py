#!/usr/bin/env python3
"""
Batch Profiles — ADS Power: 100 профилей 1 кликом для агента.
CLI: python -m tools.bulk_import --count 10 --geo DE --proxy-file proxies.txt --prefix auto --engine firefox
Создаёт profiles via profile_manager напрямую (не через API), с seed_from_bank, коллизии → suffix, atomic per profile.
"""
from __future__ import annotations

import argparse
import json
import random
import string
import sys
from pathlib import Path
from typing import Any, Optional

# ensure project root in path when run as `python -m tools.bulk_import`
import core.profile_manager as pm
from core.cookie_farmer import seed_from_bank
from core.profile_manager import create_profile


def load_proxies(proxy_file: str | Path) -> list[str]:
    """Read proxy file — one server per line (e.g. http://host:port). Ignore empty/#."""
    p = Path(proxy_file)
    if not p.exists():
        raise FileNotFoundError(f"proxy file not found: {proxy_file}")
    proxies: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxies.append(line)
    return proxies


def _next_pid_and_check(prefix: str, idx: int) -> str:
    """Generate pid f'{prefix}_{idx}' with collision handling (random suffix up to 5 tries). Returns pid to try."""
    base = f"{prefix}_{idx}"
    pid = base
    attempts = 0
    while (pm.PROFILES_ROOT / pid).exists() and (pm.PROFILES_ROOT / pid / "meta.json").exists():
        if attempts >= 5:
            break
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        pid = f"{base}_{suffix}"
        attempts += 1
    return pid


def bulk_import(
    count: int,
    geo: str = "DE",
    prefix: str = "auto",
    proxy_file: Optional[str] = None,
    proxies: Optional[list[str]] = None,
    os: Optional[str] = None,
    locale: Optional[str] = None,
    engine: str = "firefox",
    targets: Optional[list[str]] = None,
    proxy: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Создаёт `count` профилей с ids f"{prefix}_{i}" (+ random suffix on collision).
    - proxies: list of server strings (from file) — round-robin per profile
    - proxy: single dict proxy (if provided, used for all; proxies file takes precedence)
    - atomic per profile: on exception continue, collect errors
    - uses seed_from_bank, respects locks
    Returns {"created": [...], "errors": [...], "total": int}
    """
    if count < 1 or count > 100:
        raise ValueError("count must be 1..100")

    # resolve proxies list
    proxy_list: list[str] = []
    if proxies is not None:
        proxy_list = list(proxies)
    elif proxy_file:
        proxy_list = load_proxies(proxy_file)

    created: list[dict] = []
    errors: list[dict] = []

    for i in range(1, count + 1):
        base_pid = f"{prefix}_{i}"
        pid = base_pid
        # collision handling
        attempts = 0
        while (pm.PROFILES_ROOT / pid).exists() and (pm.PROFILES_ROOT / pid / "meta.json").exists():
            if attempts >= 5:
                break
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            pid = f"{base_pid}_{suffix}"
            attempts += 1
        if (pm.PROFILES_ROOT / pid).exists() and (pm.PROFILES_ROOT / pid / "meta.json").exists():
            errors.append({"index": i, "id": pid, "error": f"profile {pid} already exists"})
            continue

        # choose proxy for this index
        cur_proxy: Optional[dict] = None
        if proxy_list:
            server = proxy_list[(i - 1) % len(proxy_list)]
            # server line may contain username:password@host? For simplicity treat as server only
            # support format: server, username, password separated by space or comma
            # basic: whole line is server
            cur_proxy = {"server": server}
            # if caller also provided dict proxy with extra fields (type/provider), merge? ignore
        elif proxy is not None:
            cur_proxy = dict(proxy)

        try:
            p = create_profile(
                pid=pid,
                os=os,
                locale=locale,
                geo=geo,
                proxy=cur_proxy,
                targets=targets,
                engine=engine,
            )
            # respect locks: newly created should be unlocked; seed
            try:
                seeded = seed_from_bank(p)
            except Exception:
                seeded = 0
            # respect locks check (should be ok)
            created.append({**p.to_dict(), "seeded_cookies": seeded})
        except FileExistsError as e:
            errors.append({"index": i, "id": pid, "error": str(e)})
        except Exception as e:
            errors.append({"index": i, "id": pid, "error": str(e)})

    return {"created": created, "errors": errors, "total": len(created)}


# aliases for test compatibility (direct call to bulk_import logic, mock)
create_profiles_bulk = bulk_import
bulk_create = bulk_import
run_bulk = bulk_import


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="AgentFox bulk import — 100 профилей 1 кликом")
    ap.add_argument("--count", type=int, required=True, help="number of profiles 1..100")
    ap.add_argument("--geo", type=str, default="DE", help="geo code, default DE")
    ap.add_argument("--prefix", type=str, default="auto", help="id prefix, default auto -> auto_1, auto_2 ...")
    ap.add_argument("--proxy-file", type=str, default=None, help="path to proxy file (one server per line)")
    ap.add_argument("--os", type=str, default=None, help="os override (windows|macos|linux)")
    ap.add_argument("--locale", type=str, default=None, help="locale override e.g. de-DE")
    ap.add_argument("--engine", type=str, default="firefox", help="firefox|chromium, default firefox")
    ap.add_argument("--targets", type=str, default=None, help="comma-separated targets e.g. example.com,other.com")
    ap.add_argument("--proxy", type=str, default=None, help="single proxy json e.g. '{\"server\":\"http://host:port\"}' or plain server")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    count = args.count
    if count < 1 or count > 100:
        print(f"error: count must be 1..100, got {count}", file=sys.stderr)
        sys.exit(2)

    targets = None
    if args.targets:
        targets = [t.strip() for t in args.targets.split(",") if t.strip()]

    proxy_dict = None
    if args.proxy:
        # try json, else plain server string
        try:
            proxy_dict = json.loads(args.proxy)
            if not isinstance(proxy_dict, dict):
                proxy_dict = {"server": str(args.proxy)}
        except Exception:
            proxy_dict = {"server": args.proxy}

    # if both proxy_file and proxy dict provided, proxy_file wins (round-robin); emit warn
    if args.proxy_file and proxy_dict:
        print(f"[bulk_import] both --proxy and --proxy-file given, using proxy_file round-robin", file=sys.stderr)

    result = bulk_import(
        count=count,
        geo=args.geo,
        prefix=args.prefix,
        proxy_file=args.proxy_file,
        os=args.os,
        locale=args.locale,
        engine=args.engine,
        targets=targets,
        proxy=proxy_dict,
    )

    created = result["created"]
    errors = result["errors"]

    print(f"[bulk_import] created {len(created)}/{count} geo={args.geo} prefix={args.prefix} engine={args.engine}")
    for c in created:
        # print id and proxy server if any
        proxy_info = c.get("proxy") or {}
        server = proxy_info.get("server") if isinstance(proxy_info, dict) else ""
        print(f"  + {c['id']} {c.get('engine','')} {server or ''}")
    if errors:
        print(f"[bulk_import] errors {len(errors)}:", file=sys.stderr)
        for e in errors:
            print(f"  ! #{e.get('index')} id={e.get('id')} error={e.get('error')}", file=sys.stderr)

    # summary json for scripting
    # don't exit 1 if partial success; only exit 1 if nothing created
    if len(created) == 0 and len(errors) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
