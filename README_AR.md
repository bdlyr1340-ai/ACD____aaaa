# 🔄 إصلاح آمن لتسجيل الدخول (إرجاع للنسخة الكلاسيكية)

## ❗ المشكلة
نسخة Camoufox + كاشف الحظر المبكر كسرت تسجيل الدخول الذي كان شغال.

## ✅ الحل في هذا الملف
- **حذف Camoufox كلياً** — رجوع لـ Playwright العادي.
- **حذف كاشف "Couldn't sign you in" المبكر** — كان يوقف العملية بدون داعي.
- **إصلاح وحيد محتفظ به**: تجاهل حقل الباسورد المخفي `hiddenPasswor` عبر selector دقيق:
  ```
  input[type="password"][name="Passwd"]:not([aria-hidden="true"]):not([tabindex="-1"])
  ```
- إبقاء دعم **Device-tap → Authenticator** (المنطق الذي طلبته سابقاً).
- إبقاء دعم **PROXY_LIST** (اختياري، يدور بين الحسابات).
- stealth خفيف يدوي (إخفاء `navigator.webdriver`).

## 📦 طريقة التثبيت
1. فك الضغط.
2. استبدل ملف **`bot/services/google_account.py`** فقط بالملف الجديد.
3. أعد تشغيل البوت في Railway.

## 🧹 المتغيرات المطلوبة (الأساسية فقط)
```
BOT_TOKEN=...
ADMIN_IDS=...
DATABASE_URL=...
HEADLESS=true
DEVICE_TAP_WAIT_SEC=75
PROXY_LIST=          ← اختياري، اتركه فارغاً إن أردت
```

**احذف من Railway** (لم تعد مطلوبة):
- `USE_CAMOUFOX`
- `USE_STEALTH`
- `CAMOUFOX_GEOIP`
- `MAX_RETRIES_ON_BLOCK`

## 📝 ملاحظة
الملف يحتوي منطق تسجيل الدخول + توليد كلمة السر/2FA الجديدة فقط.
دوال `change_password` و `setup_new_2fa` التفصيلية يجب إبقاؤها كما هي في نسختك السابقة، أو أخبرني لأرسلها كاملة في حزمة ثانية.
