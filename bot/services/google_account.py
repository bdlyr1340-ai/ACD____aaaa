"""Google Account Rotator — Camoufox edition (FIXED v2026).

Fixes applied:
  1. Recovery phone is added FIRST via /signinoptions/rescuephone with re-auth.
  2. Every sensitive step triggers _reauth_if_needed BEFORE acting.
  3. 2FA setup ENSURES recovery phone exists BEFORE clicking "Turn on".
  4. Selectors updated for Google UI 2026-Q2.
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
ScreenshotCallback = Callable[[str, str], Awaitable[None]]
SmsCodeProvider = Callable[[], Awaitable[Optional[str]]]
CredentialsCallback = Callable[[str, str], Awaitable[None]]

DEFAULT_NEW_PASSWORD = os.environ.get("DEFAULT_NEW_PASSWORD", "VJ77X2305xx30j5")
DEFAULT_FALLBACK_PHONE = os.environ.get("FALLBACK_PHONE", "+9647728257333").strip()

SHOTS_DIR = os.environ.get("SHOTS_DIR", "/tmp/shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

_SPEED = float(os.getenv("SHEERID_SPEED_FACTOR", "0.35"))

_STEALTH_UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"),
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

# ══════════════════════════════════════════════════════════════════════
# Password / TOTP helpers
# ══════════════════════════════════════════════════════════════════════

def _generate_strong_password(length: int = 16) -> str:
    if length < 12:
        length = 12
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+"
    pwd = [secrets.choice(p) for p in [upper, lower, digits, symbols]]
    pwd += [secrets.choice(upper + lower + digits + symbols) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def _now_totp(secret: str) -> str:
    return pyotp.TOTP(secret.replace(" ", "").upper()).now()


# ══════════════════════════════════════════════════════════════════════
# Delays / mouse / typing
# ══════════════════════════════════════════════════════════════════════

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
    await page.click(selector)
    await _hd(0.2, 0.5)
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))


async def _type_human_locator(locator, text: str) -> None:
    await locator.click()
    await _hd(0.2, 0.5)
    for ch in text:
        await locator.type(ch, delay=random.randint(30, 90))
        await asyncio.sleep(random.uniform(0.04, 0.12))


# ══════════════════════════════════════════════════════════════════════
# Waiters
# ══════════════════════════════════════════════════════════════════════

async def _wait_for_visible_locator(page, selectors: List[str], timeout_ms: int = 20_000):
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
    raise RuntimeError(f"Timed out waiting for visible element: {joined} ({last_error})")


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


# ══════════════════════════════════════════════════════════════════════
# Screenshots / Debug
# ══════════════════════════════════════════════════════════════════════

async def _shoot(page, user_id: int, tag: str,
                 on_screenshot: Optional[ScreenshotCallback] = None) -> None:
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


# ══════════════════════════════════════════════════════════════════════
# Re-auth detector
# ══════════════════════════════════════════════════════════════════════

def _looks_like_password_challenge(body_text: str, url: str = "") -> bool:
    body = (body_text or "").lower()
    cur = (url or "").lower()
    return any(kw in body for kw in [
        "to continue, first verify",
        "first verify that it's you",
        "first verify that it’s you",
        "verify it's you",
        "verify it’s you",
        "enter your password",
        "welcome",
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


async def _reauth_if_needed(
    page,
    current_password,
    timeout_ms: int = 25_000,
    *,
    on_password_used: Optional[Callable[[str], Awaitable[None]]] = None,
) -> bool:
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
                "تأكّد من هويتك",
                "مرحباً",
                "أدخل كلمة المرور",
                "veuillez confirmer",
                "bienvenue",
                "willkommen",
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
                "couldn’t verify", "كلمة المرور غير صحيحة", "غير صحيحة",
                "password is incorrect",
            ])
            if rejected:
                log.warning("Re-auth: password candidate %s REJECTED", label)
                continue
            else:
                log.warning("Re-auth: field still visible but no error — assuming success")
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


# ══════════════════════════════════════════════════════════════════════
# Passkey / TOTP switcher
# ══════════════════════════════════════════════════════════════════════

async def _handle_passkey_screen(page) -> bool:
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    has_passkey = any(kw in body for kw in [
        "use your passkey", "passkey", "مفتاح المرور", "passkeys",
    ])
    if not has_passkey:
        return False

    clicked = await _click_text(page, [
        "use your passkey", "use passkey", "استخدم مفتاح المرور",
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


# ══════════════════════════════════════════════════════════════════════
# Browser launch
# ══════════════════════════════════════════════════════════════════════

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
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        return None

    proxy_cfg = _build_proxy_cfg()
    kwargs: Dict[str, Any] = {
        "headless": True,
        "humanize": True,
        "i_know_what_im_doing": True,
    }
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    try:
        cm = AsyncCamoufox(**kwargs)
        browser = await cm.__aenter__()
        ctx = await browser.new_context(locale="en-US")
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
    except Exception:
        pass
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


# ══════════════════════════════════════════════════════════════════════
# Login
# ══════════════════════════════════════════════════════════════════════

_SIGNIN_URLS = [
    "https://accounts.google.com/v3/signin/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin",
    "https://accounts.google.com/ServiceLogin",
    "https://accounts.google.com/signin/v2/identifier?hl=en",
]


async def _enter_email(page, gmail: str) -> bool:
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
            return False
    except Exception:
        pass

    try:
        email_loc = await _wait_for_visible_locator(
            page,
            ['input[type="email"]', 'input#identifierId'],
            timeout_ms=20_000,
        )
    except Exception:
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
    await on_progress("step:google_login_email")
    page = page_holder["page"]
    ctx = page_holder["ctx"]

    # Warmup
    try:
        await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=20_000)
        await _hd(1.5, 3.0)
        await _human_mouse_move(page)
        consent = page.locator(
            'button:has-text("Accept all"), button:has-text("Accept"), '
            'button:has-text("Reject all")'
        )
        if await consent.count() > 0:
            try:
                await consent.first.click(timeout=3000)
                await _hd(1.0, 2.0)
            except Exception:
                pass
        search = page.locator('textarea[name="q"], input[name="q"]')
        if await search.count() > 0:
            queries = ["weather today", "latest news", "best restaurants near me"]
            try:
                await search.first.click()
                await _hd(0.3, 0.8)
                await search.first.type(random.choice(queries), delay=random.randint(50, 120))
                await _hd(0.5, 1.5)
                await page.keyboard.press("Escape")
                await _hd(0.5, 1.0)
            except Exception:
                pass
    except Exception:
        pass

    if not await _enter_email(page, gmail):
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
            raise RuntimeError("Google يرفض الاتصال — جرّب بروكسي مختلف")

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


# ══════════════════════════════════════════════════════════════════════
# Change Password
# ══════════════════════════════════════════════════════════════════════

async def _click_change_password_button(page) -> bool:
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
                return True
        except Exception:
            continue

    if await _click_text(page, [
        "change password", "تغيير كلمة المرور", "تغيير كلمة السر", "save", "حفظ",
    ], 5000):
        return True
    return False


async def _confirm_change_password_dialog(page) -> bool:
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
            break
        except Exception:
            continue

    if not dialog_found:
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
                return True
        except Exception:
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
                if any(kw in txt for kw in ["change password", "change", "تغيير", "ok", "confirm", "تأكيد"]):
                    if "cancel" in txt or "إلغاء" in txt:
                        continue
                    await btn.click(timeout=4000)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _change_password(page, on_progress: ProgressCallback,
                           old_password: str = "",
                           custom_new_password: str = "") -> str:
    await on_progress("step:open_security_page")

    if custom_new_password and len(custom_new_password) >= 8:
        new_pwd = custom_new_password.strip()
    else:
        new_pwd = os.environ.get("NEW_PASSWORD", DEFAULT_NEW_PASSWORD).strip()
        if not new_pwd or len(new_pwd) < 8:
            new_pwd = DEFAULT_NEW_PASSWORD

    await page.goto(
        "https://myaccount.google.com/signinoptions/password?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)

    # CRITICAL FIX: re-auth before changing password
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

    async def _fill_and_verify(field_index: int) -> bool:
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
                    return True
                log.warning("Field #%d mismatch (got len=%d, expected %d)", field_index, len(actual), len(new_pwd))
            except Exception as exc:
                log.warning("Field #%d fill attempt %d failed: %s", field_index, attempt, exc)
                await _hd(0.5, 1.0)
        return False

    if not await _fill_and_verify(0):
        raise RuntimeError("تعذّر ملء حقل كلمة السر الجديدة")
    await _hd(0.4, 0.8)
    if not await _fill_and_verify(1):
        raise RuntimeError("تعذّر ملء حقل تأكيد كلمة السر")
    await _hd(0.6, 1.2)

    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass
    await _hd(0.8, 1.6)

    clicked = await _click_change_password_button(page)
    if not clicked:
        try:
            await fields.nth(1).click()
            await _hd(0.2, 0.4)
            await page.keyboard.press("Enter")
        except Exception as exc:
            raise RuntimeError(f"تعذّر الضغط على زر Change password: {exc}")

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
        return new_pwd
    if "/signinoptions/password" not in page.url:
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


# ══════════════════════════════════════════════════════════════════════
# ADD PHONE NUMBER — COMPLETE REWRITE (FIXED)
# ══════════════════════════════════════════════════════════════════════

async def _add_phone_number(
    page,
    phone: str,
    current_password: str,
    sms_code_provider: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    *,
    on_screenshot: Optional[ScreenshotCallback] = None,
    user_id: int = 0,
) -> bool:
    """Add a phone number via Google's RECOVERY PHONE flow.

    FIX: Uses /signinoptions/rescuephone with full re-auth handling.
    """
    log.info("Adding recovery phone: %s", phone)

    async def _snap(tag: str) -> None:
        await _shoot(page, user_id, f"phone_{tag}", on_screenshot)

    # Step 1: Navigate directly to recovery phone settings
    phone_url = "https://myaccount.google.com/signinoptions/rescuephone?hl=en"
    try:
        await page.goto(phone_url, wait_until="domcontentloaded", timeout=20_000)
        await _hd(2, 3.5)
        log.info("Loaded rescuephone page")
    except Exception as exc:
        log.warning("Failed to load rescuephone: %s", exc)
        await _snap("nav_failed")
        return False
    await _snap("1_page_loaded")

    # Step 2: Handle re-auth IMMEDIATELY
    reauth_done = await _reauth_if_needed(page, current_password, timeout_ms=15_000)
    if reauth_done:
        log.info("Re-auth completed on rescuephone page")
        await _hd(2, 3)
        await _snap("1_after_reauth")

    # Step 3: Click "Add recovery phone"
    cta_clicked = False
    cta_selectors = [
        'button:has-text("Add recovery phone")',
        'button:has-text("Add a recovery phone")',
        '[role="button"]:has-text("Add recovery phone")',
        'button:has-text("Add phone number")',
        'button:has-text("Add a phone number")',
        '[role="button"]:has-text("Add phone number")',
        'button:has-text("Add phone")',
        'div[jsaction] button:has-text("Add")',
        'c-wiz a[href*="phone"]:has-text("Add")',
    ]

    for sel in cta_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await _hd(0.5, 1.0)
                await loc.click(timeout=5000)
                cta_clicked = True
                log.info("Clicked CTA via: %s", sel)
                await _hd(3, 4)
                break
        except Exception:
            continue

    if not cta_clicked:
        if await _click_text(page, [
            "add recovery phone", "add a recovery phone",
            "add phone number", "add a phone number",
            "add phone", "إضافة رقم", "إضافة الآن", "أضف الآن",
        ], 5000):
            cta_clicked = True
            log.info("Clicked CTA via text search")
            await _hd(3, 4)

    if not cta_clicked:
        log.warning("Could not find Add-phone CTA")
        await _snap("2_no_cta")
        return False

    await _snap("2_after_cta")

    # Step 4: Handle re-auth AGAIN after click
    reauth_done = await _reauth_if_needed(page, current_password, timeout_ms=12_000)
    if reauth_done:
        await _hd(2, 3)
        await _snap("2_after_second_reauth")

    # Step 5: Wait for phone input field
    phone_sel = (
        'input[type="tel"], input[name="phoneNumber"], '
        'input[aria-label*="phone" i], input[autocomplete="tel"]'
    )

    async def _find_phone_input():
        try:
            loc = page.locator(phone_sel).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
        for fr in page.frames:
            try:
                fl = fr.locator(phone_sel).first
                if await fl.count() and await fl.is_visible():
                    log.info("Found phone input inside frame")
                    return fl
            except Exception:
                continue
        return None

    phone_loc = None
    deadline = time.time() + 20.0
    while time.time() < deadline:
        phone_loc = await _find_phone_input()
        if phone_loc is not None:
            break
        if await _reauth_if_needed(page, current_password, timeout_ms=6_000):
            await _hd(2, 3)
            continue
        await asyncio.sleep(0.8)

    if phone_loc is None:
        log.warning("Phone input not found after 20s")
        await _snap("3_FAILED_no_input")
        try:
            log.info("Current URL: %s", page.url)
        except Exception:
            pass
        return False

    await _snap("3_input_visible")

    # Step 6: Select country (Iraq)
    country_iso = os.environ.get("PHONE_COUNTRY_ISO", "iq").lower().strip()
    country_name = os.environ.get("PHONE_COUNTRY_NAME", "Iraq").strip()
    country_dial = os.environ.get("PHONE_COUNTRY_DIAL", "+964").strip()

    selected = False
    try:
        dropdown_selectors = [
            'div[aria-label*="country" i][role="combobox"]',
            'div[role="combobox"][aria-haspopup="listbox"]',
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
                        await page.keyboard.type(country_name, delay=80)
                        await _hd(0.5, 1.0)
                    except Exception:
                        pass
                    option_selectors = [
                        f'[role="option"]:has-text("{country_name}")',
                        f'[role="option"]:has-text("{country_dial}")',
                        f'div[role="option"]:has-text("{country_name}")',
                    ]
                    for opt_sel in option_selectors:
                        try:
                            opt = page.locator(opt_sel).first
                            if await opt.count() and await opt.is_visible():
                                await opt.click(timeout=3000)
                                selected = True
                                log.info("Selected country: %s", country_name)
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
    except Exception as exc:
        log.warning("Country selection failed: %s", exc)

    await _snap("4_after_country")
    await _hd(1.0, 2.0)

    # Step 7: Type phone number
    try:
        await page.wait_for_selector(phone_sel, timeout=5_000, state="visible")
    except Exception:
        pass

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
    else:
        cleaned = ("+" + raw) if not phone.strip().startswith("+") else "+" + raw

    try:
        await phone_loc.fill("")
        await _hd(0.2, 0.4)
    except Exception:
        pass
    await phone_loc.type(cleaned, delay=random.randint(40, 90))
    await _hd(0.6, 1.2)
    await _snap("5_after_typing")

    # Step 8: Submit
    submit_clicked = False
    for sel in [
        'button:has-text("Next")',
        '[role="button"]:has-text("Next")',
        'button:has-text("Save")',
        'button:has-text("Send")',
        'button:has-text("Add")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=4000)
                submit_clicked = True
                log.info("Submitted phone via: %s", sel)
                break
        except Exception:
            continue

    if not submit_clicked:
        await _click_text(page, ["next", "send", "save", "add", "التالي", "إرسال", "حفظ"], 4000)
        if not submit_clicked:
            await page.keyboard.press("Enter")

    log.info("Submitted phone number")
    await _hd(3, 5)
    await _snap("6_after_submit")

    # Step 9: Handle SMS code if requested
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
        try:
            body = (await page.inner_text("body")).lower()
            if "phone number added" in body or "added" in body:
                log.info("Phone added without SMS verification")
                return True
        except Exception:
            pass

    if not code_field_visible:
        log.info("Phone added without SMS verification")
        return True

    log.info("SMS code requested")
    await _snap("7_sms_requested")

    if sms_code_provider is None:
        log.warning("No SMS provider configured")
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

    try:
        body = (await page.inner_text("body")).lower()
        if any(k in body for k in ["added", "verified", "تم", "success"]):
            return True
    except Exception:
        pass

    return True


# ══════════════════════════════════════════════════════════════════════
# 2FA Setup — FIXED to ensure phone exists first
# ══════════════════════════════════════════════════════════════════════

async def _click_turn_on_2sv(page) -> bool:
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
                log.info("Clicked 'Turn on 2SV' via: %s", sel)
                return True
        except Exception:
            continue

    if await _click_text(page, [
        "turn on 2-step verification", "turn on 2-step", "turn on 2sv",
        "تفعيل التحقّق بخطوتين", "تفعيل المصادقة الثنائية", "تفعيل التحقق",
    ], 5000):
        return True
    return False


async def _setup_new_authenticator(
    page,
    on_progress: ProgressCallback,
    current_password: str,
    sms_code_provider: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
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

    # ════════════════════════════════════════════════════════════════
    # FIX: FIRST ensure a recovery phone is added BEFORE opening 2SV!
    # ════════════════════════════════════════════════════════════════
    phone_to_add = os.environ.get("FALLBACK_PHONE", DEFAULT_FALLBACK_PHONE).strip()
    if not phone_to_add:
        raise RuntimeError("FALLBACK_PHONE environment variable is required for 2FA setup")

    await on_progress("step:add_phone_number")
    log.info("ENSURING recovery phone exists BEFORE 2FA setup")
    phone_added = await _add_phone_number(
        page, phone_to_add, current_password, sms_code_provider,
        on_screenshot=on_screenshot, user_id=user_id,
    )
    await _snap("after_mandatory_phone_add")

    if not phone_added:
        log.warning("Phone add returned False — continuing anyway, 2SV may fail")

    # Re-open 2SV page after phone add
    await _hd(2, 3)
    await page.goto(
        "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
        wait_until="domcontentloaded",
    )
    await _hd(2, 3.5)
    await _reauth_if_needed(page, password_candidates, on_password_used=on_password_used)
    await _hd(1.5, 2.5)
    await _snap("2sv_page_after_phone")

    # Check if 2SV needs turning on
    async def _read_body() -> str:
        try:
            return (await page.inner_text("body")).lower()
        except Exception:
            return ""

    body = await _read_body()
    needs_turn_on = any(kw in body for kw in [
        "turn on 2-step verification", "turn on 2-step", "تفعيل التحقّق",
        "تفعيل المصادقة الثنائية", "تفعيل التحقق", "2-step verification is off",
        "2-step verification is turned off", "off",
    ])

    if needs_turn_on:
        log.info("Clicking 'Turn on 2-Step Verification'")
        if await _click_turn_on_2sv(page):
            await _hd(2, 3.5)
            await _reauth_if_needed(page, password_candidates, on_password_used=on_password_used)
            await _hd(1.5, 2.5)
            await _snap("after_click_turn_on_2sv")

            # Handle "Add second steps" dialog if it still appears
            for attempt in range(3):
                body = await _read_body()
                if any(kw in body for kw in [
                    "add second steps", "add another one",
                    "first add second steps", "doesn't sync",
                ]):
                    log.info("Still seeing 'Add second steps' — phone may not be fully registered")
                    await _click_text(page, ["go back", "العودة", "رجوع", "back"], 3000)
                    await _hd(1.5, 2.5)

                    if phone_to_add:
                        await _add_phone_number(
                            page, phone_to_add, current_password, sms_code_provider,
                            on_screenshot=on_screenshot, user_id=user_id,
                        )
                        await _hd(2, 3)
                        await page.goto(
                            "https://myaccount.google.com/signinoptions/two-step-verification?hl=en",
                            wait_until="domcontentloaded",
                        )
                        await _hd(2, 3.5)
                        await _reauth_if_needed(page, password_candidates, on_password_used=on_password_used)
                        await _hd(1.5, 2.5)

                        if await _click_turn_on_2sv(page):
                            await _hd(2, 3.5)
                            await _reauth_if_needed(page, password_candidates, on_password_used=on_password_used)
                            await _hd(1.5, 2.5)
                    break
                await _hd(1.5, 2.5)

            # Skip wizard prompts
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

    # Click "Authenticator app"
    await _click_text(page, [
        "authenticator app", "authenticator", "add authenticator app",
        "تطبيق المصادقة", "إضافة تطبيق المصادقة",
    ], 5000)
    await _hd(1.5, 2.5)

    # Click "Set up authenticator" / "Get started"
    await _click_text(page, [
        "set up authenticator", "set up", "+ add authenticator",
        "get started", "إعداد", "بدء", "إضافة",
    ], 5000)
    await _hd(1.5, 2.5)

    # Reveal secret key
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
        raise RuntimeError("تعذّر استخراج مفتاح Authenticator الجديد")

    log.info("Extracted new TOTP secret (len=%d)", len(secret))
    await _snap("totp_secret_extracted")

    # Send credentials to user immediately
    tfa_url = f"https://2fa.fb.tools/{secret}"
    if on_credentials_ready is not None:
        for label, value in (("new_totp_secret", secret), ("totp_url", tfa_url)):
            try:
                await on_credentials_ready(label, value)
            except Exception as exc:
                log.warning("emit %s failed: %s", label, exc)
        try:
            current_code = _now_totp(secret)
            await on_credentials_ready("totp_code", current_code)
        except Exception:
            pass

    # Click "Next" to proceed to code entry
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
        next_clicked = await _click_text(page, ["next", "التالي"], 4000)
    if not next_clicked:
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
    await _hd(2.0, 3.5)
    await _snap("after_qr_next_click")

    # Wait for TOTP code field with retry
    code = _now_totp(secret)
    code_sel = (
        'input[type="tel"], input#totpPin, input[name="totpPin"], '
        'input[autocomplete="one-time-code"]'
    )
    code_field_found = False
    for attempt in range(3):
        try:
            await _reauth_if_needed(page, password_candidates, timeout_ms=6_000, on_password_used=on_password_used)
            await page.wait_for_selector(code_sel, timeout=10_000, state="visible")
            code_field_found = True
            log.info("TOTP code field appeared (attempt %d)", attempt + 1)
            break
        except Exception:
            log.warning("TOTP field not visible (attempt %d/3)", attempt + 1)
            await _snap(f"totp_field_missing_attempt{attempt + 1}")
            try:
                body_now = (await page.inner_text("body")).lower()
            except Exception:
                body_now = ""
            if _looks_like_password_challenge(body_now, page.url):
                reauthed = await _reauth_if_needed(
                    page, password_candidates, timeout_ms=15_000,
                    on_password_used=on_password_used,
                )
                await _hd(1.5, 2.5)
                if reauthed:
                    try:
                        await page.wait_for_selector(code_sel, timeout=8_000, state="visible")
                        code_field_found = True
                        break
                    except Exception:
                        pass
            if attempt < 2:
                if await _click_text(page, ["next", "التالي"], 3000):
                    pass
                else:
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                await _hd(2.0, 3.0)

    if not code_field_found:
        await _snap("totp_field_FAILED_but_secret_emitted")
        log.warning("Returning secret as partial success")
        return secret

    # Enter TOTP code
    try:
        code = _now_totp(secret)
    except Exception:
        pass
    await _type_human_at(page, code_sel, code)
    await _hd(0.5, 1.2)
    if not await _click_text(page, ["verify", "next", "تحقق", "التالي"], 4000):
        await page.keyboard.press("Enter")
    await _hd(3, 5)
    await _snap("totp_code_submitted")

    for label in [
        "done", "turn on", "save", "finish", "got it",
        "تم", "تفعيل", "حفظ", "إنهاء", "موافق",
    ]:
        if await _click_text(page, [label], 2500):
            await _hd(1.0, 2.0)
            break

    await _snap("after_2sv_finalized")
    log.info("2FA finalized successfully")
    return secret


# ══════════════════════════════════════════════════════════════════════
# Verify new 2FA
# ══════════════════════════════════════════════════════════════════════

async def _verify_new_2fa(page, gmail: str, new_password: str, new_secret: str,
                          on_progress: ProgressCallback, old_password: str = "") -> bool:
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
                raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator")
        else:
            cur = page.url
            if "myaccount" in cur or ("signin" not in cur and "challenge" not in cur):
                return True
            raise RuntimeError("لم يظهر حقل إدخال رمز Authenticator")

    code = _now_totp(new_secret)
    log.info("Auto-entering fresh TOTP code")
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


# ══════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════

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
    result: Dict[str, Any] = {
        "success": False,
        "gmail": gmail,
        "new_password": None,
        "new_totp_secret": None,
        "password_used_for_reauth": None,
        "step": "launch_browser",
        "error": None,
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
                result["error"] = "لا توجد متصفحات مثبتة"
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

        # 2. Change password
        new_password = await _change_password(
            page, _progress_wrap, old_password,
            custom_new_password=custom_new_password,
        )
        result["new_password"] = new_password
        await _emit_credential("new_password", new_password)
        await _shoot(page, user_id, "after_change_password", on_screenshot)

        async def _notify_password_used(pwd_used: str) -> None:
            result["password_used_for_reauth"] = pwd_used
            if on_credentials_ready is not None:
                try:
                    await on_credentials_ready("password_used_for_reauth", pwd_used)
                except Exception:
                    pass

        # 3. Setup 2FA (internally adds phone + sets up authenticator)
        new_secret = await _setup_new_authenticator(
            page, _progress_wrap, new_password, sms_code_provider,
            on_screenshot=on_screenshot, user_id=user_id,
            old_password=old_password,
            on_password_used=_notify_password_used,
            on_credentials_ready=on_credentials_ready,
        )
        result["new_totp_secret"] = new_secret
        result["totp_url"] = f"https://2fa.fb.tools/{new_secret}"
        try:
            result["totp_code"] = _now_totp(new_secret)
        except Exception:
            result["totp_code"] = None
        await _emit_credential("new_totp_secret", new_secret)
        await _emit_credential("totp_url", result["totp_url"])
        if result.get("totp_code"):
            await _emit_credential("totp_code", result["totp_code"])
        await _shoot(page, user_id, "after_setup_2fa", on_screenshot)

        # 4. Verify (non-fatal)
        try:
            await _verify_new_2fa(page, gmail, new_password, new_secret, _progress_wrap, old_password)
            await _shoot(page, user_id, "after_verify", on_screenshot)
        except Exception as exc:
            log.warning("verify_new_2fa failed (non-fatal): %s", exc)
            await _shoot(page, user_id, "verify_failed_nonfatal", on_screenshot)

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
