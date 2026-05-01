# 🛠️ إصلاح خطأ "Couldn't sign you in" + Timeout على input[password]

## المشكلتان
1. **Google يرفض المتصفح** ويعرض شاشة *"This browser or app may not be secure"* — السبب: Playwright Chromium الافتراضي يُكتشف بسهولة + IP السيرفر (Railway) محظور.
2. **`Page.wait_for_selector` Timeout** — السيليكتور كان يلتقط `<input name="hiddenPasswor" aria-hidden="true">` (حقل مخفي) بدل حقل كلمة السر الحقيقي.

## الحل المُطبَّق في `bot/services/google_account.py`

### 1) محرك متصفح مضاد للكشف
- **Camoufox** أولاً (يفعّل anti-detection حقيقي + بصمة Firefox)
- **fallback** إلى `playwright + playwright-stealth` مع:
  - UA حديث (Chrome 130 على Windows)
  - حقن `navigator.webdriver = undefined`
  - `--disable-blink-features=AutomationControlled`
  - locale `en-US`

### 2) كاشف حظر مبكر
دالة `_check_google_block()` تُستدعى بعد كل خطوة وترفع خطأ واضح فوراً (مع لقطة شاشة + HTML) بدل انتظار 20 ثانية.

### 3) سيليكتور كلمة السر مُصلَح
```python
'input[type="password"][name="Passwd"]:visible, '
'input[type="password"][autocomplete="current-password"]:visible, '
'input[type="password"]:visible:not([aria-hidden="true"]):not([name="hiddenPasswor"])'
```
يستثني صراحةً `hiddenPasswor` و `aria-hidden="true"`.

### 4) دعم بروكسي residential
متغير بيئي `PROXY_LIST` (مفصول بفواصل):
```
PROXY_LIST=http://user:pass@host1:8000,http://user:pass@host2:8000
```
البوت يختار واحداً عشوائياً لكل حساب.

## التثبيت

### 1) ضع الملف
استبدل `bot/services/google_account.py` لديك بالملف الجديد من هذا الـ ZIP.

### 2) أضف للـ `requirements.txt` (إن لم تكن موجودة):
```
camoufox[geoip]>=0.4.0
playwright-stealth>=1.0.6
pyotp>=2.9.0
```

### 3) ثبّت متصفح Camoufox مرة واحدة على السيرفر:
```bash
python -m camoufox fetch
playwright install chromium  # كـ fallback
```

### 4) متغيرات البيئة الجديدة في Railway
| المتغير | القيمة | إجباري؟ |
|---|---|---|
| `PROXY_LIST` | `http://user:pass@ip:port,http://user:pass@ip2:port2` | **مُوصى بشدة** |
| `HEADLESS` | `true` | اختياري |
| `DEVICE_TAP_WAIT_SEC` | `75` | اختياري |
| `SHOTS_DIR` | `/tmp/shots` | اختياري |

## ⚠️ تحذير مهم
**بدون بروكسي residential سيستمر الحظر** على Railway/DigitalOcean/أي VPS. Google يحظر هذه الـ IPs على مستوى الشبكة قبل أن يصل الطلب لمرحلة فحص المتصفح. الكود الجديد سيرسل لك خطأ واضح:
```
BLOCKED_BY_GOOGLE: Google رفض المتصفح ('Couldn't sign you in').
السبب الأرجح: IP السيرفر محظور. الحل: أضف PROXY_LIST residential.
```

## مزودو بروكسي residential مُجرَّبون
- BrightData
- Smartproxy
- IPRoyal
- Oxylabs
