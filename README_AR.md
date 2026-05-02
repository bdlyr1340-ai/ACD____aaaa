# ✨ تحديث v11 — البوت الاحترافي الكامل

## 📦 محتوى الملف المضغوط

```
services/
  └── google_account.py          ← ملف محدّث (استبدل القديم بالكامل)
handlers/
  ├── rotate_callbacks_patch.py  ← مثال handler التدوير الجديد
  ├── custom_password.py         ← زر "تعيين كلمة سر مخصصة"
  └── create_gmail.py            ← زر "إنشاء حساب Gmail جديد"
README.md                        ← هذا الملف
```

---

## 🎯 التحسينات الرئيسية

### 1️⃣ `services/google_account.py` — إصلاحات جوهرية

#### ✅ إرسال البيانات فوراً عند استخراج المفتاح
بمجرد استخراج مفتاح TOTP من Google، البوت يرسل لك تلقائياً:
- 🔑 **مفتاح 2FA السري**
- 🔗 **رابط 2fa.fb.tools/<SECRET>**
- 🔢 **الرمز الحالي**

حتى لو فشل أي شيء بعد ذلك، أنت تملك كل البيانات.

#### ✅ لا فشل عند خطأ "صفحة TOTP لم تظهر"
المفتاح أرسل لك مسبقاً → نعتبرها نجاح جزئي ونكمل.

#### ✅ تأكيد التفعيل النهائي
بعد إدخال الرمز، يضغط تلقائياً على Done/Turn on/تفعيل لإكمال 2SV.

#### ✅ دعم كلمة سر مخصصة
معامل جديد `custom_new_password` في `rotate_google_account()`. إذا تم تمريره، يُستخدم بدل الافتراضي.

#### ✅ `verify_new_2fa` غير قاتل
لو فشل تسجيل الدخول للتحقق، البوت يكمل بنجاح.

---

### 2️⃣ `handlers/custom_password.py` — زر تعيين كلمة سر مخصصة

**الفكرة**: المستخدم يحدد كلمة سر يستخدمها البوت لكل عمليات تغيير كلمة السر (بدل العشوائية).

**التخزين**: ملف JSON في `/tmp/custom_passwords.json` (يمكن تغييره عبر `CUSTOM_PWD_STORE` env).

**الاستخدام في كودك**:
```python
from bot.handlers.custom_password import (
    cmd_set_custom_password, cmd_clear_custom_password,
    handle_password_input, get_custom_password,
)

# عند ضغط زر "🔐 تعيين كلمة سر مخصصة"
await cmd_set_custom_password(bot, message)

# في message handler العام:
async def on_message(message):
    # محاولة التقاط رد كلمة السر أولاً
    if await handle_password_input(bot, message):
        return
    # باقي الـ handlers...
```

**في rotate.py**:
```python
custom_pwd = get_custom_password(user_id) or ""
result = await rotate_google_account(
    ...,
    custom_new_password=custom_pwd,
)
```

---

### 3️⃣ `handlers/create_gmail.py` — زر إنشاء حساب Gmail

**الفكرة**: شبه آلي مع تدقيق بشري عند الحاجة (CAPTCHA، رقم هاتف، SMS).

**التدفق**:
1. يفتح Camoufox/Patchright
2. يدخل بيانات وهمية (يمكن تخصيصها)
3. عند طلب رقم هاتف → يستخدم `RECOVERY_PHONE` أو يطلب من المستخدم
4. عند طلب SMS → ينتظر رد المستخدم
5. عند النجاح → يرسل: 📧 الإيميل + 🔐 كلمة السر

**مهم**: 
- Google يطلب رقم هاتف دائماً تقريباً
- يحتاج `human_input_provider` (FSM) ليطلب من المستخدم تدخل
- النموذج المرفق يحتاج تكييف حسب framework البوت عندك (aiogram/telebot/etc)

---

## 🔧 خطوات التركيب

### الخطوة 1: استبدل ملف google_account.py
```
services/google_account.py  →  bot/services/google_account.py
```

### الخطوة 2: ضع ملفات الـ handlers
```
handlers/custom_password.py  →  bot/handlers/custom_password.py
handlers/create_gmail.py     →  bot/handlers/create_gmail.py
```

### الخطوة 3: عدّل rotate.py عندك
انسخ منطق `on_credentials_ready` من `rotate_callbacks_patch.py` إلى rotate.py الحالي:
- أضف معالجة `totp_url` و `totp_code`
- مرّر `custom_new_password=get_custom_password(user_id) or ""`

### الخطوة 4: أضف الأزرار في keyboard البوت
```python
# في keyboard الرئيسي (مثال aiogram):
[
    ["🔄 تدوير حساب", "📧 إنشاء حساب Gmail"],
    ["🔐 تعيين كلمة سر مخصصة", "🗑 إلغاء كلمة السر المخصصة"],
]
```

### الخطوة 5: متغيرات البيئة (Railway)
```
NEW_PASSWORD=VJ77X2305xx30j5         # كلمة السر الافتراضية
RECOVERY_PHONE=+9647xxxxxxxxx        # رقم هاتف الاستعادة
FALLBACK_PHONE=+9647728257333        # احتياطي
CUSTOM_PWD_STORE=/data/cust_pwd.json # (اختياري) مكان حفظ كلمات السر المخصصة
```

### الخطوة 6: redeploy

---

## 📊 ما يستلمه المستخدم بعد كل عملية تدوير

```
🚀 بدء تدوير الحساب
📧 example@gmail.com
🔑 كلمة السر القديمة: xxxxx

[تقدم العملية...]

✅ تم تغيير كلمة السر
🆕 الجديدة: VJ77X2305xx30j5

🔑 مفتاح 2FA السري:
gyb422etjenljh2prwb4wiawxd5u7gvw

🔗 رابط 2FA:
https://2fa.fb.tools/gyb422etjenljh2prwb4wiawxd5u7gvw

🔢 الرمز الحالي: 123456

✅ تمت العملية بنجاح
[ملخص نهائي بكل البيانات]
```

---

## ⚠️ ملاحظات

- **create_gmail.py** يحتاج تكييف حسب framework البوت (FSM للحوار)
- **custom_password.py** يستخدم تخزين بسيط — لإنتاج جدّي استخدم Redis/PostgreSQL
- جميع الـ handlers أمثلة قابلة للنسخ — كيّفها حسب بنيتك

اختصاراً: استبدل `google_account.py` فقط، وكل التحسينات الجوهرية ستعمل. الـ handlers اختيارية لإضافة الأزرار الجديدة.
