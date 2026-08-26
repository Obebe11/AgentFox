#!/usr/bin/env bash
# tools/fork_init.sh — инициализация форка Camoufox (Level 2, ARCHITECTURE.md §9)
#
# Стратегия:
#   Уровень 1 — зависимость (pip install cloverlabs-camoufox) — сейчас.
#   Уровень 2 — форк репозитория с патчами pythonlib — когда патчей >3.
#   Уровень 3 — форк C++ движка — только по блокеру.
# Сначала носим правки как monkey-patch в core/patches.py; этот скрипт нужен
# когда решаем переходить на Level 2.
#
# Источники апстрима (MPL-2.0):
#   - daijro/camoufox        (primary upstream, исп. в pip как cloverlabs-camoufox)
#   - CloverLabsAI/camoufox  (зеркало/форк, иногда новее)
#
# Использование:
#   bash tools/fork_init.sh                  # клон daijro/camoufox в ./vendor/camoufox
#   bash tools/fork_init.sh --upstream CloverLabsAI/camoufox
#   bash tools/fork_init.sh --dir /tmp/camoufox --no-clone   # только инструкции
#   bash tools/fork_init.sh --help
#
set -euo pipefail

UPSTREAM="daijro/camoufox"
DEST="vendor/camoufox"
DO_CLONE=1

usage() {
  cat <<'EOF'
tools/fork_init.sh — клон апстрима Camoufox для Level 2 форка

Опции:
  --upstream OWNER/REPO   апстрим (default: daijro/camoufox)
                          альтернатива: CloverLabsAI/camoufox
  --dir PATH              куда клонировать (default: vendor/camoufox)
  --no-clone              не клонировать, только вывести инструкции
  --help                  эта справка

После клона:
  1. cd vendor/camoufox
  2. gh repo fork --clone=false   # или форк через GitHub UI → git remote add myfork <url>
  3. перенеси патчи из core/patches.py в pythonlib (camoufox/…)
  4. pip install -e ./vendor/camoufox
  5. rebase на новые теги апстрима: git fetch upstream && git rebase upstream/main

Переход 1→2: когда в core/patches.py >3 патчей или нужен глубокий фикс pythonlib.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --upstream) UPSTREAM="$2"; shift 2 ;;
    --dir) DEST="$2"; shift 2 ;;
    --no-clone) DO_CLONE=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

echo "[fork_init] upstream=$UPSTREAM dest=$DEST"

if [[ "$DO_CLONE" -eq 0 ]]; then
  cat <<EOF

Инструкции без клона:

  git clone https://github.com/$UPSTREAM.git $DEST
  cd $DEST
  git remote rename origin upstream
  # создай форк на GitHub: gh repo fork $UPSTREAM --clone=false
  # затем:
  git remote add origin https://github.com/<YOUR_ORG>/camoufox.git
  git fetch upstream
  # перенеси патчи из core/patches.py → pythonlib
  pip install -e .

Monkey-patch слой остаётся в core/patches.py до перехода — см. PATCHES.md.
EOF
  exit 0
fi

if [[ -d "$DEST/.git" ]]; then
  echo "[fork_init] $DEST уже существует — пропускаем клон. Обновляю remote..."
  git -C "$DEST" remote -v || true
  if ! git -C "$DEST" remote | grep -qx upstream; then
    git -C "$DEST" remote rename origin upstream 2>/dev/null || true
    echo "[fork_init] origin → upstream (если был origin)"
  fi
  echo "[fork_init] готово. Для форка выполни:"
  echo "  gh repo fork $UPSTREAM --clone=false"
  echo "  git -C $DEST remote add origin https://github.com/<YOUR_ORG>/camoufox.git"
  exit 0
fi

mkdir -p "$(dirname "$DEST")"
echo "[fork_init] клонирую https://github.com/$UPSTREAM.git → $DEST ..."
if command -v git >/dev/null 2>&1; then
  git clone "https://github.com/$UPSTREAM.git" "$DEST"
  echo "[fork_init] клон готов. Ремоуты:"
  git -C "$DEST" remote -v
  cat <<EOF

Дальше (Level 2):

  cd $DEST
  git remote rename origin upstream
  gh repo fork $UPSTREAM --clone=false   # создаст <YOUR_ORG>/camoufox
  git remote add origin https://github.com/<YOUR_ORG>/camoufox.git
  git push -u origin main

  # перенести патчи:
  #   core/patches.py → $DEST/camoufox/...
  # проверить: pip install -e . && python -m camoufox --help

См. PATCHES.md и ARCHITECTURE.md §9.
EOF
else
  echo "[fork_init] git не найден — вывожу инструкции:"
  echo "  git clone https://github.com/$UPSTREAM.git $DEST"
fi
