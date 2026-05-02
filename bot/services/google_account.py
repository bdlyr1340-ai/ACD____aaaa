"""Google Account Rotator — Camoufox edition.

Public entry point:
    rotate_google_account(on_progress, gmail, old_password, old_totp_secret, user_id)

This rewrite restores the techniques from the original (working) version:
- Camoufox first, Patchright second, Playwright Chromium last.
- Heavy stealth JS + realistic UA rotation.
- "Warmup" visit to google.com before sign-in (builds cookies, looks human).
- Random mouse movements + per-character typing delay.
- Robust password-field detection that ignores Google's hidden trap fields.
- Detects "Couldn't sign you in" and retries with a fresh page.
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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pyotp

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
ScreenshotCallback = Callable[[str, str], Awaitable[None]]  # (step_name, file_path) -> None
SmsCodeProvider = Callable[[], Awaitable[Optional[str]]]
CredentialsCallback = Callable[[str, str], Awaitable[None]]  # (label, value) -> None

# Default fixed values (override via env vars)
DEFAULT_NEW_PASSWORD = "VJ77X2305xx30j5"
DEFAULT_FALLBACK_PHONE = "+9647728257333"


async def _shoot(page, user_id: int, tag: str,
                 on_screenshot: Optional[ScreenshotCallback] = None) -> None:
    """Capture a screenshot and notify the caller via callback (if provided).

    Used to send progress screenshots to the Telegram user after every step,
    so they can debug what the bot is seeing. Failures are non-fatal.
    """
    if page is None:
        return
    try:
        ts = int(time.time() * 1000)
        path = os.path.join(SHOTS_DIR, f"{user_id}_{ts}_{tag}.png")
        await page.screenshot(path=path, full_page=True, timeout=10_000)
        if on_screenshot is not None:
            try:
                await on_screenshot(tag, path)
            except Exception as exc:
                log.warning("on_screenshot callback failed: %s", exc)
    except Exception as exc:
        log.warning("Screenshot at %s failed: %s", tag, exc)

# ---------------------------------------------------------------------------
# Steps & labels (unchanged public contract)
# ---------------------------------------------------------------------------

ROTATION_STEPS: List[str] = [
    "launch_browser",
    "google_login_email",
    "google_login_password",
    "google_login_2fa",
    "open_security_page",
    "change_password",
    "open_2fa_settings",
    "add_phone_number",
    "enable_new_authenticator",
    "verify_new_2fa",
    "done",
]

STEP_LABELS_AR: Dict[str, str] = {
    "launch_browser":           "تشغيل المتصفح",
    "google_login_email":       "إدخال البريد الإلكتروني",
    "google_login_password":    "إدخال كلمة السر",
    "google_login_2fa":         "المصادقة الثنائية",
    "open_security_page":       "فتح إعدادات الأمان",
    "change_password":          "تغيير كلمة السر",
    "open_2fa_settings":        "فتح إعدادات 2FA",
    "add_phone_number":         "إضافة رقم الهاتف",
    "enable_new_authenticator": "إضافة Authenticator جديد",
    "verify_new_2fa":           "تأكيد المصادقة الجديدة",
    "done":                     "مكتمل",
}

SHOTS_DIR = os.environ.get("SHOTS_DIR", "/tmp/shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

# Speed factor (lower = faster). Override via env var.
_SPEED = float(os.getenv("SHEERID_SPEED_FACTOR", "0.35"))

# Stealth UA pool (rotated per run)
_STEALTH_UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/135.0.7049.115 Safari/537.36"),
]

_EXTRA_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(p);
}
"""


# ---------------------------------------------------------------------------
# Helpers — passwords / TOTP
# ---------------------------------------------------------------------------

def _generate_strong_password(length: int = 16) -> str:
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


def _now_totp(secret: str) -> str:
    return pyotp.TOTP(secret.replace(" ", "").upper()).now()


# ---------------------------------------------------------------------------
# Helpers — screenshots / delays / human-ness
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


async def _hd(min_s: float = 0.4, max_s: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(min_s * _SPEED, max_s * _SPEED))


async def _human_mouse_move(page) -> None:
    try:
        for _ in range(random.randint(2, 5)):
            x = random.randint(100, 900)
            y = random.randint(100, 600)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.05, 0.2))
    except Exception:
        pass


async def _type_human_at(page, selector: str, text: str) -> None:
    """Click a selector then type each char with a small random delay."""
    await page.click(selector)
    await _hd(0.2, 0.5)
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))


async def _wait_for_visible_locator(page, selectors: List[str], timeout_ms: int = 20_000):
    """Return the first truly-visible locator, ignoring Google's hidden traps
    (hiddenPassword / aria-hidden=true / tabindex=-1)."""
    deadline = time.time() + (timeout_ms / 1000.0)
    last_error = None
    while time.time() < deadline:
        for selector in selectors:
            try:
                loc_all = page.locator(selector)
                n = await loc_all.count()
                for i in range(n):
                    cand = loc_all.nth(i)
                    try:
                        name = (await cand.get_attribute("name")) or ""
                        aria = (await cand.get_attribute("aria-hidden")) or ""
                        tab = (await cand.get_attribute("tabindex")) or ""
                        if name == "hiddenPassword" or aria == "true" or tab == "-1":
                            continue
                    except Exception:
                        pass
                    try:
                        if await cand.is_visible():
                            return cand
                    except Exception as exc:
                        last_error = exc
            except Exception as exc:
                last_error = exc
        await asyncio.sleep(0.25)
    joined = ", ".join(selectors)
    if last_error:
        raise RuntimeError(f"Timed out waiting for visible element: {joined} ({last_error})")
    raise RuntimeError(f"Timed out waiting for visible element: {joined}")


async def _click_text(page, substrings: List[str], timeout_ms: int = 4000) -> bool:
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        for substr in substrings:
            try:
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


