"""Google Account Rotator — Pro Edition.

Features added in this version:
  • Auto-detect when an account has NO 2FA enabled → enable Authenticator
    on-the-fly and return the new secret to the user.
  • Wait for "Device-tap" approval (configurable timeout) before falling
    back to TOTP. If the user explicitly DENIES the tap → report it.
  • Stronger error reporting (screenshot + html + step name).
  • Cleaner step list.

Public entry point:
    rotate_google_account(on_progress, gmail, old_password,
                          old_totp_secret, user_id)
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import secrets
import string
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import pyotp

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]

# ---------------------------------------------------------------------------
# Step labels
# ---------------------------------------------------------------------------
ROTATION_STEPS: List[str] = [
    "launch_browser",
    "google_login_email",
    "google_login_password",
    "google_login_2fa",
    "open_security_page",
    "change_password",
    "open_2fa_settings",
    "enable_new_authenticator",
    "verify_new_2fa",
    "done",
]

STEP_LABELS_AR: Dict[str, str] = {
    "launch_browser":           "تشغيل المتصفح",
    "google_login_email":       "إدخال البريد الإلكتروني",
    "google_login_password":    "إدخال كلمة السر",
    "google_login_2fa":         "المصادقة الثنائية",
    "wait_device_tap":          "انتظار موافقة الهاتف",
    "open_security_page":       "فتح إعدادات الأمان",
    "change_password":          "تغيير كلمة السر",
    "open_2fa_settings":        "فتح إعدادات 2FA",
    "enable_new_authenticator": "إضافة Authenticator جديد",
    "enable_initial_2fa":       "تفعيل 2FA لأول مرة",
    "verify_new_2fa":           "تأكيد المصادقة الجديدة",
    "done":                     "مكتمل",
}

SHOTS_DIR = os.environ.get("SHOTS_DIR", "/tmp/shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

# How long we wait (seconds) for the user to tap "Yes" on their phone
# before we attempt to switch to the Authenticator-code method.
DEVICE_TAP_WAIT_SEC = int(os.environ.get("DEVICE_TAP_WAIT_SEC", "75"))


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _generate_strong_password(length: int = 16) -> str:
    if length < 12:
        length = 12
    upper, lower = string.ascii_uppercase, string.ascii_lowercase
    digits, symbols = string.digits, "!@#$%^&*()-_=+"
    pwd = [secrets.choice(p) for p in (upper, lower, digits, symbols)]
    pwd += [secrets.choice(upper + lower + digits + symbols) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def _now_totp(secret: str) -> str:
    return pyotp.TOTP(secret.replace(" ", "").upper()).now()


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

async def _capture(page, user_id: int, tag: str) -> Dict[str, Optional[str]]:
    ts = int(time.time() * 1000)
    base = f"{user_id}_{ts}_{tag}"
    shot_path = os.path.join(SHOTS_DIR, f"{base}.png")
    html_path = os.path.join(SHOTS_DIR, f"{base}.html")
    out: Dict[str, Optional[str]] = {"screenshot_path": None, "html_path": None}
    try:
        await page.screenshot(path=shot_path, full_page=True, timeout=15_000)
        out["screenshot_path"] = shot_path
    except Exception as exc:
        log.warning("Screenshot failed: %s", exc)
    try:
        html = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        out["html_path"] = html_path
    except Exception as exc:
        log.warning("HTML dump failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Human-like helpers
# ---------------------------------------------------------------------------

async def _hd(min_s: float = 0.4, max_s: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _type_human(page, selector: str, text: str) -> None:
    await page.click(selector)
    await _hd(0.2, 0.5)
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))


async def _click_text(page, substrings: List[str], timeout_ms: int = 4000) -> bool:
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        for substr in substrings:
            try:
                loc = page.locator(
                    "xpath=//*[self::button or self::div or self::span or self::a or @role='button']"
                    "[contains(translate(normalize-space(.),"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{substr.lower()}')]"
                ).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2000)
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.3)
    return False


async def _page_text(page) -> str:
    try:
        return (await page.inner_text("body")).lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Device-tap handling
# ---------------------------------------------------------------------------

DEVICE_TAP_KEYWORDS = [
    "tap yes", "check your", "device-tap", "open the gmail app",
    "tap your", "اضغط نعم", "فتح تطبيق", "تحقق من جهازك",
]
DEVICE_TAP_DENIED_KEYWORDS = [
    "you said it wasn’t you", "you said it wasn't you",
    "request was denied", "didn’t recognize", "didn't recognize",
    "تم رفض", "لم تتعرف",
]


async def _is_device_tap_screen(page) -> bool:
    body = await _page_text(page)
    return any(k in body for k in DEVICE_TAP_KEYWORDS)


async def _is_tap_denied(page) -> bool:
    body = await _page_text(page)
    return any(k in body for k in DEVICE_TAP_DENIED_KEYWORDS)


async def _wait_for_device_tap(page, on_progress: ProgressCallback) -> bool:
    """Wait until the user taps Yes on their phone (or until timeout).

    Returns True if approved (page progressed past the tap screen).
    Raises RuntimeError if the tap was explicitly denied.
    """
    await on_progress("step:wait_device_tap")
    deadline = time.time() + DEVICE_TAP_WAIT_SEC
    while time.time() < deadline:
        await asyncio.sleep(2.5)
        if await _is_tap_denied(page):
            raise RuntimeError("تم رفض إشعار التحقق من الهاتف")
        if not await _is_device_tap_screen(page):
            return True  # page progressed → approved
    return False


async def _switch_to_totp_method(page) -> bool:
    if not await _click_text(
        page, ["try another way", "طريقة أخرى", "جرب طريقة"], 4000
    ):
        return False
    await _hd(0.8, 1.6)
    if not await _click_text(page, [
        "google authenticator", "authenticator app",
        "code from your authenticator", "تطبيق المصادقة",
    ], 4000):
        return False
    await _hd(0.8, 1.6)
    return True


# ---------------------------------------------------------------------------
# Browser launch
# ---------------------------------------------------------------------------

async def _launch_browser(pw):
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    proxy_cfg = None
    if proxy_url:
        u = urlparse(proxy_url)
        proxy_cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            proxy_cfg["username"] = u.username
        if u.password:
            proxy_cfg["password"] = u.password

    browser = await pw.chromium.launch(
        headless=True,
        proxy=proxy_cfg,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"
    )
    ctx = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    page = await ctx.new_page()
    page.set_default_timeout(20_000)
    return browser, ctx, page


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def _do_login(page, gmail: str, old_password: str,
                    old_totp_secret: str, on_progress: ProgressCallback) -> Dict[str, Any]:
    """Login. Returns dict {had_2fa: bool}."""
    info = {"had_2fa": False}

    await on_progress("step:google_login_email")
    await page.goto(
        "https://accounts.google.com/signin/v2/identifier?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(1, 2)
    email_sel = 'input[type="email"], input#identifierId'
    await page.wait_for_selector(email_sel, timeout=20_000)
    await _type_human(page, email_sel, gmail)
    await _hd(0.4, 0.9)
    await page.keyboard.press("Enter")

    # Password
    await on_progress("step:google_login_password")
    pwd_sel = 'input[type="password"][name="Passwd"], input[type="password"]'
    await page.wait_for_selector(pwd_sel, timeout=20_000, state="visible")
    await _hd(0.6, 1.2)
    await _type_human(page, pwd_sel, old_password)
    await _hd(0.4, 0.9)
    await page.keyboard.press("Enter")
    await _hd(2.5, 3.5)

    body = await _page_text(page)
    if any(k in body for k in [
        "wrong password", "couldn't find your google account",
        "couldn’t find your google account", "wasn't recognized",
        "كلمة المرور غير صحيحة",
    ]):
        raise RuntimeError("كلمة السر القديمة غير صحيحة")

    # Detect challenge type
    await on_progress("step:google_login_2fa")
    cur = page.url
    body = await _page_text(page)

    on_challenge = ("signin/challenge" in cur) or any(
        k in body for k in [
            "2-step verification", "verify it’s you", "verify it's you",
            "enter the code", "tap yes", "check your",
            "التحقق بخطوتين", "تحقق من هويتك",
        ]
    )

    if not on_challenge:
        # No 2FA at all on this account
        info["had_2fa"] = False
        # skip "stay signed in" prompts
        for label in ["not now", "ليس الآن", "skip", "تخطي"]:
            if await _click_text(page, [label], 1500):
                await _hd(0.6, 1.2)
                break
        return info

    info["had_2fa"] = True

    # Device-tap challenge?
    if await _is_device_tap_screen(page):
        if old_totp_secret:
            # User gave us a TOTP secret → switch immediately
            await _switch_to_totp_method(page)
        else:
            # No TOTP — wait for the user to tap Yes on the phone
            approved = await _wait_for_device_tap(page, on_progress)
            if not approved:
                raise RuntimeError(
                    "انتهى وقت انتظار موافقة الهاتف. أعد المحاولة "
                    "أو أرسل مفتاح Authenticator (TOTP)."
                )

    # If we still have a TOTP input field, fill it
    totp_input_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )
    needs_totp = False
    try:
        await page.wait_for_selector(totp_input_sel, timeout=4000, state="visible")
        needs_totp = True
    except Exception:
        needs_totp = False

    if needs_totp:
        if not old_totp_secret:
            raise RuntimeError(
                "الحساب يطلب رمز Authenticator، أرسل المفتاح السري أيضاً."
            )
        code = _now_totp(old_totp_secret)
        await _type_human(page, totp_input_sel, code)
        await _hd(0.4, 0.9)
        await page.keyboard.press("Enter")
        await _hd(2.5, 4)

    # Skip "Stay signed in"
    for label in ["not now", "ليس الآن", "skip", "تخطي"]:
        if await _click_text(page, [label], 1500):
            await _hd(0.6, 1.2)
            break

    cur = page.url
    if "signin/challenge" in cur or "signin/v2" in cur:
        raise RuntimeError("الحساب يطلب تأكيد إضافي (Recovery) — يلزم تدخّل يدوي")

    return info


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------

async def _change_password(page, on_progress: ProgressCallback) -> str:
    await on_progress("step:open_security_page")
    new_pwd = _generate_strong_password(16)
    await page.goto(
        "https://myaccount.google.com/signinoptions/password?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)

    pwd_sel = 'input[type="password"]'
    try:
        await page.wait_for_selector(pwd_sel, timeout=15_000, state="visible")
    except Exception:
        raise RuntimeError("لم تُفتح صفحة تغيير كلمة السر")

    await on_progress("step:change_password")
    fields = page.locator('input[type="password"]')
    n = await fields.count()
    if n < 2:
        await _hd(2, 3)
        n = await fields.count()
    if n < 2:
        raise RuntimeError("حقول كلمة السر الجديدة غير ظاهرة")

    await fields.nth(0).click(); await _hd(0.2, 0.5)
    await page.keyboard.type(new_pwd, delay=60); await _hd(0.4, 0.9)
    await fields.nth(1).click(); await _hd(0.2, 0.5)
    await page.keyboard.type(new_pwd, delay=60); await _hd(0.4, 0.9)

    if not await _click_text(
        page, ["change password", "save", "تغيير كلمة المرور", "حفظ"], 4000
    ):
        await page.keyboard.press("Enter")
    await _hd(3, 5)

    body = await _page_text(page)
    if any(k in body for k in ["password changed", "تم تغيير", "تم الحفظ"]):
        return new_pwd
    if "/signinoptions/password" not in page.url:
        return new_pwd
    raise RuntimeError("تعذّر تأكيد تغيير كلمة السر")


# ---------------------------------------------------------------------------
# 2FA setup (works whether or not 2FA was previously enabled)
# ---------------------------------------------------------------------------

async def _setup_authenticator(page, on_progress: ProgressCallback,
                               had_2fa: bool) -> str:
    label_step = "enable_new_authenticator" if had_2fa else "enable_initial_2fa"
    await on_progress(f"step:{label_step}")

    await page.goto(
        "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)

    # If 2FA isn't on yet, Google may show a "Get started" / "Turn on" wizard.
    if not had_2fa:
        await _click_text(page, [
            "get started", "turn on", "بدء", "تفعيل",
        ], 4000)
        await _hd(1.5, 2.5)

    await _click_text(page, ["authenticator", "تطبيق المصادقة"], 5000)
    await _hd(1.5, 2.5)
    await _click_text(page, [
        "set up authenticator", "set up", "+ add authenticator",
        "get started", "إعداد", "بدء",
    ], 5000)
    await _hd(1.5, 2.5)

    revealed = await _click_text(page, [
        "can't scan it", "can’t scan it", "can't scan",
        "لا يمكنك المسح", "show secret", "show key",
    ], 5000)
    await _hd(1.0, 2.0)

    secret = ""
    if revealed:
        try:
            html = await page.content()
            m = re.search(
                r"\b([A-Z2-7]{4}\s?[A-Z2-7]{4}\s?[A-Z2-7]{4}\s?[A-Z2-7]{4}[A-Z2-7\s]*)\b",
                html,
            )
            if m:
                secret = m.group(1).replace(" ", "").upper()
        except Exception:
            pass
    if not secret:
        raise RuntimeError("تعذّر استخراج مفتاح Authenticator الجديد من Google")

    await _click_text(page, ["next", "التالي"], 5000)
    await _hd(1.0, 2.0)

    code = _now_totp(secret)
    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )
    await page.wait_for_selector(code_sel, timeout=15_000, state="visible")
    await _type_human(page, code_sel, code)
    await _hd(0.5, 1.2)
    if not await _click_text(page, ["verify", "next", "تحقق", "التالي"], 4000):
        await page.keyboard.press("Enter")
    await _hd(2, 4)

    # If this was an initial enable, finalize the wizard
    if not had_2fa:
        await _click_text(page, [
            "turn on", "done", "finish", "تفعيل", "تم", "إنهاء",
        ], 4000)
        await _hd(1, 2)

    return secret


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

async def _verify_new_2fa(page, gmail: str, new_password: str,
                          new_secret: str,
                          on_progress: ProgressCallback) -> bool:
    await on_progress("step:verify_new_2fa")
    try:
        await page.goto(
            "https://accounts.google.com/Logout",
            wait_until="domcontentloaded", timeout=20_000,
        )
        await _hd(2, 3)
    except Exception:
        pass
    await page.goto(
        "https://accounts.google.com/signin/v2/identifier?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(1.5, 2.5)
    await _type_human(page, 'input[type="email"]', gmail)
    await page.keyboard.press("Enter")
    await _hd(2, 3)
    await page.wait_for_selector('input[type="password"]', timeout=20_000, state="visible")
    await _type_human(page, 'input[type="password"]', new_password)
    await page.keyboard.press("Enter")
    await _hd(2.5, 4)

    if await _is_device_tap_screen(page):
        await _switch_to_totp_method(page)

    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )
    try:
        await page.wait_for_selector(code_sel, timeout=15_000, state="visible")
    except Exception:
        return True
    await _type_human(page, code_sel, _now_totp(new_secret))
    await page.keyboard.press("Enter")
    await _hd(2, 4)
    return "myaccount" in page.url or "signin" not in page.url


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def rotate_google_account(
    on_progress: ProgressCallback,
    *,
    gmail: str,
    old_password: str,
    old_totp_secret: str,
    user_id: int,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "gmail": gmail,
        "new_password": None,
        "new_totp_secret": None,
        "had_2fa_before": None,
        "step": "launch_browser",
        "error": None,
        "screenshot_path": None,
        "html_path": None,
    }

    if not gmail or "@" not in gmail:
        result["error"] = "إيميل غير صالح"
        return result
    if not old_password:
        result["error"] = "كلمة السر القديمة مطلوبة"
        return result
    if old_totp_secret:
        try:
            pyotp.TOTP(old_totp_secret.replace(" ", "").upper()).now()
        except Exception as exc:
            result["error"] = f"مفتاح 2FA القديم غير صالح: {exc}"
            return result

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result["error"] = (
            "Playwright غير مُثبَّت. شغّل: "
            "pip install playwright && playwright install chromium"
        )
        return result

    pw_cm = async_playwright()
    pw = await pw_cm.__aenter__()
    browser = ctx = page = None
    current_step = "launch_browser"

    async def _progress_wrap(s: str) -> None:
        nonlocal current_step
        if s.startswith("step:"):
            current_step = s.split(":", 1)[1]
        try:
            await on_progress(_format_progress(current_step))
        except Exception:
            pass

    try:
        await _progress_wrap("step:launch_browser")
        browser, ctx, page = await _launch_browser(pw)

        login_info = await _do_login(
            page, gmail, old_password, old_totp_secret, _progress_wrap
        )
        result["had_2fa_before"] = login_info["had_2fa"]

        new_password = await _change_password(page, _progress_wrap)
        result["new_password"] = new_password

        new_secret = await _setup_authenticator(
            page, _progress_wrap, had_2fa=login_info["had_2fa"]
        )
        result["new_totp_secret"] = new_secret

        await _verify_new_2fa(
            page, gmail, new_password, new_secret, _progress_wrap
        )

        await _progress_wrap("step:done")
        result["success"] = True
        result["step"] = "done"
        return result

    except Exception as exc:
        log.exception("rotate_google_account failed at step=%s", current_step)
        result["step"] = current_step
        result["error"] = str(exc) or exc.__class__.__name__
        if page is not None:
            cap = await _capture(page, user_id, current_step)
            result["screenshot_path"] = cap["screenshot_path"]
            result["html_path"] = cap["html_path"]
        return result

    finally:
        for closer in (page, ctx, browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        try:
            await pw_cm.__aexit__(None, None, None)
        except Exception:
            pass


def _format_progress(step: str) -> str:
    label = STEP_LABELS_AR.get(step, step)
    try:
        idx = ROTATION_STEPS.index(step) + 1
    except ValueError:
        idx = 0
    bar = "".join(
        "🟢" if i <= idx else "⚪️" for i, _ in enumerate(ROTATION_STEPS, 1)
    )
    return f"{bar}\n\n🔧 الخطوة {idx}/{len(ROTATION_STEPS)}: *{label}*"
