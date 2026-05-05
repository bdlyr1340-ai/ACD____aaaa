"""Google Account Rotator — Camoufox edition (FIXED v2026).

Public entry point:
    rotate_google_account(on_progress, gmail, old_password, old_totp_secret, user_id)

Changes from previous version:
  1. _change_password now generates a RANDOM password instead of fixed one.
  2. _setup_new_authenticator adds phone INSIDE 2-Step Verification page (not recovery phone).
  3. _setup_new_authenticator sends secret + url + code to user IMMEDIATELY after extraction.
  4. rotate_google_account returns old_password in result for user records.
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
    """Capture a screenshot and notify the caller via callback (if provided)."""
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


def _looks_like_password_challenge(body_text: str, url: str = "") -> bool:
    body = (body_text or "").lower()
    cur = (url or "").lower()
    return any(kw in body for kw in [
        "to continue, first verify",
        "first verify that it's you",
        "first verify that it’s you",
        "first verify that its you",
        "first verify it",
        "verify that it's you",
        "verify that it’s you",
        "verify that its you",
        "verify it's you",
        "verify it’s you",
        "welcome",
        "enter your password",
        # Arabic
        "للمتابعة، تحقّق",
        "تحقق من هويتك",
        "تأكّد من هويتك",
        "أدخل كلمة المرور",
        "مرحباً",
    ]) or any(path in cur for path in [
        "signin/challenge",
        "signin/v2/challenge",
        "challenge/pwd",
        "/v3/signin/",
        "accounts.google.com/v3/signin",
    ])


async def _write_admin_debug_reports(
    page,
    *,
    user_id: int,
    step: str,
    error_text: str,
    screenshot_path: Optional[str] = None,
    html_path: Optional[str] = None,
    old_password: str = "",
    new_password: str = "",
    password_used_for_reauth: str = "",
) -> Dict[str, Optional[str]]:
    ts = int(time.time() * 1000)
    base = f"{user_id}_{ts}_{step}"
    problem_path = os.path.join(SHOTS_DIR, f"{base}_problem.txt")
    solution_path = os.path.join(SHOTS_DIR, f"{base}_solution.txt")

    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""

    body_excerpt = (body or "").strip()
    if len(body_excerpt) > 4000:
        body_excerpt = body_excerpt[:4000] + "\n...[truncated]"

    password_gate = _looks_like_password_challenge(body, url)
    try:
        totp_visible = await page.locator(
            'input[type="tel"], input#totpPin, input[name="totpPin"], input[autocomplete="one-time-code"]'
        ).first.is_visible()
    except Exception:
        totp_visible = False

    problem_text = (
        "Google rotation debug report\n"
        f"step: {step}\n"
        f"error: {error_text}\n"
        f"url: {url}\n"
        f"title: {title}\n"
        f"password_gate_detected: {password_gate}\n"
        f"totp_input_visible: {totp_visible}\n"
        f"old_password: {old_password or '<empty>'}\n"
        f"new_password: {new_password or '<not-created>'}\n"
        f"password_used_for_reauth: {password_used_for_reauth or '<unknown>'}\n"
        f"screenshot_path: {screenshot_path or '<none>'}\n"
        f"html_path: {html_path or '<none>'}\n\n"
        "body_excerpt:\n"
        f"{body_excerpt}\n"
    )
    solution_text = (
        "Suggested recovery logic\n"
        "1. If Google shows 'Enter your password' / 'Verify it's you', try the NEW password first.\n"
        "2. If the new password is rejected, retry once with the OLD password.\n"
        "3. After the password challenge clears, wait again for the Authenticator code field.\n"
        "4. If the code field is still missing, click Next again or switch through 'Try another way' to Authenticator.\n"
        "5. Use the attached screenshot + HTML dump + this report to inspect the exact blocker page.\n"
    )

    out: Dict[str, Optional[str]] = {"problem_txt_path": None, "solution_txt_path": None}
    try:
        with open(problem_path, "w", encoding="utf-8") as f:
            f.write(problem_text)
        out["problem_txt_path"] = problem_path
    except Exception as exc:
        log.warning("Problem debug txt write failed: %s", exc)
    try:
        with open(solution_path, "w", encoding="utf-8") as f:
            f.write(solution_text)
        out["solution_txt_path"] = solution_path
    except Exception as exc:
        log.warning("Solution debug txt write failed: %s", exc)
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
    """Return the first truly-visible locator, ignoring Google's hidden traps."""
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
    """If Google shows 'Use your passkey', click it then fall through to TOTP."""
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
    """Click the 'Change password' button inside Google's confirmation dialog."""
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
        log.info("No confirmation dialog appeared (may have submitted directly)")
        return False

    await _hd(0.5, 1.2)

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

    try:
        dlg = page.locator('[role="dialog"], [role="alertdialog"], div[aria-modal="true"]').first
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
    """Try multiple strategies to click the 'Change password' button reliably."""
    selectors = [
        'button:has-text("Change password")',
        'button:has-text("Change Password")',
        'button:has-text("تغيير كلمة المرور")',
        'button:has-text("Save")',
        '[role="button"]:has-text("Change password")',
        '[role="button"]:has-text("تغيير كلمة المرور")',
        'button[jsname]:has-text("Change")',
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
                log.info("Clicked change-password button via: %s", sel)
                return True
        except Exception as exc:
            log.debug("Selector %s failed: %s", sel, exc)
            continue

    if await _click_text(page, [
        "change password", "تغيير كلمة المرور", "تغيير كلمة السر", "save", "حفظ",
    ], 5000):
        log.info("Clicked change-password button via text search")
        return True

    return False


async def _change_password(page, on_progress: ProgressCallback,
                           old_password: str = "",
                           custom_new_password: str = "") -> str:
    await on_progress("step:open_security_page")
    
    # ═════════════════════════════════════════════════════════════════
    # FIX #1: Generate random password instead of fixed one!
    # ═════════════════════════════════════════════════════════════════
    if custom_new_password and len(custom_new_password) >= 8:
        new_pwd = custom_new_password.strip()
        log.info("Using custom password from bot (len=%d)", len(new_pwd))
    else:
        new_pwd = _generate_strong_password(16)
        log.info("Generated random password (len=%d)", len(new_pwd))

    await page.goto(
        "https://myaccount.google.com/signinoptions/password?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)

    # Google often shows a "verify it's you" / Welcome page before allowing
    # password changes. Detect and handle it using the OLD password.
    if old_password:
        reauth_done = await _reauth_if_needed(page, old_password, timeout_ms=15_000)
        if reauth_done:
            log.info("Re-auth completed before password change")
            await _hd(2, 3)

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

    log.info("Filling new password into both fields")

    async def _fill_and_verify(field_index: int) -> bool:
        """Fill field N with new_pwd, verify, retry once if mismatch."""
        for attempt in range(2):
            try:
                f = fields.nth(field_index)
                await f.click()
                await _hd(0.2, 0.4)
                await f.fill("")
                await _hd(0.15, 0.3)
                await f.fill(new_pwd)
                await _hd(0.3, 0.6)
                actual = await f.input_value()
                if actual == new_pwd:
                    log.info("Field #%d filled correctly (len=%d)", field_index, len(actual))
                    return True
                log.warning("Field #%d mismatch (got len=%d, expected %d) — retrying",
                            field_index, len(actual), len(new_pwd))
            except Exception as exc:
                log.warning("Field #%d fill attempt %d failed: %s", field_index, attempt, exc)
                await _hd(0.5, 1.0)
        return False

    if not await _fill_and_verify(0):
        raise RuntimeError("تعذّر ملء حقل كلمة السر الجديدة بشكل صحيح")
    await _hd(0.4, 0.8)
    if not await _fill_and_verify(1):
        raise RuntimeError("تعذّر ملء حقل تأكيد كلمة السر بشكل صحيح")
    await _hd(0.6, 1.2)

    # Tab out so Google validates the strength meter
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass
    await _hd(0.8, 1.6)

    # Click "Change password"
    clicked = await _click_change_password_button(page)
    if not clicked:
        try:
            await fields.nth(1).click()
            await _hd(0.2, 0.4)
            await page.keyboard.press("Enter")
            log.info("Submitted via Enter key (fallback)")
        except Exception as exc:
            raise RuntimeError(f"تعذّر الضغط على زر Change password: {exc}")

    # Confirmation DIALOG
    await _hd(1.5, 2.5)
    await _confirm_change_password_dialog(page)

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
    """Click 'Turn on 2-Step Verification' button if visible."""
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


# ════════════════════════════════════════════════════════════════════════
# NEW: Add phone number INSIDE 2-Step Verification page (not recovery phone)
# ════════════════════════════════════════════════════════════════════════

async def _add_2sv_phone_number(
    page,
    phone: str,
    password: str,
    *,
    on_screenshot: Optional[ScreenshotCallback] = None,
    user_id: int = 0,
) -> bool:
    """Add a phone number inside the 2-Step Verification page (Second steps).
    
    This matches the user's screenshots exactly:
      1. Click "Add a phone number" in 2SV list
      2. Click "+ Add phone number" in phones sub-page
      3. Fill country (Iraq) + number (+964 772 825 7333)
      4. Click Next
    """
    log.info("Adding 2SV phone: %s", phone)

    async def _snap(tag: str) -> None:
        await _shoot(page, user_id, f"2sv_phone_{tag}", on_screenshot)

    # Step 1: Click "Add a phone number" in 2SV main page
    # (yellow badge under "Phone number" in screenshot 1)
    clicked = await _click_text(page, [
        "add a phone number", "add phone number",
        "إضافة رقم هاتف", "أضف رقم",
    ], 5000)

    if not clicked:
        # Try clicking the Phone number row/section
        for sel in [
            'div:has-text("Phone number"):has-text("Add")',
            'div[role="button"]:has-text("Phone number")',
            'div[jscontroller]:has-text("Phone number"):has-text("Add")',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    clicked = True
                    log.info("Clicked Phone number section")
                    break
            except Exception:
                pass

    if not clicked:
        log.warning("Could not find 'Add a phone number' in 2SV page")
        await _snap("no_cta")
        return False

    await _hd(2, 4)
    await _snap("after_cta")

    # Step 2: Handle re-auth if shown
    await _reauth_if_needed(page, password, timeout_ms=12_000)
    await _hd(2, 3)

    # Step 3: May land on /two-step-verification/phones sub-page
    # Click "+ Add phone number" if shown (screenshot 2)
    await _click_text(page, ["add phone number", "+ add"], 4000)
    await _hd(1, 2)

    # Step 4: Fill phone form (screenshot 3)
    await _reauth_if_needed(page, password, timeout_ms=10_000)
    await _hd(2, 3)

    phone_sel = (
        'input[type="tel"], input[name="phoneNumber"], '
        'input[aria-label*="phone" i], input[autocomplete="tel"]'
    )

    # Select country: Iraq
    try:
        dropdown_selectors = [
            'div[role="combobox"][aria-label*="country" i]',
            'div[role="button"][aria-label*="Country" i]',
            'div[jscontroller] div[role="combobox"]',
            'div[jsname] div[role="button"]:has-text("+")',
        ]
        for dsel in dropdown_selectors:
            try:
                dd = page.locator(dsel).first
                if await dd.count() and await dd.is_visible():
                    await dd.click(timeout=4000)
                    await _hd(0.8, 1.5)
                    log.info("Opened country dropdown")
                    try:
                        await page.keyboard.type("Iraq", delay=80)
                        await _hd(0.5, 1.0)
                    except Exception:
                        pass
                    opt_sel = '[role="option"]:has-text("Iraq")'
                    try:
                        opt = page.locator(opt_sel).first
                        if await opt.count() and await opt.is_visible():
                            await opt.click(timeout=3000)
                            log.info("Selected country: Iraq")
                            break
                    except Exception:
                        pass
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                    break
            except Exception:
                continue
    except Exception as exc:
        log.warning("Country selection failed: %s", exc)

    await _hd(1.0, 2.0)

    # Type phone number
    try:
        await page.wait_for_selector(phone_sel, timeout=5_000, state="visible")
    except Exception:
        pass

    phone_loc = page.locator(phone_sel).first
    if not (await phone_loc.count() and await phone_loc.is_visible()):
        log.warning("Phone input not visible")
        return False

    await phone_loc.click()
    await _hd(0.3, 0.7)

    # Clean number: remove +964 prefix since country is selected
    raw = re.sub(r"\D", "", phone.strip())
    if raw.startswith("964"):
        local = raw[3:].lstrip("0")
    else:
        local = raw.lstrip("0")

    try:
        await phone_loc.fill("")
        await _hd(0.2, 0.4)
    except Exception:
        pass
    await phone_loc.type(local, delay=random.randint(40, 90))
    await _hd(0.6, 1.2)
    await _snap("typed")

    # Ensure "Receive codes by text message" is selected (radio button in screenshot 3)
    try:
        sms_radio = page.locator(
            'div[role="radio"]:has-text("text message"), '
            'div[role="radio"]:has-text("Receive codes by text message")'
        ).first
        if await sms_radio.count() and await sms_radio.is_visible():
            await sms_radio.click(timeout=3000)
            log.info("Selected SMS option")
    except Exception:
        pass

    # Click Next
    next_clicked = False
    for sel in [
        'button:has-text("Next")',
        '[role="button"]:has-text("Next")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=4000)
                next_clicked = True
                log.info("Submitted phone via: %s", sel)
                break
        except Exception:
            continue

    if not next_clicked:
        await _click_text(page, ["next", "التالي"], 4000)
        if not next_clicked:
            await page.keyboard.press("Enter")

    log.info("Submitted phone number")
    await _hd(3, 5)
    await _snap("after_submit")

    # Check if successful (no SMS code usually needed during 2SV setup)
    try:
        body = (await page.inner_text("body")).lower()
        if any(k in body for k in ["added", "verified", "تم", "success", "phone number"]):
            log.info("Phone appears added successfully")
            return True
    except Exception:
        pass

    return True


# ---------------------------------------------------------------------------
# 2FA Setup — REWRITTEN to match user's screenshots exactly
# ---------------------------------------------------------------------------

async def _setup_new_authenticator(
    page,
    on_progress: ProgressCallback,
    current_password: str,
    sms_code_provider: Optional[SmsCodeProvider] = None,
    *,
    on_screenshot: Optional[ScreenshotCallback] = None,
    user_id: int = 0,
    old_password: str = "",
    on_password_used: Optional[Callable[[str], Awaitable[None]]] = None,
    on_credentials_ready: Optional[CredentialsCallback] = None,
) -> str:
    async def _snap(tag: str) -> None:
        await _shoot(page, user_id, tag, on_screenshot)

    password_candidates = [p for p in [current_password, old_password] if p]

    await on_progress("step:open_2fa_settings")
    await page.goto(
        "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)
    await _snap("2sv_page_loaded")

    # Re-auth may be required to open the 2SV settings page itself
    await _reauth_if_needed(page, [current_password, old_password] if old_password else current_password, on_password_used=on_password_used)
    await _hd(1.5, 2.5)

    # Default phone is hard-coded for Iraq — FIXED, never changes
    phone_to_add = DEFAULT_FALLBACK_PHONE

    # Step 1: Read page state
    async def _read_body() -> str:
        try:
            return (await page.inner_text("body")).lower()
        except Exception:
            return ""

    body = await _read_body()
    is_2sv_on = any(kw in body for kw in [
        "turn off 2-step verification", "2-step verification is on",
        "you're now protected", "on since",
    ])
    has_phone = "0772" in body or "+964" in body or (
        "phone number" in body and "add a phone number" not in body
    )
    has_authenticator = "add authenticator app" not in body and "authenticator" in body

    log.info("2SV state: on=%s phone=%s auth=%s", is_2sv_on, has_phone, has_authenticator)

    # ════════════════════════════════════════════════════════════════════
    # FIX #2: Add phone INSIDE 2-Step Verification (not recovery phone)
    # ════════════════════════════════════════════════════════════════════
    if not has_phone:
        await on_progress("step:add_phone_number")
        added = await _add_2sv_phone_number(
            page, phone_to_add, current_password,
            on_screenshot=on_screenshot, user_id=user_id,
        )
        await _snap("after_add_phone")
        if not added:
            log.warning("2SV phone add returned False — will try to continue")

        # Re-open 2SV page to refresh state
        await _hd(2, 3)
        await page.goto(
            "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
            wait_until="domcontentloaded",
        )
        await _hd(2, 3.5)
        await _reauth_if_needed(page, password_candidates, on_password_used=on_password_used)
        await _hd(1.5, 2.5)
        await _snap("2sv_page_after_phone")

    # Step 2: Add authenticator if not present
    secret = ""
    if not has_authenticator:
        await on_progress("step:enable_new_authenticator")
        await _snap("before_authenticator_setup")

        # Click "Authenticator" or "Add authenticator app"
        clicked = await _click_text(page, [
            "authenticator app", "authenticator", "add authenticator app",
            "تطبيق المصادقة", "إضافة تطبيق المصادقة",
        ], 5000)

        if not clicked:
            for sel in [
                'div:has-text("Authenticator"):has-text("Add")',
                'div[role="button"]:has-text("Authenticator")',
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=4000)
                        clicked = True
                        break
                except Exception:
                    pass

        if not clicked:
            raise RuntimeError("Could not find 'Add authenticator app' in 2SV page")

        await _hd(2, 3)

        # Click "Set up authenticator" / "Get started" / "+ Add"
        await _click_text(page, [
            "set up authenticator", "set up", "+ add authenticator",
            "get started", "إعداد", "بدء", "إضافة",
        ], 5000)
        await _hd(2, 3)

        # Handle re-auth inside wizard
        await _reauth_if_needed(page, password_candidates, on_password_used=on_password_used)
        await _hd(1.5, 2.5)

        # Click "Can't scan it?" — reveals secret key (screenshots 5-7)
        revealed = await _click_text(page, [
            "can't scan it", "can’t scan it", "can't scan", "cannot scan",
            "لا يمكنك المسح", "show secret", "show key",
            "set up without a qr code", "without a qr",
        ], 5000)
        await _hd(1.0, 2.0)

        # Extract secret from page text
        if revealed:
            try:
                html = await page.content()
                m = re.search(r"\b([A-Z2-7]{4}(?:\s[A-Z2-7]{4}){7,})\b", html.upper())
                if m:
                    secret = m.group(1).replace(" ", "").upper()
                    log.info("Extracted secret from HTML (len=%d)", len(secret))
            except Exception:
                pass

        if not secret:
            try:
                chunks = await page.locator("body").all_text_contents()
                for c in chunks:
                    m = re.search(r"\b([A-Z2-7]{4}(?:\s[A-Z2-7]{4}){7,})\b", c.upper())
                    if m:
                        secret = m.group(1).replace(" ", "").upper()
                        log.info("Extracted secret from text (len=%d)", len(secret))
                        break
            except Exception:
                pass

        if not secret or len(secret) < 16:
            raise RuntimeError("تعذّر استخراج مفتاح Authenticator الجديد من Google")

        log.info("Extracted new TOTP secret (len=%d)", len(secret))
        await _snap("totp_secret_extracted")

        # ════════════════════════════════════════════════════════════════
        # FIX #3: Send secret + url + code to user IMMEDIATELY
        # ════════════════════════════════════════════════════════════════
        tfa_url = f"https://2fa.fb.tools/{secret}"
        current_code = _now_totp(secret)

        if on_credentials_ready is not None:
            try:
                await on_credentials_ready("new_totp_secret", secret)
                await on_credentials_ready("totp_url", tfa_url)
                await on_credentials_ready("totp_code", current_code)
                log.info("Sent secret+url+code to user immediately")
            except Exception as exc:
                log.warning("emit credentials failed: %s", exc)

        # Click Next to proceed to code entry (screenshot 9)
        next_clicked = False
        for sel in [
            'button:has-text("Next"):not(:has-text("Try"))',
            '[role="button"]:has-text("Next"):not(:has-text("Try"))',
            'button[jsname]:has-text("Next")',
            'button:has-text("التالي")',
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
            next_clicked = await _click_text(page, ["next", "التالي"], 4000)
        if not next_clicked:
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass

        await _hd(2.0, 3.5)
        await _snap("after_qr_next_click")

        # Wait for TOTP code entry field (screenshot 9: "Enter the 6-digit code")
        code = _now_totp(secret)
        code_sel = (
            'input[type="tel"], input#totpPin, input[name="totpPin"], '
            'input[autocomplete="one-time-code"]'
        )

        try:
            await page.wait_for_selector(code_sel, timeout=15_000, state="visible")
        except Exception:
            raise RuntimeError("لم يظهر حقل إدخال كود Authenticator")

        # Enter code in Google
        try:
            code = _now_totp(secret)
        except Exception:
            pass
        await _type_human_at(page, code_sel, code)
        await _hd(0.5, 1.2)

        verify_clicked = False
        for sel in [
            'button:has-text("Next")',
            'button:has-text("Verify")',
            '[role="button"]:has-text("Next")',
            '[role="button"]:has-text("Verify")',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    verify_clicked = True
                    break
            except Exception:
                pass

        if not verify_clicked:
            await _click_text(page, ["verify", "next", "تحقق", "التالي"], 4000)
            if not verify_clicked:
                await page.keyboard.press("Enter")

        await _hd(3, 5)
        await _snap("totp_code_submitted")

    # Step 3: Turn on 2-Step Verification (screenshots 10-11)
    if not is_2sv_on:
        log.info("Clicking 'Turn on 2-Step Verification'")
        await _hd(2, 3)
        if await _click_turn_on_2sv(page):
            await _hd(3, 5)

            # Click Done / Got it (screenshot 11: "Done" button)
            for label in [
                "done", "got it", "finish", "تم", "موافق", "done",
            ]:
                if await _click_text(page, [label], 2500):
                    await _hd(1.0, 2.0)
                    break

            await _snap("after_2sv_turned_on")
            log.info("2FA turned on successfully")
        else:
            log.warning("Could not find 'Turn on 2-Step Verification' button")

    return secret


async def _verify_new_2fa(page, gmail: str, new_password: str, new_secret: str,
                          on_progress: ProgressCallback, old_password: str = "") -> bool:
    """Sign out, sign back in with the new credentials, and confirm 2FA works."""
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

    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )

    password_candidates = [p for p in [new_password, old_password] if p]

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
        del pwd_loc
        await _reauth_if_needed(page, password_candidates, timeout_ms=15_000)
        await _hd(2.5, 4)
    except Exception:
        log.info("No password field — proceeding to 2FA detection")

    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    if _looks_like_password_challenge(body, page.url):
        await _reauth_if_needed(page, password_candidates, timeout_ms=15_000)
        await _hd(1.5, 2.5)

    await _switch_to_totp_method(page)

    try:
        await page.wait_for_selector(code_sel, timeout=20_000, state="visible")
    except Exception:
        if await _switch_to_totp_method(page):
            try:
                await page.wait_for_selector(code_sel, timeout=15_000, state="visible")
            except Exception:
                cur = page.url
                if "myaccount" in cur or ("signin" not in cur and "challenge" not in cur):
                    return True
                raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator في التحقق")
        else:
            cur = page.url
            if "myaccount" in cur or ("signin" not in cur and "challenge" not in cur):
                return True
            raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator في التحقق")

    code = _now_totp(new_secret)
    log.info("Auto-entering fresh TOTP code from new secret")
    await _type_human_at(page, code_sel, code)
    await _hd(0.4, 0.9)
    if not await _click_text(page, ["verify", "next", "تحقق", "التالي"], 3000):
        await page.keyboard.press("Enter")
    await _hd(3, 5)

    for label in ["not now", "ليس الآن", "skip", "تخطي"]:
        if await _click_text(page, [label], 1500):
            await _hd(0.6, 1.2)
            break

    cur = page.url
    success = "myaccount" in cur or ("signin" not in cur and "challenge" not in cur)
    return success


# ---------------------------------------------------------------------------
# Re-auth helper (full definition — moved here to preserve file structure)
# ---------------------------------------------------------------------------

async def _reauth_if_needed(
    page,
    current_password,
    timeout_ms: int = 25_000,
    *,
    on_password_used: Optional[Callable[[str], Awaitable[None]]] = None,
) -> bool:
    """If Google re-prompts for the password, fill it automatically."""
    if isinstance(current_password, str):
        candidates = [current_password]
    else:
        candidates = [p for p in current_password if p]
    if not candidates:
        return False

    pwd_sel = (
        'input[type="password"][name="Passwd"], '
        'input[type="password"]:not([name="hiddenPassword"])'
        ':not([aria-hidden="true"]):not([tabindex="-1"])'
    )

    deadline = time.time() + (timeout_ms / 1000.0)
    detected = False
    while time.time() < deadline:
        try:
            loc = page.locator(pwd_sel).first
            if await loc.count() and await loc.is_visible():
                detected = True
                break
        except Exception:
            pass
        try:
            cur = page.url.lower()
            if any(p in cur for p in [
                "signin/challenge", "signin/v2/challenge", "challenge/pwd",
                "/v3/signin/", "rejected", "/signin/v2/sl/pwd",
                "accounts.google.com/v3/signin",
            ]):
                try:
                    await page.wait_for_selector(pwd_sel, timeout=5_000, state="visible")
                    detected = True
                    break
                except Exception:
                    pass
        except Exception:
            pass
        try:
            body_text = (await page.inner_text("body")).lower()
            if any(kw in body_text for kw in [
                "to continue, first verify",
                "verify it's you",
                "welcome",
                "enter your password",
                "للمتابعة، تحقّق",
                "تحقق من هويتك",
                "أدخل كلمة المرور",
                "مرحباً",
            ]):
                try:
                    await page.wait_for_selector(pwd_sel, timeout=5_000, state="visible")
                    detected = True
                    break
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(0.5)

    if not detected:
        return False

    for idx, pwd in enumerate(candidates):
        label = f"#{idx+1} of {len(candidates)}"
        log.info("Re-auth: trying password candidate %s (len=%d)", label, len(pwd))
        try:
            loc = page.locator(pwd_sel).first
            if not (await loc.count() and await loc.is_visible()):
                return True

            try:
                await loc.fill("")
                await _hd(0.2, 0.4)
            except Exception:
                pass
            await loc.click()
            await _hd(0.3, 0.7)
            for ch in pwd:
                await page.keyboard.type(ch)
                await asyncio.sleep(random.uniform(0.04, 0.12))
            await _hd(0.4, 0.8)

            nxt = page.locator("#passwordNext").first
            if await nxt.count() == 0:
                nxt = page.get_by_role("button", name="Next")
            if await nxt.count() > 0:
                await nxt.click()
            else:
                await page.keyboard.press("Enter")
            await _hd(3, 5)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            still_there = page.locator(pwd_sel).first
            field_visible = False
            try:
                if await still_there.count() and await still_there.is_visible():
                    field_visible = True
            except Exception:
                pass

            if not field_visible:
                log.info("Re-auth SUCCESS with candidate %s", label)
                if on_password_used is not None:
                    try:
                        await on_password_used(pwd)
                    except Exception as exc:
                        log.warning("on_password_used callback failed: %s", exc)
                return True

            try:
                body = (await page.inner_text("body")).lower()
            except Exception:
                body = ""
            rejected = any(kw in body for kw in [
                "wrong password", "incorrect password", "couldn't verify",
                "couldn’t verify", "كلمة المرور غير صحيحة", "password is incorrect",
            ])
            if rejected:
                log.warning("Re-auth: password candidate %s REJECTED — trying next", label)
                continue
            else:
                log.warning("Re-auth: field still visible but no error msg — assuming success")
                if on_password_used is not None:
                    try:
                        await on_password_used(pwd)
                    except Exception:
                        pass
                return True

        except Exception as exc:
            log.warning("Re-auth attempt with %s failed: %s", label, exc)
            continue

    log.warning("Re-auth: ALL %d password candidates failed", len(candidates))
    return False


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
    custom_new_password: str = "",
) -> Dict[str, Any]:
    """Rotate a Google account's password and 2FA secret.

    Returns dict with:
      - success: bool
      - gmail: str
      - old_password: str          ← FIX #4: included for user records
      - new_password: str
      - new_totp_secret: str
      - totp_url: str
      - totp_code: str
      - password_used_for_reauth: str
      - step: str
      - error: str
    """
    result: Dict[str, Any] = {
        "success": False,
        "gmail": gmail,
        "old_password": old_password,           # ← FIX #4
        "new_password": None,
        "new_totp_secret": None,
        "totp_url": None,
        "totp_code": None,
        "password_used_for_reauth": None,
        "step": "launch_browser",
        "error": None,
        "screenshot_path": None,
        "html_path": None,
        "problem_txt_path": None,
        "solution_txt_path": None,
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
            label = STEP_LABELS_AR.get(current_step, current_step)
            idx = ROTATION_STEPS.index(current_step) + 1
            bar = ""
            for i, _ in enumerate(ROTATION_STEPS, 1):
                bar += "🟢" if i <= idx else "⚪️"
            msg = f"{bar}\n\n🔧 الخطوة {idx}/{len(ROTATION_STEPS)}: *{label}*"
            await on_progress(msg)
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

        result_launch = await _launch_camoufox()
        engine = "camoufox"
        if not result_launch:
            result_launch = await _launch_patchright()
            engine = "patchright"
        if not result_launch:
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

        # 1. Login
        await _do_login(page_holder, gmail, old_password, old_totp_secret, _progress_wrap)
        page = page_holder["page"]
        await _shoot(page, user_id, "after_login", on_screenshot)

        # 2. Change password (RANDOM!)
        new_password = await _change_password(
            page, _progress_wrap, old_password,
            custom_new_password=custom_new_password,
        )
        result["new_password"] = new_password
        await _emit_credential("new_password", new_password)
        await _shoot(page, user_id, "after_change_password", on_screenshot)

        async def _notify_password_used(pwd_used: str) -> None:
            label = "new_password" if pwd_used == new_password else "old_password_still_active"
            log.info("Password actually used during reauth: %s", label)
            result["password_used_for_reauth"] = pwd_used
            if on_credentials_ready is not None:
                try:
                    await on_credentials_ready("password_used_for_reauth", pwd_used)
                except Exception as exc:
                    log.warning("on_credentials_ready callback failed: %s", exc)

        # 3. Setup 2FA (phone inside 2SV + authenticator + turn on)
        new_secret = await _setup_new_authenticator(
            page, _progress_wrap, new_password, sms_code_provider,
            on_screenshot=on_screenshot, user_id=user_id,
            old_password=old_password,
            on_password_used=_notify_password_used,
            on_credentials_ready=on_credentials_ready,
        )
        result["new_totp_secret"] = new_secret
        if new_secret:
            result["totp_url"] = f"https://2fa.fb.tools/{new_secret}"
            try:
                result["totp_code"] = _now_totp(new_secret)
            except Exception:
                result["totp_code"] = None

        await _shoot(page, user_id, "after_setup_2fa", on_screenshot)

        # 4. Verify (non-fatal)
        try:
            if new_secret:
                await _verify_new_2fa(page, gmail, new_password, new_secret, _progress_wrap, old_password)
            await _shoot(page, user_id, "after_verify", on_screenshot)
        except Exception as exc:
            log.warning("verify_new_2fa failed (non-fatal): %s", exc)
            await _shoot(page, user_id, "verify_failed_nonfatal", on_screenshot)

        # 5. Final security page screenshot
        try:
            await page.goto("https://myaccount.google.com/security?hl=en",
                            wait_until="domcontentloaded", timeout=20_000)
            await _hd(2, 3)
            await _shoot(page, user_id, "final_security_page", on_screenshot)
        except Exception:
            pass

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
            txts = await _write_admin_debug_reports(
                page,
                user_id=user_id,
                step=current_step,
                error_text=result["error"],
                screenshot_path=result.get("screenshot_path"),
                html_path=result.get("html_path"),
                old_password=old_password,
                new_password=result.get("new_password") or "",
                password_used_for_reauth=result.get("password_used_for_reauth") or "",
            )
            result["problem_txt_path"] = txts.get("problem_txt_path")
            result["solution_txt_path"] = txts.get("solution_txt_path")
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