async def _handle_passkey_screen(page) -> bool:
    """If Google shows 'Use your passkey', click it then fall through to TOTP.

    The user wants the bot to press 'Use your passkey' first, then switch
    to the Authenticator code (TOTP) flow. After clicking the passkey button,
    Google usually shows a system prompt that we cannot satisfy headlessly,
    so we click 'Try another way' to reach the Authenticator option.
    """
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""

    has_passkey = any(kw in body for kw in [
        "use your passkey", "passkey", "مفتاح المرور", "passkeys",
    ])
    if not has_passkey:
        return False

    log.info("Passkey screen detected — clicking 'Use your passkey'")
    clicked = await _click_text(page, [
        "use your passkey", "use passkey", "استخدم مفتاح المرور", "مفتاح المرور",
    ], 4000)
    if not clicked:
        # Try button selectors directly
        for sel in [
            'button:has-text("Use your passkey")',
            'button:has-text("passkey")',
            '[role="button"]:has-text("passkey")',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue

    if clicked:
        await _hd(2, 3.5)
        # After passkey prompt fails (we're headless, no security key), pick TOTP
        await _click_text(page, [
            "try another way", "طريقة أخرى", "جرب طريقة",
            "use a different method", "use another method",
        ], 5000)
        await _hd(1.0, 2.0)
        await _click_text(page, [
            "google authenticator", "authenticator app", "code from your authenticator",
            "تطبيق المصادقة", "مصادق Google", "Google Authenticator", "verification code",
        ], 4000)
        await _hd(1.0, 2.0)
    return clicked


async def _switch_to_totp_method(page) -> bool:
    """Handle device-tap, passkey, and other 2FA prompts to land on TOTP input."""
    # Passkey screen first
    if await _handle_passkey_screen(page):
        return True

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
# Warmup — visit google.com first to look human and gather cookies
# ---------------------------------------------------------------------------

async def _warmup_google(page) -> None:
    try:
        log.info("Warmup: visiting google.com")
        await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=20_000)
        await _hd(1.5, 3.0)
        await _human_mouse_move(page)
        consent = page.locator(
            "button:has-text('Accept all'), "
            "button:has-text('Accept'), "
            "button:has-text('I agree'), "
            "button:has-text('Reject all')"
        )
        if await consent.count() > 0:
            try:
                await consent.first.click(timeout=3000)
                await _hd(1.0, 2.0)
            except Exception:
                pass
        search = page.locator('textarea[name="q"], input[name="q"]')
        if await search.count() > 0:
            queries = ["weather today", "latest news", "best restaurants near me", "time now"]
            try:
                await search.first.click()
                await _hd(0.3, 0.8)
                await search.first.type(random.choice(queries), delay=random.randint(50, 120))
                await _hd(0.5, 1.5)
                await page.keyboard.press("Escape")
                await _hd(0.5, 1.0)
            except Exception:
                pass
        log.info("Warmup complete")
    except Exception as exc:
        log.warning("Warmup failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Browser launch — Camoufox first, then Patchright, then Playwright
# ---------------------------------------------------------------------------

def _build_proxy_cfg() -> Optional[Dict[str, str]]:
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if not proxy_url:
        return None
    u = urlparse(proxy_url)
    cfg: Dict[str, str] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
    if u.password:
        cfg["password"] = u.password
    return cfg


async def _launch_camoufox() -> Optional[Tuple[Any, Any, Any, Any]]:
    """Returns (cleanup_callable, browser, context, page) or None on failure."""
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        log.info("Camoufox not installed — skipping")
        return None

    proxy_cfg = _build_proxy_cfg()
    kwargs: Dict[str, Any] = {"headless": True, "humanize": True, "i_know_what_im_doing": True}
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    try:
        log.info("Launching Camoufox (proxy=%s)", "yes" if proxy_cfg else "no")
        cm = AsyncCamoufox(**kwargs)
        browser = await cm.__aenter__()
        ctx = await browser.new_context()
        await ctx.add_init_script(_EXTRA_STEALTH_JS)
        page = await ctx.new_page()
        page.set_default_timeout(25_000)

        async def _cleanup():
            try:
                await page.close()
            except Exception:
                pass
            try:
                await ctx.close()
            except Exception:
                pass
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

        return _cleanup, browser, ctx, page
    except Exception as exc:
        log.warning("Camoufox launch failed: %s", exc)
        return None


async def _launch_patchright() -> Optional[Tuple[Any, Any, Any, Any]]:
    try:
        from patchright.async_api import async_playwright as patchright_pw
    except ImportError:
        log.info("Patchright not installed — skipping")
        return None
    proxy_cfg = _build_proxy_cfg()
    try:
        pw_inst = await patchright_pw().start()
        kwargs: Dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars", "--disable-dev-shm-usage",
                "--disable-extensions", "--no-sandbox",
                "--window-size=1280,800",
            ],
        }
        if proxy_cfg:
            kwargs["proxy"] = proxy_cfg
        ua = random.choice(_STEALTH_UAS)
        browser = await pw_inst.chromium.launch(**kwargs)
        ctx = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        await ctx.add_init_script(_EXTRA_STEALTH_JS)
        page = await ctx.new_page()
        page.set_default_timeout(25_000)

        async def _cleanup():
            try:
                await page.close()
            except Exception:
                pass
            try:
                await ctx.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw_inst.stop()
            except Exception:
                pass

        return _cleanup, browser, ctx, page
    except Exception as exc:
        log.warning("Patchright launch failed: %s", exc)
        return None


async def _launch_playwright(pw) -> Tuple[Any, Any, Any, Any]:
    proxy_cfg = _build_proxy_cfg()
    kwargs: Dict[str, Any] = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars", "--disable-dev-shm-usage",
            "--disable-extensions", "--no-sandbox",
            "--window-size=1280,800",
        ],
    }
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    ua = random.choice(_STEALTH_UAS)
    is_mac = "Macintosh" in ua
    browser = await pw.chromium.launch(**kwargs)
    ctx = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    await ctx.add_init_script(_EXTRA_STEALTH_JS)
    try:
        from playwright_stealth import Stealth
        stealth = Stealth(
            navigator_user_agent_override=ua,
            navigator_vendor_override="Google Inc.",
            navigator_platform_override="MacIntel" if is_mac else "Win32",
            navigator_languages_override=["en-US", "en"],
        )
        await stealth.apply_stealth_async(ctx)
    except Exception as exc:
        log.warning("playwright-stealth unavailable: %s", exc)
    page = await ctx.new_page()
    page.set_default_timeout(25_000)

    async def _cleanup():
        try:
            await page.close()
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    return _cleanup, browser, ctx, page


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

_SIGNIN_URLS = [
    "https://accounts.google.com/v3/signin/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin",
    "https://accounts.google.com/ServiceLogin",
    "https://accounts.google.com/signin/v2/identifier?hl=en",
]


async def _enter_email(page, gmail: str) -> bool:
    """Navigate to a sign-in URL and submit the email. Returns True on success."""
    last_err = None
    for url in _SIGNIN_URLS:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            break
        except Exception as exc:
            last_err = exc
            await _hd(1.5, 2.5)
    else:
        raise RuntimeError(f"تعذّر فتح صفحة تسجيل دخول Google: {last_err}")

    await _hd(2, 4)
    await _human_mouse_move(page)

    try:
        title = (await page.title()).lower()
        if "couldn" in title and "sign" in title:
            log.warning("Hit 'Couldn't sign you in' on initial load")
            return False
    except Exception:
        pass

    try:
        email_loc = await _wait_for_visible_locator(
            page,
            ['input[type="email"]', 'input#identifierId'],
            timeout_ms=20_000,
        )
    except Exception as exc:
        log.warning("Email field not visible: %s", exc)
        return False

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
    return True


