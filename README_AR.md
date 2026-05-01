# Google Account Rotator Bot

بوت تيليجرام يقوم تلقائياً بـ:
1. تسجيل الدخول إلى حساب Google باستخدام (إيميل + كلمة سر + مفتاح 2FA).
2. تجاوز شاشة "Tap on your phone" بالتحويل تلقائياً إلى Google Authenticator.
3. تغيير كلمة السر إلى كلمة قوية جديدة.
4. إعادة إعداد المصادقة الثنائية وإنشاء مفتاح 2FA جديد.
5. إرسال البيانات الجديدة (إيميل + كلمة السر + مفتاح 2FA) للمستخدم.
6. عند أي فشل: إرسال **لقطة شاشة + اسم الخطوة + نص الخطأ** إلى المستخدم وإلى الأدمن.

## المتغيرات البيئية المطلوبة

| المتغير | الوصف |
|---|---|
| `BOT_TOKEN` | توكن البوت من BotFather |
| `DATABASE_URL` | رابط Postgres (Railway) |
| `ADMIN_IDS` | معرّفات الأدمن مفصولة بفواصل |
| `PROXY_URL` | (اختياري) `http://user:pass@host:port` |
| `DEFAULT_CREDITS` | الرصيد الافتراضي (3) |
| `ROTATE_COST` | تكلفة كل عملية (1) |
| `MAX_BULK_ACCOUNTS` | الحد الأقصى للقائمة (30) |
| `ROTATE_TIMEOUT_SEC` | المهلة لكل حساب (300) |

## التشغيل المحلي

```bash
pip install -r requirements.txt
playwright install chromium
python main.py
```

## Railway

ملفات `Procfile` و `Dockerfile` و `railway.json` جاهزة. ارفع المستودع و عرّف المتغيرات أعلاه.

## استخدام البوت

- **حساب واحد:** اضغط زر *🔐 تغيير حساب* ثم أرسل:
  ```
  email@gmail.com | OldPassword | OLD2FASECRET
  ```
- **قائمة حسابات:** اضغط *📋 قائمة حسابات* وأرسل ملف نصي / رسالة فيها سطر لكل حساب بنفس الصيغة.
- إن لم يوجد 2FA على الحساب: ضع `skip` بدلاً من المفتاح.

## أوامر

`/start /me /ref /qd /use /help`  
أوامر الأدمن: `/admin /stats /addcredit /ban /unban /broadcast /genkey /listkeys`

## ملاحظات هامة

- Google قد يطلب أحياناً تأكيد عبر Recovery Email/Phone — عندها يبلّغ البوت عن الحاجة لتدخّل يدوي ويرسل لقطة الشاشة بدلاً من الفشل الصامت.
- يُنصح باستخدام بروكسي residential لتقليل احتمالات الحظر من Google.
- مفتاح TOTP في الإدخال يجب أن يكون نصاً Base32 (مثل `JBSWY3DPEHPK3PXP`)، وليس الكود السداسي المؤقت.
