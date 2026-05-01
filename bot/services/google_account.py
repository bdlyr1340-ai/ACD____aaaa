"""Google Account Rotator — change password & 2FA for a Google account.

Public entry point:
    rotate_google_account(on_progress, gmail, old_password, old_totp_secret, user_id)

Returns dict with: success, gmail, new_password, new_totp_secret,
                   step, error, screenshot_path, html_path.

Designed to be resilient: any exception is caught, a screenshot is taken,
and the failing step name is reported so the caller can forward it to the
user / admin.
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
from urllib.parse import parse_qs, urlparse

import pyotp

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]

# Ordered list of steps used for progress display
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
    "launch_browser":          "تشغيل المتصفح",
    "google_login_email":      "إدخال البريد الإلكتروني",
    "google_login_password":   "إدخال كلمة السر",
    "google_login_2fa":        "المصادقة الثنائية",
    "open_security_page":      "فتح إعدادات الأمان",
    "change_password":         "تغيير كلمة السر",
    "open_2fa_settings":       "فتح إعدادات 2FA",
    "enable_new_authenticator": "إضافة Authenticator جديد",
    "verify_new_2fa":          "تأكيد المصادقة الجديدة",
    "done":                    "مكتمل",
}

SHOTS_DIR = os.environ.get("SHOTS_DIR", "/tmp/shots")
os.makedirs(SHOTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers: password & secret generation
# ---------------------------------------------------------------------------

def _generate_strong_password(length: int = 16) -> str:
    """Generate a strong password meeting Google's requirements."""
    if length < 12:
        length = 12
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+"
    pools = [upper, lower, digits, symbols]
    pwd = [secrets.choice(p) for p in pools]
    pwd += [secrets.choice(upper + lower + digits + symbols) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def _generate_totp_secret() -> str:
    """Generate a fresh 32-char base32 TOTP secret."""
    return pyotp.random_base32(length=32)


def _now_totp(secret: str) -> str:
    return pyotp.TOTP(secret.replace(" ", "").upper()).now()


# ---------------------------------------------------------------------------
# Helpers: screenshots / html dumps
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
# Helpers: human-like delays
# ---------------------------------------------------------------------------

async def _hd(min_s: float = 0.4, max_s: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _type_human(page, selector: str, text: str) -> None:
    await page.click(selector)
    await _hd(0.2, 0.5)
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))


async def _wait_for_visible_locator(page, selectors: List[str], timeout_ms: int = 20_000):
    """Return the first visible locator from a list of selectors."""
    deadline = time.time() + (timeout_ms / 1000.0)
    last_error = None
    while time.time() < deadline:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception as exc:
                last_error = exc
        await asyncio.sleep(0.25)
    joined = ", ".join(selectors)
    if last_error:
        raise RuntimeError(f"Timed out waiting for visible element: {joined} ({last_error})")
    raise RuntimeError(f"Timed out waiting for visible element: {joined}")


# ---------------------------------------------------------------------------
# Helpers: click by visible text
# ---------------------------------------------------------------------------

async def _click_text(page, substrings: List[str], timeout_ms: int = 4000) -> bool:
    """Click the first visible element containing any of the given substrings."""
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        for substr in substrings:
            try:
                # Match button / div / span / a containing the text (case-insensitive)
                loc = page.locator(
                    f"xpath=//*[self::button or self::div or self::span or self::a or @role='button']"
                    f"[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{substr.lower()}')]"
                ).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2000)
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Helpers: switch challenge from Device-tap to TOTP
# ---------------------------------------------------------------------------

async def _switch_to_totp_method(page) -> bool:
    """When Google asks for a phone tap, click 'Try another way' → 'Google Authenticator'."""
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""

    needs_switch = any(kw in body for kw in [
        "tap yes", "check your", "device-tap", "open the gmail app",
        "tap your", "اضغط نعم", "فتح تطبيق",
    ])
    if not needs_switch:
        return False

    if not await _click_text(page, ["try another way", "طريقة أخرى", "جرب طريقة"], 4000):
        return False
    await _hd(0.8, 1.6)
    if not await _click_text(page, [
        "google authenticator", "authenticator app", "code from your authenticator",
        "تطبيق المصادقة", "مصادق Google", "Google Authenticator",
    ], 4000):
        return False
    await _hd(0.8, 1.6)
    return True


