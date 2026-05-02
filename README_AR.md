# 🔧 إصلاح v7 — معالجة صفحة Recovery Phone

## المشكلة
عند خطوة `enable_new_authenticator`، Google يفرض على المستخدم إضافة **Recovery phone** (رقم استرداد) قبل السماح بإعداد Authenticator جديد. البوت كان يبحث عن حقل TOTP فلا يجده ويفشل بعد 3 محاولات.

التقرير أظهر:
```
url: .../signinoptions/rescuephone
title: Recovery phone
body: Add recovery phone
```

## الحل
أضفت دالتين جديدتين إلى `bot/services/google_account.py`:

### 1. `_is_recovery_phone_page(url, title, body_text)`
تكتشف صفحة Recovery phone عبر:
- URL يحوي `rescuephone` / `recoveryphone`
- عنوان أو نص الصفحة يحوي "Recovery phone" / "Add recovery phone"

### 2. `_handle_recovery_phone_page(page, on_progress)`
- **إذا تم تعريف `RECOVERY_PHONE` في متغيرات البيئة**: يضغط زر "Add recovery phone"، يُدخل الرقم، ثم Next/Save.
- **إذا لم يكن هناك رقم**: يحاول تخطّي الصفحة (Skip/Not now)، أو الرجوع للوراء، أو التوجّه المباشر لصفحة Authenticator.

### 3. تكامل مع `_setup_new_authenticator`
قبل كل محاولة بحث عن حقل TOTP:
```python
if _is_recovery_phone_page(page.url, title, body_text):
    await _handle_recovery_phone_page(page, on_progress)
    # ثم أعد التوجيه لصفحة Authenticator
    continue
```

---

## خطوات التطبيق

1. **افتح ملف** `bot/services/google_account.py`
2. **أضف الدالتين** `_is_recovery_phone_page` و `_handle_recovery_phone_page` من الملف المرفق `google_account_patch_recovery.py` (في أعلى الملف بعد الـ imports).
3. **عدّل دالة `_setup_new_authenticator`** بإضافة الفحص في بداية كل محاولة (راجع `EXAMPLE_INTEGRATION` في الملف).
4. **أضف متغير البيئة في Railway:**
   ```
   RECOVERY_PHONE=+9647xxxxxxxxx
   ```
   (بصيغة دولية كاملة مع رمز الدولة)
5. **أعد النشر (Redeploy).**

## ملاحظة مهمة
- يُفضّل استخدام رقم تليفون صالح فعلاً يستقبل SMS من Google لأن Google قد يُرسل كود تحقق للرقم.
- إذا لم تُضف `RECOVERY_PHONE`، سيحاول البوت تخطّي الصفحة لكن Google قد يرفض ذلك في بعض الحالات.

## في حال استمرار المشكلة
أرسل لي `problem.txt` و `solution.txt` الجديد + screenshot لأرى ماذا يظهر بعد التحديث.
