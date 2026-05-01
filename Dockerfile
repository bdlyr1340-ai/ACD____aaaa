FROM python:3.11-slim

WORKDIR /app

# System libs needed by Camoufox (Firefox-based) + Playwright Chromium fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates curl \
    fonts-liberation fonts-noto-color-emoji \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libwayland-client0 libxcomposite1 libxdamage1 \
    libxfixes3 libxkbcommon0 libxrandr2 xdg-utils libx11-xcb1 \
    libdbus-glib-1-2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Camoufox browser binary (REQUIRED — fail build if it fails)
RUN python -m camoufox fetch
# Optional GeoIP database (silent if missing license)
RUN python -m camoufox fetch --geoip || echo "GeoIP skipped"
# Playwright Chromium as fallback only
RUN playwright install --with-deps chromium || echo "Playwright chromium skipped"

COPY . .

RUN find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Force Camoufox / no-proxy mode by default
ENV NO_PROXY=1
ENV BROWSERLESS_PROXY=none

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f "python.*main" || exit 1

CMD ["python", "-m", "bot.main"]