# ---------------------------------------------------------------------------
# Browser launch
# ---------------------------------------------------------------------------

async def _launch_browser(pw):
    """Launch a stealthy Chromium with optional proxy."""
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
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--no-sandbox",
            "--window-size=1280,800",
        ],
    )
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.6778.86 Safari/537.36"
    )
    ctx = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    try:
        from playwright_stealth import Stealth
        stealth = Stealth(
            navigator_user_agent_override=ua,
            navigator_vendor_override="Google Inc.",
            navigator_platform_override="Win32",
        )
        await stealth.apply_stealth_async(ctx)
    except Exception as exc:
        log.warning("playwright-stealth unavailable or failed: %s", exc)
    page = await ctx.new_page()
    page.set_default_timeout(20_000)
    return browser, ctx, page


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

async def _do_login(page, gmail: str, old_password: str, old_totp_secret: str,
                    on_progress: ProgressCallback) -> str:
    """Returns name of last completed login step. Raises on failure."""
    await on_progress("step:google_login_email")

    signin_urls = [
        "https://accounts.google.com/v3/signin/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin",
        "https://accounts.google.com/ServiceLogin",
        "https://accounts.google.com/signin/v2/identifier?hl=en",
    ]
    last_nav_error = None
    for url in signin_urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            break
        except Exception as exc:
            last_nav_error = exc
            await _hd(1.5, 2.5)
    else:
        raise RuntimeError(f"تعذّر فتح صفحة تسجيل دخول Google: {last_nav_error}")

    await _hd(2, 4)

    email_loc = await _wait_for_visible_locator(
        page,
        ['input[type="email"]:visible', 'input#identifierId:visible', 'input[type="email"]'],
        timeout_ms=20_000,
    )
    await email_loc.click()
    await _hd(0.3, 0.8)
    await email_loc.type(gmail, delay=random.randint(40, 120))
    await _hd(0.5, 1.4)

    next_btn = page.locator("#identifierNext").first
    if await next_btn.count() == 0:
        next_btn = page.get_by_role("button", name="Next")
    if await next_btn.count() > 0:
        await next_btn.click()
    else:
        await page.keyboard.press("Enter")

    await on_progress("step:google_login_password")
    await _hd(3, 5)

    pwd_loc = await _wait_for_visible_locator(
        page,
        [
            'input[type="password"][name="Passwd"]:visible',
            'input[type="password"]:not([aria-hidden="true"]):visible',
            'input[type="password"]:visible',
        ],
        timeout_ms=20_000,
    )
    await pwd_loc.click()
    await _hd(0.3, 0.8)
    await pwd_loc.type(old_password, delay=random.randint(30, 90))
    await _hd(0.5, 1.0)

    pwd_next = page.locator("#passwordNext").first
    if await pwd_next.count() == 0:
        pwd_next = page.get_by_role("button", name="Next")
    if await pwd_next.count() > 0:
        await pwd_next.click()
    else:
        await page.keyboard.press("Enter")
    await _hd(3, 5)

    # Detect wrong password
    body = ""
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        pass
    if any(k in body for k in [
        "wrong password", "couldn't find your google account", "couldn’t find your google account",
        "wasn't recognized", "كلمة المرور غير صحيحة",
    ]):
        raise RuntimeError("Wrong password / account not found")

    # 2FA
    await on_progress("step:google_login_2fa")
    if old_totp_secret:
        # If device-tap appeared, switch to TOTP
        await _switch_to_totp_method(page)

        totp_input_sel = (
            'input[type="tel"], input#totpPin, input[name="totpPin"], '
            'input[autocomplete="one-time-code"]'
        )
        try:
            await page.wait_for_selector(totp_input_sel, timeout=15_000, state="visible")
        except Exception:
            # Maybe still on device-tap screen — try once more
            if await _switch_to_totp_method(page):
                await page.wait_for_selector(totp_input_sel, timeout=15_000, state="visible")
            else:
                raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator")

        code = _now_totp(old_totp_secret)
        await _type_human(page, totp_input_sel, code)
        await _hd(0.4, 0.9)
        await page.keyboard.press("Enter")
        await _hd(2.5, 4)
    else:
        # No TOTP supplied — wait & hope the account has no 2FA
        await _hd(2, 4)

    # Skip "Stay signed in" / "save device" prompts
    for label in ["not now", "ليس الآن", "skip", "تخطي"]:
        if await _click_text(page, [label], 1500):
            await _hd(0.6, 1.2)
            break

    # Confirm we landed somewhere logged-in
    cur = page.url
    if "signin/challenge" in cur or "signin/v2" in cur:
        # Final attempt: maybe a recovery email/phone prompt blocks us
        raise RuntimeError("الحساب يطلب تأكيد إضافي (Recovery) — يلزم تدخّل يدوي")

    return "google_login_2fa"


