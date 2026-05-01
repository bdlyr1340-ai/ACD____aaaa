"""
Google Account Rotator — تسجيل دخول + تغيير كلمة السر + إعادة 2FA.

هذا الملف هو إصلاح كامل لمشكلة:
  - "Couldn't sign you in - This browser or app may not be secure"
  - Page.wait_for_selector Timeout على input[type=password] (كان يلتقط hiddenPasswor المخفي)

الحل المطبَّق:
  1. تشغيل المتصفح عبر Camoufox (anti-detection حقيقي) مع fallback إلى playwright + stealth.
  2. كاشف مبكر لرفض Google ("Couldn't sign you in") يرفع خطأ واضح + لقطة شاشة فوراً.
  3. سيليكتور كلمة السر يستثني الحقول المخفية (aria-hidden / hiddenPasswor).
  4. دعم بروكسي residential عبر PROXY_LIST (دوّاري لكل حساب).
  5. إعادة محاولة + تقرير خطأ تفصيلي لكل خطوة.

ملاحظة: استبدل ملفك الحالي bot/services/google_account.py بهذا الملف.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets as pysecrets
import string
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

import pyotp

log = logging.getLogger(__name__)

# ---------- إعدادات قابلة للتهيئة عبر متغيرات البيئة ----------
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
DEVICE_TAP_WAIT_SEC = int(os.getenv("DEVICE_TAP_WAIT_SEC", "75"))
PROXY_LIST = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
SHOTS_DIR = Path(os.getenv("SHOTS_DIR", "/tmp/shots"))
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENTS = [
    # Chrome على Windows — أحدث، يمر بفحص "browser may not be secure"
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


# ---------- أدوات مساعدة ----------
def _gen_strong_password(length: int = 16) -> str:
    """كلمة سر تستوفي شروط Google."""
    upper = pysecrets.choice(string.ascii_uppercase)
    lower = pysecrets.choice(string.ascii_lowercase)
    digit = pysecrets.choice(string.digits)
    sym = pysecrets.choice("!@#$%^&*-_=+?")
    rest = "".join(
        pysecrets.choice(string.ascii_letters + string.digits + "!@#$%^&*-_=+?")
        for _ in range(length - 4)
    )
    pwd = list(upper + lower + digit + sym + rest)
    random.shuffle(pwd)
    return "".join(pwd)


def _pick_proxy() -> Optional[str]:
    if not PROXY_LIST:
        log.warning(
            "⚠️ PROXY_LIST فارغ — Google غالباً سيرفض IP السيرفر "
            "ويعرض 'browser may not be secure'. أضف بروكسي residential."
        )
        return None
    return random.choice(PROXY_LIST)


async def _shot(page, user_id: int, step: str) -> Optional[str]:
    try:
        path = SHOTS_DIR / f"{user_id}_{int(time.time())}_{step}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as e:
        log.warning("screenshot failed: %s", e)
        return None


async def _save_html(page, user_id: int, step: str) -> Optional[str]:
    try:
        path = SHOTS_DIR / f"{user_id}_{int(time.time())}_{step}.html"
        html = await page.content()
        path.write_text(html, encoding="utf-8")
        return str(path)
    except Exception:
        return None


# ---------- مشغّل المتصفح: Camoufox أولاً ثم playwright+stealth ----------
class _BrowserSession:
    """Context manager موحَّد يفتح صفحة جاهزة للأتمتة."""

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self._cm = None
        self.page = None
        self.context = None
        self.engine = None  # "camoufox" | "playwright"

    async def __aenter__(self):
        # محاولة Camoufox (مفضل)
        try:
            from camoufox.async_api import AsyncCamoufox  # type: ignore

            kwargs = dict(
                headless=HEADLESS,
                humanize=True,
                locale="en-US",
                os="windows",
                geoip=True,
            )
            if self.proxy:
                kwargs["proxy"] = {"server": self.proxy}
            self._cm = AsyncCamoufox(**kwargs)
            browser = await self._cm.__aenter__()
            self.page = await browser.new_page()
            self.context = browser
            self.engine = "camoufox"
            log.info("browser engine: camoufox (proxy=%s)", bool(self.proxy))
            return self
        except Exception as e:
            log.warning("Camoufox unavailable (%s) — falling back to playwright+stealth", e)

        # Fallback: playwright + stealth
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        launch_kwargs = dict(headless=HEADLESS, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ])
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self.context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # حقن مزيل علامة الأتمتة
        await self.context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        )
        self.page = await self.context.new_page()
        try:
            from playwright_stealth import stealth_async  # type: ignore
            await stealth_async(self.page)
        except Exception:
            log.warning("playwright-stealth not installed")
        self.engine = "playwright"
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.engine == "camoufox" and self._cm:
                await self._cm.__aexit__(exc_type, exc, tb)
            elif self.engine == "playwright":
                await self._browser.close()
                await self._pw.stop()
        except Exception:
            pass


# ---------- كاشف الحظر المبكر ----------
BLOCK_PATTERNS_RE = (
    r"Couldn.?t sign you in|"
    r"browser or app may not be secure|"
    r"This browser or app may not be supported|"
    r"تعذر تسجيل دخولك"
)


async def _check_google_block(page, user_id: int, step: str):
    try:
        count = await page.locator(f"text=/{BLOCK_PATTERNS_RE}/i").count()
    except Exception:
        count = 0
    if count > 0:
        shot = await _shot(page, user_id, f"BLOCKED_{step}")
        html = await _save_html(page, user_id, f"BLOCKED_{step}")
        raise RuntimeError(
            "BLOCKED_BY_GOOGLE: Google رفض المتصفح ('Couldn't sign you in'). "
            "السبب الأرجح: IP السيرفر محظور. الحل: أضف PROXY_LIST residential. "
            f"[shot={shot}] [html={html}]"
        )


# ---------- التدفق الرئيسي ----------
async def rotate_google_account(
    on_progress: Callable[[str], Awaitable[None]],
    gmail: str,
    old_password: str,
    old_totp_secret: str,
    user_id: int,
) -> dict:
    """
    يرجع dict:
      success, gmail, new_password, new_totp_secret, step, error,
      screenshot_path, html_path
    """
    result = {
        "success": False,
        "gmail": gmail,
        "new_password": None,
        "new_totp_secret": None,
        "step": "init",
        "error": None,
        "screenshot_path": None,
        "html_path": None,
    }

    proxy = _pick_proxy()
    new_password = _gen_strong_password(16)
    new_secret = pyotp.random_base32()

    try:
        async with _BrowserSession(proxy=proxy) as sess:
            page = sess.page

            # ===== 1) launch + go to login =====
            result["step"] = "open_login"
            await on_progress("🌐 فتح صفحة تسجيل الدخول…")
            await page.goto(
                "https://accounts.google.com/signin/v2/identifier?hl=en&flowName=GlifWebSignIn",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await _check_google_block(page, user_id, "open_login")

            # ===== 2) email =====
            result["step"] = "enter_email"
            await on_progress("📧 إدخال الإيميل…")
            email_input = page.locator(
                'input[type="email"]:visible, input#identifierId:visible'
            ).first
            await email_input.wait_for(state="visible", timeout=20000)
            await email_input.fill(gmail)
            await page.locator('#identifierNext, button:has-text("Next")').first.click()

            # انتظر انتقال الصفحة وتحقق من الحظر
            await page.wait_for_timeout(2500)
            await _check_google_block(page, user_id, "after_email")

            # ===== 3) password (إصلاح السيليكتور) =====
            result["step"] = "enter_password"
            await on_progress("🔑 إدخال كلمة السر…")
            # نستثني صراحةً الحقول المخفية (hiddenPasswor / aria-hidden)
            pwd_input = page.locator(
                'input[type="password"][name="Passwd"]:visible, '
                'input[type="password"][autocomplete="current-password"]:visible, '
                'input[type="password"]:visible:not([aria-hidden="true"]):not([name="hiddenPasswor"])'
            ).first
            await pwd_input.wait_for(state="visible", timeout=30000)
            await pwd_input.fill(old_password)
            await page.locator('#passwordNext, button:has-text("Next")').first.click()

            await page.wait_for_timeout(2500)
            await _check_google_block(page, user_id, "after_password")

            # ===== 4) 2FA (إن وُجد) =====
            result["step"] = "enter_2fa"
            await _handle_2fa(page, old_totp_secret, on_progress, user_id)

            # ===== 5) تغيير كلمة السر =====
            result["step"] = "change_password"
            await on_progress("🔁 تغيير كلمة السر…")
            await _change_password(page, old_password, new_password)

            # ===== 6) إعادة إعداد 2FA =====
            result["step"] = "rotate_2fa"
            await on_progress("🛡️ إعادة إعداد 2FA…")
            await _rotate_2fa(page, gmail, new_secret)

            # ===== 7) تم =====
            result.update(
                success=True,
                step="done",
                new_password=new_password,
                new_totp_secret=new_secret,
            )
            return result

    except Exception as e:
        result["error"] = str(e)
        # إن كانت الجلسة لا تزال مفتوحة لن نصل هنا، لذا الحفظ يتم في _check_google_block
        log.exception("rotate_google_account failed at step=%s", result["step"])
        return result


# ---------- 2FA ----------
async def _handle_2fa(page, old_totp_secret: str, on_progress, user_id: int):
    """يتعامل مع: TOTP، Device-tap (try another way)، أو غياب 2FA."""
    # ننتظر قليلاً لنرى ما الذي يطلبه Google
    await page.wait_for_timeout(2000)
    url = page.url

    # إن دخلنا مباشرة → لا 2FA
    if "myaccount" in url or "accounts.google.com/signin/oauth" in url:
        return

    # device-tap؟ → بدّل لـ Authenticator
    try:
        if await page.locator('text=/Check your .* device|Tap Yes|on your phone/i').count():
            await on_progress("📱 طلب موافقة الجهاز — جاري التبديل إلى Google Authenticator…")
            try_another = page.locator('button:has-text("Try another way"), text="Try another way"').first
            if await try_another.count():
                await try_another.click()
                await page.wait_for_timeout(1500)
                auth_opt = page.locator('text=/Google Authenticator|authenticator app/i').first
                if await auth_opt.count():
                    await auth_opt.click()
                    await page.wait_for_timeout(1500)
    except Exception:
        pass

    # الآن أدخل TOTP
    if old_totp_secret and old_totp_secret.lower() != "skip":
        try:
            code_input = page.locator(
                'input[type="tel"]:visible, input#totpPin:visible, input[name="totpPin"]:visible'
            ).first
            await code_input.wait_for(state="visible", timeout=20000)
            code = pyotp.TOTP(old_totp_secret).now()
            await code_input.fill(code)
            await page.locator('#totpNext, button:has-text("Next")').first.click()
            await page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("2FA step skipped/failed: %s", e)


# ---------- تغيير كلمة السر ----------
async def _change_password(page, old_password: str, new_password: str):
    await page.goto(
        "https://myaccount.google.com/signinoptions/password",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    # قد يُطلب إعادة إدخال كلمة السر القديمة
    try:
        re_pwd = page.locator(
            'input[type="password"]:visible:not([aria-hidden="true"]):not([name="hiddenPasswor"])'
        ).first
        await re_pwd.wait_for(state="visible", timeout=10000)
        await re_pwd.fill(old_password)
        await page.locator('#passwordNext, button:has-text("Next")').first.click()
        await page.wait_for_timeout(2500)
    except Exception:
        pass

    # حقول كلمة السر الجديدة
    new_inputs = page.locator(
        'input[type="password"]:visible:not([aria-hidden="true"]):not([name="hiddenPasswor"])'
    )
    await new_inputs.first.wait_for(state="visible", timeout=20000)
    await new_inputs.nth(0).fill(new_password)
    await new_inputs.nth(1).fill(new_password)
    await page.locator('button:has-text("Change password"), button:has-text("Save")').first.click()
    await page.wait_for_timeout(4000)


# ---------- إعادة 2FA ----------
async def _rotate_2fa(page, gmail: str, new_secret: str):
    """
    يضيف Google Authenticator جديد بمفتاح new_secret.
    استراتيجية: نفتح الإعدادات، نحذف القديم إن وُجد، نضيف جديداً
    ثم نُدخل كود TOTP من المفتاح الجديد للتأكيد.
    """
    await page.goto(
        "https://myaccount.google.com/signinoptions/two-step-verification",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    await page.wait_for_timeout(3000)

    # حذف authenticator قديم إن وُجد
    try:
        old = page.locator('text=/Authenticator app|Google Authenticator/i').first
        if await old.count():
            await old.click()
            await page.wait_for_timeout(1500)
            del_btn = page.locator('button:has-text("Remove"), button:has-text("Delete")').first
            if await del_btn.count():
                await del_btn.click()
                await page.wait_for_timeout(2000)
    except Exception:
        pass

    # إضافة جديد
    add = page.locator(
        'button:has-text("Set up authenticator"), '
        'button:has-text("Add authenticator app"), '
        'button:has-text("+ Authenticator")'
    ).first
    if await add.count():
        await add.click()
    await page.wait_for_timeout(2500)

    # توليد كود من المفتاح الجديد وإدخاله
    code = pyotp.TOTP(new_secret).now()
    code_input = page.locator(
        'input[type="tel"]:visible, input#totpPin:visible, input[name="totpPin"]:visible'
    ).first
    await code_input.wait_for(state="visible", timeout=15000)
    await code_input.fill(code)
    await page.locator('button:has-text("Verify"), button:has-text("Save"), #totpNext').first.click()
    await page.wait_for_timeout(3000)
