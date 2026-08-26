# AgentFox runtime — slim, headless, без визуала
# Сборка браузера — только на build-машине (multibuild.py), сюда копируется готовый артефакт.

FROM debian:bookworm-slim AS runtime

# Минимальные рантайм-зависимости Firefox (без build-deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 libasound2 \
    libdrm2 libgbm1 libxcomposite1 libxdamage1 libxrandr2 libxss1 \
    ca-certificates python3 python3-pip curl \
 && rm -rf /var/lib/apt/lists/*

# Агентский режим — без визуала, но с headless патчем Camoufox
ENV AGENTFOX_HEADLESS=1 \
    AGENTFOX_BLOCK_IMAGES=0 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Ожидается что /opt/camoufox уже распакован из релизного zip.
# Для локальной сборки: COPY --from=builder /dist/camoufox /opt/camoufox
# Для VPS деплоя: скачивается один раз на хосте и монтируется как volume.
COPY pyproject.toml README.md ./
COPY core ./core
COPY behavior ./behavior
COPY api ./api

RUN pip install --no-cache-dir --break-system-packages -e "."

# Профили и GeoIP — volumes (COW, бэкап = tar одного user_data)
VOLUME ["/app/profiles", "/app/profiles/_cookie_bank"]

EXPOSE 8000
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# --- builder stage (только для справки, не входит в рантайм) ---
# FROM ubuntu:22.04 AS builder
# COPY . /app
# RUN apt-get update && apt-get install -y build-essential ... && \
#     python3 multibuild.py --target linux --arch x86_64