async def _do_login(page_holder: Dict[str, Any], gmail: str, old_password: str,
                    old_totp_secret: str, on_progress: ProgressCallback) -> str:
    """page_holder is a dict with 'page' and 'ctx' so we can swap the page on retry."""
    await on_progress("step:google_login_email")
    page = page_holder["page"]
    ctx = page_holder["ctx"]

    # Warmup first — this is what made the old version work
    await _warmup_google(page)

    if not await _enter_email(page, gmail):
        # Retry with a fresh page
        log.info("Retrying email entry on a fresh page")
        try:
            await page.close()
        except Exception:
            pass
        page = await ctx.new_page()
        page.set_default_timeout(25_000)
        page_holder["page"] = page
        await _hd(2, 4)
        if not await _enter_email(page, gmail):
            raise RuntimeError("Google يرفض الاتصال (Couldn't sign you in) — جرّب بروكسي مختلف")

    await on_progress("step:google_login_password")
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await _hd(2.0, 3.5)
    await _human_mouse_move(page)

    pwd_loc = await _wait_for_visible_locator(
        page,
        [
            'input[type="password"][name="Passwd"]',
            'input[type="password"]:not([name="hiddenPassword"]):not([aria-hidden="true"]):not([tabindex="-1"])',
            'input[type="password"]',
        ],
        timeout_ms=30_000,
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

    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    if any(k in body for k in [
        "wrong password", "couldn't find your google account",
        "couldn’t find your google account", "wasn't recognized",
        "كلمة المرور غير صحيحة",
    ]):
        raise RuntimeError("كلمة سر خاطئة أو الحساب غير موجود")

    await on_progress("step:google_login_2fa")
    if old_totp_secret:
        await _switch_to_totp_method(page)
        totp_sel = (
            'input[type="tel"], input#totpPin, input[name="totpPin"], '
            'input[autocomplete="one-time-code"]'
        )
        try:
            await page.wait_for_selector(totp_sel, timeout=15_000, state="visible")
        except Exception:
            if await _switch_to_totp_method(page):
                await page.wait_for_selector(totp_sel, timeout=15_000, state="visible")
            else:
                raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator")

        code = _now_totp(old_totp_secret)
        await _type_human_at(page, totp_sel, code)
        await _hd(0.4, 0.9)
        await page.keyboard.press("Enter")
        await _hd(2.5, 4)
    else:
        await _hd(2, 4)

    for label in ["not now", "ليس الآن", "skip", "تخطي"]:
        if await _click_text(page, [label], 1500):
            await _hd(0.6, 1.2)
            break

    cur = page.url
    if "signin/challenge" in cur or "signin/v2" in cur:
        raise RuntimeError("الحساب يطلب تأكيد إضافي (Recovery) — يلزم تدخّل يدوي")

    return "google_login_2fa"


# ---------------------------------------------------------------------------
# Change password / setup new authenticator / verify
# ---------------------------------------------------------------------------

async def _confirm_change_password_dialog(page) -> bool:
    """Click the 'Change password' button inside Google's confirmation dialog.

    After clicking the first 'Change password' button, Google shows a modal
    dialog ("You'll stay signed in on these devices after changing your
    password") with two buttons: 'Cancel' and 'Change password'. We need
    to click the second one to actually commit the change.
    """
    # Wait briefly for the dialog to appear
    dialog_indicators = [
        '[role="dialog"]',
        '[role="alertdialog"]',
        'div[aria-modal="true"]',
    ]
    dialog_found = False
    for sel in dialog_indicators:
        try:
            await page.wait_for_selector(sel, timeout=3000, state="visible")
            dialog_found = True
            log.info("Confirmation dialog detected: %s", sel)
            break
        except Exception:
            continue

    if not dialog_found:
        # Maybe Google submitted directly without a dialog — that's fine
        log.info("No confirmation dialog appeared (may have submitted directly)")
        return False

    await _hd(0.5, 1.2)

    # Try to click "Change password" inside the dialog specifically
    dialog_button_selectors = [
        '[role="dialog"] button:has-text("Change password")',
        '[role="alertdialog"] button:has-text("Change password")',
        'div[aria-modal="true"] button:has-text("Change password")',
        '[role="dialog"] button:has-text("تغيير كلمة المرور")',
        '[role="dialog"] [role="button"]:has-text("Change password")',
        '[role="dialog"] button:has-text("Change Password")',
    ]
    for sel in dialog_button_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await _hd(0.3, 0.7)
                await loc.click(timeout=4000)
                log.info("Clicked dialog 'Change password' via: %s", sel)
                return True
        except Exception as exc:
            log.debug("Dialog selector %s failed: %s", sel, exc)
            continue

    # Fallback: scope to dialog then text-search inside it
    try:
        dlg = page.locator('[role="dialog"], [role="alertdialog"], div[aria-modal="true"]').first
        # Look for any button inside the dialog whose text matches
        btns = dlg.locator('button, [role="button"]')
        count = await btns.count()
        for i in range(count):
            btn = btns.nth(i)
            try:
                if not await btn.is_visible():
                    continue
                txt = (await btn.inner_text()).strip().lower()
                if any(kw in txt for kw in [
                    "change password", "change", "تغيير", "ok", "confirm", "تأكيد",
                ]):
                    # Skip cancel buttons
                    if "cancel" in txt or "إلغاء" in txt:
                        continue
                    await btn.click(timeout=4000)
                    log.info("Clicked dialog button via text scan: %r", txt)
                    return True
            except Exception:
                continue
    except Exception as exc:
        log.warning("Dialog text-scan failed: %s", exc)

    log.warning("Could not find 'Change password' button inside dialog")
    return False


async def _click_change_password_button(page) -> bool:
    """Try multiple strategies to click the 'Change password' button reliably.

    Google sometimes renders this button as <button>, sometimes as a div with
    role=button, and the visible text varies (Change password / تغيير / Save).
    We try locators in order and return on first success.
    """
    # Strategy 1: button with exact visible text (English)
    selectors = [
        'button:has-text("Change password")',
        'button:has-text("Change Password")',
        'button:has-text("تغيير كلمة المرور")',
        'button:has-text("Save")',
        '[role="button"]:has-text("Change password")',
        '[role="button"]:has-text("تغيير كلمة المرور")',
        # Google's Material button class fallback
        'button[jsname]:has-text("Change")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                # Scroll into view first (the button can be below fold)
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await _hd(0.3, 0.7)
                await loc.click(timeout=4000)
                log.info("Clicked change-password button via: %s", sel)
                return True
        except Exception as exc:
            log.debug("Selector %s failed: %s", sel, exc)
            continue

    # Strategy 2: text-based xpath (case-insensitive)
    if await _click_text(page, [
        "change password", "تغيير كلمة المرور", "تغيير كلمة السر", "save", "حفظ",
    ], 5000):
        log.info("Clicked change-password button via text search")
        return True

    return False


async def _change_password(page, on_progress: ProgressCallback) -> str:
    await on_progress("step:open_security_page")
    # Use fixed password from env var or hard-coded default
    new_pwd = os.environ.get("NEW_PASSWORD", DEFAULT_NEW_PASSWORD).strip()
    if not new_pwd or len(new_pwd) < 8:
        log.warning("NEW_PASSWORD too short, falling back to default")
        new_pwd = DEFAULT_NEW_PASSWORD
    log.info("Using fixed password (len=%d)", len(new_pwd))

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

    # Fill new password (field #1)
    await fields.nth(0).click()
    await _hd(0.2, 0.5)
    await page.keyboard.type(new_pwd, delay=random.randint(40, 90))
    await _hd(0.4, 0.9)
    # Confirm new password (field #2)
    await fields.nth(1).click()
    await _hd(0.2, 0.5)
    await page.keyboard.type(new_pwd, delay=random.randint(40, 90))
    await _hd(0.6, 1.2)

    # Tab out so Google validates the strength meter
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass
    await _hd(0.8, 1.6)

    # Click "Change password" — try several strategies
    clicked = await _click_change_password_button(page)
    if not clicked:
        # Last-resort: focus confirm field and press Enter
        try:
            await fields.nth(1).click()
            await _hd(0.2, 0.4)
            await page.keyboard.press("Enter")
            log.info("Submitted via Enter key (fallback)")
        except Exception as exc:
            raise RuntimeError(f"تعذّر الضغط على زر Change password: {exc}")

    # Google now shows a confirmation DIALOG ("You'll stay signed in on these
    # devices...") that has its own "Change password" button. We must click it
    # too, otherwise the password won't actually be changed.
    await _hd(1.5, 2.5)
    await _confirm_change_password_dialog(page)

    # Wait for the page to react (URL change / success banner)
    await _hd(3, 5)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    if any(k in body for k in [
        "password changed", "password was changed", "تم تغيير", "تم الحفظ",
    ]):
        log.info("Password change confirmed via banner")
        return new_pwd
    if "/signinoptions/password" not in page.url:
        log.info("Password change confirmed via URL change: %s", page.url)
        return new_pwd

    # One more wait + recheck — Google sometimes shows banner late
    await _hd(2, 3)
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    if any(k in body for k in ["password changed", "password was changed", "تم تغيير"]):
        return new_pwd
    if "/signinoptions/password" not in page.url:
        return new_pwd

    raise RuntimeError("تعذّر تأكيد تغيير كلمة السر")


async def _click_turn_on_2sv(page) -> bool:
    """Click 'Turn on 2-Step Verification' button if visible.

    This appears when 2SV is currently OFF. After clicking, Google may
    re-prompt for the password before continuing. We handle that too.
    """
    selectors = [
        'button:has-text("Turn on 2-Step Verification")',
        'button:has-text("Turn on")',
        'button:has-text("تفعيل التحقّق")',
        'button:has-text("تفعيل المصادقة")',
        '[role="button"]:has-text("Turn on 2-Step")',
        '[role="button"]:has-text("تفعيل")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await _hd(0.3, 0.7)
                await loc.click(timeout=4000)
                log.info("Clicked 'Turn on 2-Step Verification' via: %s", sel)
                return True
        except Exception:
            continue

    if await _click_text(page, [
        "turn on 2-step verification", "turn on 2-step", "turn on 2sv",
        "تفعيل التحقّق بخطوتين", "تفعيل المصادقة الثنائية", "تفعيل التحقق",
    ], 5000):
        return True
    return False


async def _reauth_if_needed(page, current_password: str) -> None:
    """If Google re-prompts for the password (common after clicking sensitive
    settings buttons), fill it automatically using the password we just set."""
    pwd_sel = (
        'input[type="password"][name="Passwd"], '
        'input[type="password"]:not([name="hiddenPassword"])'
        ':not([aria-hidden="true"]):not([tabindex="-1"])'
    )
    try:
        await page.wait_for_selector(pwd_sel, timeout=5_000, state="visible")
    except Exception:
        return  # No re-auth needed
    log.info("Re-auth prompt detected — entering current password")
    try:
        loc = page.locator(pwd_sel).first
        await loc.click()
        await _hd(0.3, 0.7)
        await loc.type(current_password, delay=random.randint(40, 90))
        await _hd(0.4, 0.8)
        nxt = page.locator("#passwordNext").first
        if await nxt.count() == 0:
            nxt = page.get_by_role("button", name="Next")
        if await nxt.count() > 0:
            await nxt.click()
        else:
            await page.keyboard.press("Enter")
        await _hd(2.5, 4)
    except Exception as exc:
        log.warning("Re-auth attempt failed: %s", exc)


async def _add_phone_number(
    page,
    phone: str,
    sms_code_provider: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    *,
    on_screenshot: Optional[ScreenshotCallback] = None,
    user_id: int = 0,
) -> bool:
    """Add a phone number via direct URL navigation.

    Instead of clicking through the multi-step UI ("Add a phone number" →
    "Add phone number" → "Enter phone number"), we navigate directly to
    https://myaccount.google.com/phone which opens the phone management
    page and shows the input field directly. This is much more reliable
    because it bypasses ALL the intermediate buttons that change between
    Google account types and UI versions.

    Sequence:
      1. goto https://myaccount.google.com/phone
      2. Click "Add recovery phone" / "Add a phone number" if shown
      3. Select country (Iraq) from dropdown
      4. Type the number and click Next
      5. Handle SMS code if requested
    """
    log.info("Adding phone via direct URL: %s", phone)

    async def _snap(tag: str) -> None:
        await _shoot(page, user_id, f"phone_{tag}", on_screenshot)

    # ── Step 1: navigate to phone management page directly ──
    phone_urls = [
        "https://myaccount.google.com/phone?hl=en",
        "https://myaccount.google.com/personal-info/phone?hl=en",
    ]
    loaded = False
    for url in phone_urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await _hd(2, 3.5)
            loaded = True
            log.info("Loaded phone page: %s", url)
            break
        except Exception as exc:
            log.warning("Failed to load %s: %s", url, exc)
    if not loaded:
        await _snap("nav_failed")
        return False
    await _snap("1_page_loaded")

    # ── Step 2: click "Add now" / "Add recovery phone" / "Add phone" if shown ──
    # On the /phone page, the button is usually "Add now". On other variants
    # it might be "Add recovery phone" or "Add a phone number".
    log.info("Looking for Add-phone CTA button")
    cta_clicked = False
    for sel in [
        'button:has-text("Add now")',
        '[role="button"]:has-text("Add now")',
        'button:has-text("Add recovery phone")',
        'button:has-text("Add a phone number")',
        'button:has-text("Add phone number")',
        'button:has-text("Add phone")',
        '[role="button"]:has-text("Add recovery phone")',
        '[role="button"]:has-text("Add phone number")',
        '[role="button"]:has-text("Add a phone number")',
        'a:has-text("Add now")',
        'a:has-text("Add recovery phone")',
        'a:has-text("Add a phone number")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await _hd(0.3, 0.7)
                await loc.click(timeout=4000)
                cta_clicked = True
                log.info("Clicked CTA via: %s", sel)
                await _hd(2.5, 4)
                break
        except Exception:
            continue
    if not cta_clicked:
        # Text-search fallback
        if await _click_text(page, [
            "add now", "add recovery phone", "add a phone number",
            "add phone number", "إضافة الآن", "أضف الآن",
        ], 4000):
            cta_clicked = True
            log.info("Clicked CTA via text search")
            await _hd(2.5, 4)
    await _snap("2_after_cta")

    # ── Step 3: wait for the phone input field (across frames + with retries) ──
    # After clicking "Add now", Google may:
    #   (a) navigate to a new page,
    #   (b) load the input inline in the same page,
    #   (c) load it inside an iframe,
    #   (d) require a re-auth password challenge first.
    # We handle all four cases with patience and frame-traversal.
    phone_sel = (
        'input[type="tel"], input[name="phoneNumber"], '
        'input[aria-label*="phone" i], input[autocomplete="tel"]'
    )

    async def _find_phone_input_anywhere():
        """Search the main page AND every iframe for the phone input."""
        # Main page first
        try:
            loc = page.locator(phone_sel).first
            if await loc.count() and await loc.is_visible():
                return loc, page
        except Exception:
            pass
        # All frames
        try:
            for fr in page.frames:
                try:
                    fl = fr.locator(phone_sel).first
                    if await fl.count() and await fl.is_visible():
                        log.info("Found phone input inside frame: %s", fr.url)
                        return fl, fr
                except Exception:
                    continue
        except Exception:
            pass
        return None, None

    async def _maybe_handle_reauth():
        """If Google shows a password re-auth prompt, fill it (best-effort)."""
        try:
            pwd_loc = page.locator(
                'input[type="password"]:not([name="hiddenPassword"])'
                ':not([aria-hidden="true"]):not([tabindex="-1"])'
            ).first
            if await pwd_loc.count() and await pwd_loc.is_visible():
                log.info("Re-auth prompt detected on phone page")
                # We don't have the password here, so just press Enter to skip
                # (the caller's _reauth_if_needed should have handled it before)
                return True
        except Exception:
            pass
        return False

    log.info("Waiting for phone input field (with frame search + retries)")
    found_loc = None
    found_frame = None
    deadline = time.time() + 25.0
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        # Check for re-auth challenge
        if await _maybe_handle_reauth():
            log.warning("Phone page requires re-auth — bailing out")
            await _snap("3_reauth_required")
            return False
        # Search main + frames
        loc, frame = await _find_phone_input_anywhere()
        if loc is not None:
            found_loc = loc
            found_frame = frame
            log.info("Phone input found on attempt %d", attempts)
            break
        # Wait a bit and retry
        await asyncio.sleep(1.0)
        # On 4th attempt, try alternative URL
        if attempts == 4:
            log.info("Trying alternative URL: /signinoptions/rescuephone")
            try:
                await page.goto(
                    "https://myaccount.google.com/signinoptions/rescuephone?hl=en",
                    wait_until="domcontentloaded", timeout=15_000,
                )
                await _hd(2, 3)
            except Exception as exc:
                log.warning("Alternative URL failed: %s", exc)
        # On 7th attempt, try clicking any button containing "phone" again
        if attempts == 7:
            log.info("Last resort: clicking any phone-related button")
            try:
                if await _click_text(page, [
                    "add now", "add a phone", "add phone",
                    "edit", "تعديل", "إضافة",
                ], 3000):
                    await _hd(2, 3)
            except Exception:
                pass

    if found_loc is None:
        log.warning("Phone input field not visible after %d attempts (25s)", attempts)
        await _snap("3_FAILED_no_input")
        try:
            log.info("Current URL: %s", page.url)
            log.info("Page title: %s", await page.title())
        except Exception:
            pass
        return False

    await _snap("3_input_visible")

    # ── Step 4: select country (Iraq) from dropdown ──
    country_iso = os.environ.get("PHONE_COUNTRY_ISO", "iq").lower().strip()
    country_name = os.environ.get("PHONE_COUNTRY_NAME", "Iraq").strip()
    country_dial = os.environ.get("PHONE_COUNTRY_DIAL", "+964").strip()

    selected = False
    try:
        dropdown_selectors = [
            'div[aria-label*="country" i][role="combobox"]',
            'div[role="combobox"][aria-haspopup="listbox"]',
            'button[aria-label*="country" i]',
            'div[aria-label*="Country" i]',
            '[role="combobox"]',
        ]
        for dsel in dropdown_selectors:
            try:
                dd = page.locator(dsel).first
                if await dd.count() and await dd.is_visible():
                    await dd.click(timeout=3000)
                    await _hd(0.6, 1.2)
                    log.info("Opened country dropdown via: %s", dsel)
                    try:
                        await page.keyboard.type(country_name, delay=80)
                        await _hd(0.5, 1.0)
                    except Exception:
                        pass
                    iraq_option_selectors = [
                        f'[role="option"]:has-text("{country_name}")',
                        f'li:has-text("{country_name}")',
                        f'div[role="option"]:has-text("{country_dial}")',
                        f'[role="option"]:has-text("{country_dial}")',
                    ]
                    for opt_sel in iraq_option_selectors:
                        try:
                            opt = page.locator(opt_sel).first
                            if await opt.count() and await opt.is_visible():
                                await opt.click(timeout=2500)
                                selected = True
                                log.info("Selected country '%s'", country_name)
                                break
                        except Exception:
                            continue
                    if not selected:
                        try:
                            await page.keyboard.press("Enter")
                            selected = True
                        except Exception:
                            pass
                    break
            except Exception:
                continue
        if not selected:
            log.info("Country dropdown not found — using international format")
    except Exception as exc:
        log.warning("Country selection failed (non-fatal): %s", exc)
    await _snap("4_after_country")

    await _hd(0.8, 1.5)

    # ── Step 5: type the phone number ──
    try:
        await page.wait_for_selector(phone_sel, timeout=5_000, state="visible")
    except Exception:
        pass
    phone_loc = page.locator(phone_sel).first
    await phone_loc.click()
    await _hd(0.3, 0.7)

    raw = re.sub(r"\D", "", phone.strip())
    if selected:
        dial_digits = re.sub(r"\D", "", country_dial)
        if dial_digits and raw.startswith(dial_digits):
            local = raw[len(dial_digits):]
        else:
            local = raw
        cleaned = local.lstrip("0") if local.startswith("0") else local
        log.info("Country selected — typing local: %s", cleaned)
    else:
        cleaned = ("+" + raw) if not phone.strip().startswith("+") else "+" + raw
        log.info("No country — typing international: %s", cleaned)

    try:
        await phone_loc.fill("")
        await _hd(0.2, 0.4)
    except Exception:
        pass
    await phone_loc.type(cleaned, delay=random.randint(40, 90))
    await _hd(0.6, 1.2)
    await _snap("5_after_typing")

    # ── Step 6: submit ──
    if not await _click_text(page, ["next", "send", "save", "التالي", "إرسال", "حفظ"], 4000):
        await page.keyboard.press("Enter")
    log.info("Submitted phone number")
    await _hd(3, 5)
    await _snap("6_after_submit")

    # ── Step 7: handle SMS code if requested ──
    code_input_sel = (
        'input[type="tel"][maxlength="6"], '
        'input[autocomplete="one-time-code"], '
        'input[name="code"], input#code, input[name="Pin"], '
        'input[aria-label*="code" i]'
    )
    code_field_visible = False
    try:
        await page.wait_for_selector(code_input_sel, timeout=8_000, state="visible")
        code_field_visible = True
    except Exception:
        pass

    if not code_field_visible:
        log.info("Phone added without SMS verification")
        return True

    log.info("Google is asking for an SMS verification code")
    await _snap("7_sms_requested")

    if sms_code_provider is None:
        log.warning("No SMS provider configured — aborting")
        return False

    try:
        code = await sms_code_provider()
    except Exception as exc:
        log.warning("SMS code provider raised: %s", exc)
        return False
    if not code or not str(code).strip():
        return False

    code = re.sub(r"\D", "", str(code).strip())
    if not code:
        return False

    log.info("Entering SMS code")
    code_loc = page.locator(code_input_sel).first
    await code_loc.click()
    await _hd(0.3, 0.7)
    await code_loc.type(code, delay=random.randint(40, 90))
    await _hd(0.4, 0.9)
    if not await _click_text(page, ["verify", "next", "تحقق", "التالي", "submit"], 4000):
        await page.keyboard.press("Enter")
    await _hd(2.5, 4)
    await _snap("7_after_code")
    return True


async def _setup_new_authenticator(
    page,
    on_progress: ProgressCallback,
    current_password: str,
    sms_code_provider: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    *,
    on_screenshot: Optional[ScreenshotCallback] = None,
    user_id: int = 0,
) -> str:
    async def _snap(tag: str) -> None:
        await _shoot(page, user_id, tag, on_screenshot)

    await on_progress("step:open_2fa_settings")
    await page.goto(
        "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)
    await _snap("2sv_page_loaded")

    # Re-auth may be required to open the 2SV settings page itself
    await _reauth_if_needed(page, current_password)
    await _hd(1.5, 2.5)

    # Default phone is hard-coded but can be overridden via env var.
    # Use international format (+964...) for Iraqi numbers — Google requires it.
    phone_to_add = os.environ.get("FALLBACK_PHONE", "+9647728257333").strip()

    # Step 1: If 2SV is currently OFF, try to turn it on
    async def _read_body() -> str:
        try:
            return (await page.inner_text("body")).lower()
        except Exception:
            return ""

    body = await _read_body()
    needs_turn_on = any(kw in body for kw in [
        "turn on 2-step verification", "turn on 2-step", "تفعيل التحقّق",
        "تفعيل المصادقة الثنائية", "تفعيل التحقق",
    ])

    # Detect if a phone number is already on the account. If "Add a phone number"
    # is visible in the Second steps section, the account has none.
    phone_already_set = True
    try:
        if "add a phone number" in body or "إضافة رقم هاتف" in body:
            phone_already_set = False
    except Exception:
        pass
    log.info("State: needs_turn_on=%s phone_already_set=%s", needs_turn_on, phone_already_set)

    # ── PROACTIVE: add phone FIRST (before clicking Turn on 2SV) ──
    # This avoids the "Add second steps to your account" dialog entirely on
    # accounts that have only a passkey. We add the phone as a second step
    # while 2SV is still off, then turn 2SV on cleanly.
    if needs_turn_on and not phone_already_set and phone_to_add:
        log.info("Adding phone number proactively BEFORE turning on 2SV")
        await on_progress("step:add_phone_number")
        try:
            added = await _add_phone_number(
                page, phone_to_add, sms_code_provider,
                on_screenshot=on_screenshot, user_id=user_id,
            )
            await _snap("after_proactive_add_phone")
            if added:
                # Reload 2SV page to refresh state
                await _hd(2, 3)
                try:
                    await page.goto(
                        "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
                        wait_until="domcontentloaded",
                    )
                    await _hd(2, 3.5)
                    await _reauth_if_needed(page, current_password)
                    await _hd(1.5, 2.5)
                    await _snap("2sv_page_after_phone")
                except Exception as exc:
                    log.warning("Could not reload 2SV page after phone add: %s", exc)
            else:
                log.warning("Proactive phone add did not succeed — will try fallback path")
        except Exception as exc:
            log.warning("Proactive phone add raised: %s — continuing", exc)

    if needs_turn_on:
        log.info("Clicking 'Turn on 2-Step Verification'")
        if await _click_turn_on_2sv(page):
            await _hd(2, 3.5)
            await _reauth_if_needed(page, current_password)
            await _hd(1.5, 2.5)
            await _snap("after_click_turn_on_2sv")

            # FALLBACK: if the dialog still appears (phone add failed earlier
            # or for any other reason), detect and handle it as before.
            needs_phone = False
            for attempt in range(3):
                body = await _read_body()
                if any(kw in body for kw in [
                    "add second steps", "add another one or add another second step",
                    "doesn't sync across your devices", "first add second steps",
                    "إضافة طرق تحقق", "أضف خطوات تحقق",
                ]):
                    needs_phone = True
                    log.info("Detected 'Add second steps' dialog (body match)")
                    break
                try:
                    html = (await page.content()).lower()
                    if any(kw in html for kw in [
                        "add second steps", "first add second steps",
                        "doesn't sync across your devices",
                    ]):
                        needs_phone = True
                        log.info("Detected 'Add second steps' dialog (html match)")
                        break
                except Exception:
                    pass
                try:
                    go_back = page.locator(
                        '[role="dialog"] button:has-text("Go back"), '
                        '[role="alertdialog"] button:has-text("Go back"), '
                        'div[aria-modal="true"] button:has-text("Go back")'
                    ).first
                    if await go_back.count() and await go_back.is_visible():
                        needs_phone = True
                        log.info("Detected 'Add second steps' dialog (Go back button)")
                        break
                except Exception:
                    pass
                if "two-step-verification" in page.url and attempt > 0:
                    log.info("Still on 2SV page after click — assuming phone needed")
                    needs_phone = True
                    break
                await _hd(1.5, 2.5)

            if needs_phone:
                log.info("Fallback: dialog appeared, adding phone now")
                await _snap("dialog_add_second_steps")
                await _click_text(page, ["go back", "العودة", "رجوع", "back"], 3000)
                await _hd(1.5, 2.5)

                if phone_to_add:
                    await on_progress("step:add_phone_number")
                    added = await _add_phone_number(
                        page, phone_to_add, sms_code_provider,
                        on_screenshot=on_screenshot, user_id=user_id,
                    )
                    await _snap("after_add_phone_number")
                    if added:
                        await _hd(2, 3)
                        try:
                            await page.goto(
                                "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
                                wait_until="domcontentloaded",
                            )
                            await _hd(2, 3.5)
                            await _reauth_if_needed(page, current_password)
                            await _hd(1.5, 2.5)
                        except Exception as exc:
                            log.warning("Could not reload 2SV page: %s", exc)
                        if await _click_turn_on_2sv(page):
                            await _hd(2, 3.5)
                            await _reauth_if_needed(page, current_password)
                            await _hd(1.5, 2.5)
                            await _snap("after_2sv_retry")
                else:
                    raise RuntimeError(
                        "Google يطلب إضافة رقم هاتف لتفعيل 2SV. "
                        "أضف متغير البيئة FALLBACK_PHONE برقم الهاتف وأعد المحاولة."
                    )

            # Skip any leftover wizard prompts
            for label in [
                "skip", "تخطي", "not now", "ليس الآن", "later", "لاحقاً",
                "use another method", "use a different method",
            ]:
                if await _click_text(page, [label], 1500):
                    await _hd(0.6, 1.2)
        else:
            log.warning("Could not find 'Turn on 2-Step Verification' button")

    await on_progress("step:enable_new_authenticator")
    await _snap("before_authenticator_setup")

    # Step 2: Open the Authenticator section
    await _click_text(page, [
        "authenticator app", "authenticator", "add authenticator app",
        "تطبيق المصادقة", "إضافة تطبيق المصادقة",
    ], 5000)
    await _hd(1.5, 2.5)

    # Step 3: Click "Set up authenticator" / "Get started" / "+ Add"
    await _click_text(page, [
        "set up authenticator", "set up", "+ add authenticator",
        "get started", "إعداد", "بدء", "إضافة",
    ], 5000)
    await _hd(1.5, 2.5)

    # Step 4: Reveal the secret key (instead of scanning QR)
    revealed = await _click_text(page, [
        "can't scan it", "can’t scan it", "can't scan", "cannot scan",
        "لا يمكنك المسح", "show secret", "show key",
        "set up without a qr code", "without a qr",
    ], 5000)
    await _hd(1.0, 2.0)

    secret = ""
    if revealed:
        try:
            html = await page.content()
            m = re.search(r"\b([A-Z2-7]{4}\s?[A-Z2-7]{4}\s?[A-Z2-7]{4}\s?[A-Z2-7]{4}[A-Z2-7\s]*)\b", html)
            if m:
                secret = m.group(1).replace(" ", "").upper()
        except Exception:
            pass
    if not secret:
        # Fallback: try reading any visible text element that looks like a base32 chunk
        try:
            chunks = await page.locator("xpath=//*[contains(text(),' ')]").all_text_contents()
            for c in chunks:
                m = re.search(r"\b([A-Z2-7]{4}\s[A-Z2-7]{4}(?:\s[A-Z2-7]{4}){2,})\b", c.upper())
                if m:
                    secret = m.group(1).replace(" ", "").upper()
                    break
        except Exception:
            pass

    if not secret:
        raise RuntimeError("تعذّر استخراج مفتاح Authenticator الجديد من Google")

    log.info("Extracted new TOTP secret (len=%d)", len(secret))
    await _snap("totp_secret_extracted")

    # ── Click "Next" to proceed from QR/secret page to the code-entry page ──
    # The previous text-only search was too greedy and could click wrong buttons.
    # Try specific button selectors first, then fall back to text search.
    log.info("Clicking 'Next' to proceed to TOTP code entry")
    next_clicked = False
    for sel in [
        'button:has-text("Next"):not(:has-text("Try"))',
        '[role="button"]:has-text("Next"):not(:has-text("Try"))',
        'button[jsname]:has-text("Next")',
        'button:has-text("التالي")',
        '[role="button"]:has-text("التالي")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await _hd(0.3, 0.7)
                await loc.click(timeout=4000)
                next_clicked = True
                log.info("Clicked 'Next' on QR page via: %s", sel)
                break
        except Exception:
            continue
    if not next_clicked:
        # Last resort: text search (but only for "next"/"التالي" — not "try")
        next_clicked = await _click_text(page, ["next", "التالي"], 4000)
    if not next_clicked:
        log.warning("Could not find 'Next' button on QR page — pressing Enter")
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
    await _hd(2.0, 3.5)
    await _snap("after_qr_next_click")

    # ── Wait for TOTP code-entry field with retry ──
    code = _now_totp(secret)
    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )
    code_field_found = False
    for attempt in range(3):
        try:
            await page.wait_for_selector(code_sel, timeout=10_000, state="visible")
            code_field_found = True
            log.info("TOTP code field appeared (attempt %d)", attempt + 1)
            break
        except Exception:
            log.warning("TOTP code field not visible (attempt %d/3)", attempt + 1)
            await _snap(f"totp_field_missing_attempt{attempt + 1}")
            # Retry: maybe Next wasn't actually clicked — try again
            if attempt < 2:
                if await _click_text(page, ["next", "التالي"], 3000):
                    log.info("Re-clicked 'Next' on retry %d", attempt + 1)
                else:
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                await _hd(2.0, 3.0)

    if not code_field_found:
        # Dump page state for debugging
        try:
            log.error("TOTP page never appeared. URL=%s, title=%s",
                      page.url, await page.title())
        except Exception:
            pass
        await _snap("totp_field_FAILED")
        raise RuntimeError(
            "صفحة إدخال رمز Authenticator لم تظهر بعد محاولة 3 مرات. "
            "تحقق من screenshots لرؤية الصفحة الفعلية."
        )

    await _type_human_at(page, code_sel, code)
    await _hd(0.5, 1.2)
    if not await _click_text(page, ["verify", "next", "تحقق", "التالي"], 4000):
        await page.keyboard.press("Enter")
    await _hd(2, 4)
    await _snap("totp_code_submitted")
    return secret


async def _verify_new_2fa(page, gmail: str, new_password: str, new_secret: str,
                          on_progress: ProgressCallback) -> bool:
    """Sign out, sign back in with the new credentials, and confirm 2FA works
    without any user interaction. Handles passkey/device-tap automatically."""
    await on_progress("step:verify_new_2fa")
    try:
        await page.goto("https://accounts.google.com/Logout",
                        wait_until="domcontentloaded", timeout=20_000)
        await _hd(2, 3)
    except Exception:
        pass

    await page.goto("https://accounts.google.com/signin/v2/identifier?hl=en",
                    wait_until="domcontentloaded")
    await _hd(1.5, 2.5)

    email_loc = await _wait_for_visible_locator(
        page, ['input[type="email"]', 'input#identifierId'], timeout_ms=20_000,
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
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    # After email, Google may go directly to passkey / 2FA without showing a
    # password field (if account is passwordless). Check first.
    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )

    # Try password field first (normal flow)
    try:
        pwd_loc = await _wait_for_visible_locator(
            page,
            [
                'input[type="password"][name="Passwd"]',
                'input[type="password"]:not([name="hiddenPassword"]):not([aria-hidden="true"]):not([tabindex="-1"])',
                'input[type="password"]',
            ],
            timeout_ms=10_000,
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
    except Exception:
        # No password field — Google may have jumped straight to passkey/2FA
        log.info("No password field shown — proceeding to 2FA detection")

    # Now handle whatever 2FA challenge appears: passkey, device-tap, or TOTP
    # _switch_to_totp_method handles passkey + device-tap + try-another-way
    await _switch_to_totp_method(page)

    # Wait for TOTP input field
    try:
        await page.wait_for_selector(code_sel, timeout=20_000, state="visible")
    except Exception:
        # One more retry: maybe still on passkey screen
        if await _switch_to_totp_method(page):
            try:
                await page.wait_for_selector(code_sel, timeout=15_000, state="visible")
            except Exception:
                # If we can't find TOTP input but URL says we're logged in, treat as success
                cur = page.url
                if "myaccount" in cur or ("signin" not in cur and "challenge" not in cur):
                    log.info("Verify: no TOTP shown but URL looks logged-in (%s)", cur)
                    return True
                raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator في التحقق")
        else:
            cur = page.url
            if "myaccount" in cur or ("signin" not in cur and "challenge" not in cur):
                return True
            raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator في التحقق")

    # Auto-enter the TOTP code using the new secret
    code = _now_totp(new_secret)
    log.info("Auto-entering fresh TOTP code from new secret")
    await _type_human_at(page, code_sel, code)
    await _hd(0.4, 0.9)
    if not await _click_text(page, ["verify", "next", "تحقق", "التالي"], 3000):
        await page.keyboard.press("Enter")
    await _hd(3, 5)

    # Skip "Stay signed in" / "save device" prompts so we land on myaccount
    for label in ["not now", "ليس الآن", "skip", "تخطي"]:
        if await _click_text(page, [label], 1500):
            await _hd(0.6, 1.2)
            break

    cur = page.url
    success = "myaccount" in cur or ("signin" not in cur and "challenge" not in cur)
    log.info("Verify result: success=%s url=%s", success, cur)
    return success


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
    sms_code_provider: Optional[SmsCodeProvider] = None,
    on_screenshot: Optional[ScreenshotCallback] = None,
    on_credentials_ready: Optional[CredentialsCallback] = None,
) -> Dict[str, Any]:
    """Rotate a Google account's password and 2FA secret.

    Args:
        on_progress: async callback receiving status strings.
        gmail: account email.
        old_password: current password.
        old_totp_secret: current TOTP secret (base32) or empty.
        user_id: telegram user id (for screenshot filenames).
        sms_code_provider: optional async callable that returns an SMS code
            string. The bot's main handler should pass a function that:
              1. Sends a Telegram notification asking the user for the code
              2. Waits for the user's reply
              3. Returns the code
            If Google never asks for an SMS code, this is never called.
        on_screenshot: optional callback called after every major step with
            (step_name, screenshot_path). Use it to forward screenshots to
            the user via Telegram so they can debug visually.
        on_credentials_ready: optional callback called as soon as a new
            credential is available, with (label, value). Examples:
                ("new_password", "VJ77X2305xx30j5")
                ("new_totp_secret", "JBSWY3DPEHPK3PXP")
            This lets the bot send the new password to the user IMMEDIATELY
            after it's set, so even if a later step fails, the user has
            their password already.
    """
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

    current_step = "launch_browser"

    async def _progress_wrap(s: str) -> None:
        nonlocal current_step
        if s.startswith("step:"):
            current_step = s.split(":", 1)[1]
        try:
            await on_progress(_format_progress(current_step))
        except Exception:
            pass

    cleanup = browser = ctx = page = None
    pw_cm = pw = None

    async def _emit_credential(label: str, value: str) -> None:
        """Notify the caller as soon as a credential is ready."""
        if on_credentials_ready is None or not value:
            return
        try:
            await on_credentials_ready(label, value)
        except Exception as exc:
            log.warning("on_credentials_ready callback failed: %s", exc)

    try:
        await _progress_wrap("step:launch_browser")

        # 1) Try Camoufox (best anti-detection)
        result_launch = await _launch_camoufox()
        engine = "camoufox"
        if not result_launch:
            # 2) Try Patchright
            result_launch = await _launch_patchright()
            engine = "patchright"
        if not result_launch:
            # 3) Fallback to Playwright Chromium
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                result["error"] = "لا توجد متصفحات مثبتة (Camoufox/Patchright/Playwright)"
                return result
            pw_cm = async_playwright()
            pw = await pw_cm.__aenter__()
            result_launch = await _launch_playwright(pw)
            engine = "playwright"

        cleanup, browser, ctx, page = result_launch
        log.info("Browser engine: %s", engine)

        page_holder = {"page": page, "ctx": ctx}
        await _do_login(page_holder, gmail, old_password, old_totp_secret, _progress_wrap)
        page = page_holder["page"]  # may have been swapped
        await _shoot(page, user_id, "after_login", on_screenshot)

        new_password = await _change_password(page, _progress_wrap)
        result["new_password"] = new_password
        # Send the new password to the user IMMEDIATELY
        await _emit_credential("new_password", new_password)
        await _shoot(page, user_id, "after_change_password", on_screenshot)

        new_secret = await _setup_new_authenticator(
            page, _progress_wrap, new_password, sms_code_provider,
            on_screenshot=on_screenshot, user_id=user_id,
        )
        result["new_totp_secret"] = new_secret
        # Send the new 2FA secret to the user IMMEDIATELY
        await _emit_credential("new_totp_secret", new_secret)
        await _shoot(page, user_id, "after_setup_2fa", on_screenshot)

        await _verify_new_2fa(page, gmail, new_password, new_secret, _progress_wrap)
        await _shoot(page, user_id, "after_verify", on_screenshot)

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
            # Also push the failure screenshot via the live callback
            if cap.get("screenshot_path") and on_screenshot is not None:
                try:
                    await on_screenshot(f"error_{current_step}", cap["screenshot_path"])
                except Exception:
                    pass
        return result

    finally:
        if cleanup:
            try:
                await cleanup()
            except Exception:
                pass
        if pw_cm:
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
