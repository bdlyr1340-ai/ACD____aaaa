# 🛠️ حزمة استرجاع البوت - النسخة العاملة

## ما تم إصلاحه (سبب CRASHED على Railway)

1. **`Dockerfile`**: حذفنا `python -m camoufox fetch` — كان يفشل البناء لأن Camoufox لا يعمل بشكل موثوق على Railway slim images، وحجم التحميل ضخم.
2. **`requirements.txt`**: حذفنا `camoufox[geoip]` لأن `bot/services/google_account.py` (النسخة الآمنة الحالية) **لا يستخدمه أصلاً** — كان مجرد ثقل زائد يُفشل التثبيت.
3. **`HEALTHCHECK`**: حذفناه — كان يسبب إعادة تشغيل لانهائية حين يفشل `pgrep`.
4. **`railway.json`**: قللنا `restartPolicyMaxRetries` من 10 إلى 5 لتجنب حلقات إعادة التشغيل الطويلة.

## ✅ ما لم يتغير (المنطق نفسه)

- `bot/services/google_account.py` — نفس النسخة الآمنة (Playwright + stealth + إصلاح hidden password).
- `bot/handlers/*.py` و `bot/db/*` و `bot/main.py` — لم يُلمس شيء.
- دعم بروكسي عبر `PROXY_LIST` لا يزال يعمل.

## 🔑 المتغيرات المطلوبة على Railway

```bash
BOT_TOKEN=...
ADMIN_IDS=123456789
DATABASE_URL=postgresql://...
# اختياري:
PROXY_LIST=http://user:pass@host:port,...
DEFAULT_CREDITS=3
ROTATE_COST=1
```

## 🗑️ احذف هذه المتغيرات من Railway (لم تعد مستخدمة)

```
USE_CAMOUFOX
USE_STEALTH
CAMOUFOX_GEOIP
MAX_RETRIES_ON_BLOCK
NO_PROXY
BROWSERLESS_PROXY
```

## 🚀 خطوات التطبيق

1. ارفع محتوى هذه الحزمة على GitHub (استبدل الملفات).
2. على Railway: امسح المتغيرات المذكورة أعلاه.
3. اضغط **Redeploy**.
4. راقب Logs — يجب أن ترى `Bot connected: @YourBotName`.

إذا ظهر أي خطأ جديد، انسخ آخر 30 سطر من Logs وأرسلها لي.