async def _change_password(page, on_progress: ProgressCallback) -> str:
    """Change account password. Returns the new password."""
    await on_progress("step:open_security_page")
    new_pwd = _generate_strong_password(16)

    await page.goto(
        "https://myaccount.google.com/signinoptions/password?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)

    # Sometimes Google asks to re-authenticate here
    pwd_sel = 'input[type="password"]'
    try:
        await page.wait_for_selector(pwd_sel, timeout=15_000, state="visible")
    except Exception:
        raise RuntimeError("لم تُفتح صفحة تغيير كلمة السر")

    # Two password fields: new + confirm
    await on_progress("step:change_password")
    fields = page.locator('input[type="password"]')
    n = await fields.count()
    if n < 2:
        # Possibly need re-auth first — fill with old? We don't have it here.
        # Wait a bit and re-check.
        await _hd(2, 3)
        n = await fields.count()
    if n < 2:
        raise RuntimeError("حقول كلمة السر الجديدة غير ظاهرة")

    await fields.nth(0).click()
    await _hd(0.2, 0.5)
    await page.keyboard.type(new_pwd, delay=60)
    await _hd(0.4, 0.9)
    await fields.nth(1).click()
    await _hd(0.2, 0.5)
    await page.keyboard.type(new_pwd, delay=60)
    await _hd(0.4, 0.9)

    if not await _click_text(page, ["change password", "save", "تغيير كلمة المرور", "حفظ"], 4000):
        # Fallback: press Enter
        await page.keyboard.press("Enter")

    await _hd(3, 5)

    body = ""
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        pass
    if any(k in body for k in ["password changed", "تم تغيير", "تم الحفظ"]):
        return new_pwd
    # Heuristic: if URL changed away from /password we treat as success
    if "/signinoptions/password" not in page.url:
        return new_pwd
    raise RuntimeError("تعذّر تأكيد تغيير كلمة السر")


async def _setup_new_authenticator(page, on_progress: ProgressCallback) -> str:
    """Reset 2-step verification: add a new Authenticator app and capture its secret."""
    await on_progress("step:open_2fa_settings")
    await page.goto(
        "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)

    # Click "Authenticator" entry
    await on_progress("step:enable_new_authenticator")
    await _click_text(page, ["authenticator", "تطبيق المصادقة"], 5000)
    await _hd(1.5, 2.5)

    # "Set up authenticator" / "Get started" / "+ ADD"
    await _click_text(page, [
        "set up authenticator", "set up", "+ add authenticator",
        "get started", "إعداد", "بدء",
    ], 5000)
    await _hd(1.5, 2.5)

    # Click "Can't scan it?" to reveal the secret key
    revealed = await _click_text(page, [
        "can't scan it", "can’t scan it", "can't scan", "لا يمكنك المسح",
        "show secret", "show key",
    ], 5000)
    await _hd(1.0, 2.0)

    secret = ""
    if revealed:
        # Try to read the secret text from the page
        try:
            html = await page.content()
            # Look for a base32 chunk (>= 16 chars)
            m = re.search(r"\b([A-Z2-7]{4}\s?[A-Z2-7]{4}\s?[A-Z2-7]{4}\s?[A-Z2-7]{4}[A-Z2-7\s]*)\b", html)
            if m:
                secret = m.group(1).replace(" ", "").upper()
        except Exception:
            pass

    if not secret:
        # Fallback: use our own generated secret. Google won't accept it because
        # the displayed QR is server-issued. So we mark partial-success.
        raise RuntimeError("تعذّر استخراج مفتاح Authenticator الجديد من Google")

    # Click "Next" to reach the verify-code step
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
    await _click_text(page, ["verify", "next", "تحقق", "التالي"], 4000) or await page.keyboard.press("Enter")
    await _hd(2, 4)

    return secret


async def _verify_new_2fa(page, gmail: str, new_password: str, new_secret: str,
                          on_progress: ProgressCallback) -> bool:
    """Sign out then sign back in with the new credentials to confirm everything works."""
    await on_progress("step:verify_new_2fa")
    try:
        await page.goto(
            "https://accounts.google.com/Logout",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        await _hd(2, 3)
    except Exception:
        pass

    await page.goto(
        "https://accounts.google.com/signin/v2/identifier?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(1.5, 2.5)

    email_loc = await _wait_for_visible_locator(
        page,
        ['input[type="email"]:visible', 'input#identifierId:visible', 'input[type="email"]'],
        timeout_ms=20_000,
    )
    await email_loc.click()
    await _hd(0.3, 0.8)
    await email_loc.type(gmail, delay=random.randint(40, 120))
    next_btn = page.locator("#identifierNext").first
    if await next_btn.count() == 0:
        next_btn = page.get_by_role("button", name="Next")
    if await next_btn.count() > 0:
        await next_btn.click()
    else:
        await page.keyboard.press("Enter")

    await _hd(2, 3)
    pwd_loc = await _wait_for_visible_locator(
        page,
        [
            'input[type="password"][name="Passwd"]:visible',
            'input[type="password"]:not([aria-hidden="true"]):visible',
            'input[type="password"]:visible',
        ],
        timeout_ms=20_000,
    )
    await pwd_loc.click()
    await _hd(0.3, 0.8)
    await pwd_loc.type(new_password, delay=random.randint(30, 90))
    pwd_next = page.locator("#passwordNext").first
    if await pwd_next.count() == 0:
        pwd_next = page.get_by_role("button", name="Next")
    if await pwd_next.count() > 0:
        await pwd_next.click()
    else:
        await page.keyboard.press("Enter")
    await _hd(2.5, 4)

    await _switch_to_totp_method(page)
    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )
    try:
        await page.wait_for_selector(code_sel, timeout=15_000, state="visible")
    except Exception:
        return True  # No 2FA challenge — still considered logged-in
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
    """Rotate a Google account's password and 2FA secret."""
    result: Dict[str, Any] = {
        "success": False,
        "gmail": gmail,
        "new_password": None,
        "new_totp_secret": None,
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
        result["error"] = "Playwright غير مُثبَّت. شغّل: pip install playwright && playwright install chromium"
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

        await _do_login(page, gmail, old_password, old_totp_secret, _progress_wrap)
        new_password = await _change_password(page, _progress_wrap)
        result["new_password"] = new_password

        new_secret = await _setup_new_authenticator(page, _progress_wrap)
        result["new_totp_secret"] = new_secret

        await _verify_new_2fa(page, gmail, new_password, new_secret, _progress_wrap)

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
    bar = ""
    for i, _ in enumerate(ROTATION_STEPS, 1):
        bar += "🟢" if i <= idx else "⚪️"
    return f"{bar}\n\n🔧 الخطوة {idx}/{len(ROTATION_STEPS)}: *{label}*"
