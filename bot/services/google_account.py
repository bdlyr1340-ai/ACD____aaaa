"""Google Account Rotator — Camoufox edition (FIXED v2026).

Public entry point:
    rotate_google_account(on_progress, gmail, old_password, old_totp_secret, user_id)
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
        "first verify it",
        "verify that it's you",
        "enter your password",
        "تحقق من هويتك",
        "أدخل كلمة المرور",
    ]) or any(path in cur for path in [
        "signin/challenge",
        "challenge/pwd",
    ])


async def _write_admin_debug_reports(
    page, *, user_id: int, step: str, error_text: str,
    screenshot_path: Optional[str] = None, html_path: Optional[str] = None,
    old_password: str = "", new_password: str = "", password_used_for_reauth: str = "",
) -> Dict[str, Optional[str]]:
    ts = int(time.time() * 1000)
    base = f"{user_id}_{ts}_{step}"
    problem_path = os.path.join(SHOTS_DIR, f"{base}_problem.txt")
    solution_path = os.path.join(SHOTS_DIR, f"{base}_solution.txt")
    try:
        title = await page.title()
        url = page.url or ""
        body = await page.inner_text("body")
    except Exception:
        title = url = body = ""

    problem_text = (
        f"Google rotation debug report\nstep: {step}\nerror: {error_text}\n"
        f"url: {url}\ntitle: {title}\nold_password: {old_password}\n"
        f"new_password: {new_password}\n"
    )
    out: Dict[str, Optional[str]] = {"problem_txt_path": None, "solution_txt_path": None}
    try:
        with open(problem_path, "w", encoding="utf-8") as f: f.write(problem_text)
        out["problem_txt_path"] = problem_path
    except: pass
    return out


async def _hd(min_s: float = 0.4, max_s: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(min_s * _SPEED, max_s * _SPEED))


async def _human_mouse_move(page) -> None:
    try:
        for _ in range(random.randint(2, 5)):
            x, y = random.randint(100, 900), random.randint(100, 600)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.05, 0.2))
    except: pass


async def _type_human_at(page, selector: str, text: str) -> None:
    await page.click(selector)
    await _hd(0.2, 0.5)
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))


async def _wait_for_visible_locator(page, selectors: List[str], timeout_ms: int = 20_000):
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if await loc.is_visible(): return loc
            except: continue
        await asyncio.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for visible element: {selectors}")


async def _click_text(page, substrings: List[str], timeout_ms: int = 4000) -> bool:
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        for substr in substrings:
            try:
                loc = page.get_by_text(re.compile(substr, re.I)).first
                if await loc.is_visible():
                    await loc.click()
                    return True
            except: pass
        await asyncio.sleep(0.3)
    return False

async def _handle_passkey_screen(page) -> bool:
    clicked = await _click_text(page, ["use your passkey", "استخدم مفتاح المرور"], 3000)
    if clicked:
        await _hd(2, 3)
        await _click_text(page, ["try another way", "طريقة أخرى"], 4000)
        await _hd(1, 2)
        await _click_text(page, ["google authenticator", "تطبيق المصادقة"], 4000)
    return clicked

async def _switch_to_totp_method(page) -> bool:
    if await _handle_passkey_screen(page): return True
    try: body = (await page.inner_text("body")).lower()
    except: body = ""
    if any(kw in body for kw in ["tap yes", "اضغط نعم", "check your phone"]):
        if await _click_text(page, ["try another way", "طريقة أخرى"], 4000):
            await _hd(1, 2)
            return await _click_text(page, ["google authenticator", "تطبيق المصادقة"], 4000)
    return False

# ---------------------------------------------------------------------------
# Browser Engines Logic (Keeping Your Original Engines)
# ---------------------------------------------------------------------------

def _build_proxy_cfg() -> Optional[Dict[str, str]]:
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if not proxy_url: return None
    u = urlparse(proxy_url)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username: cfg["username"] = u.username
    if u.password: cfg["password"] = u.password
    return cfg

async def _launch_camoufox() -> Optional[Tuple[Any, Any, Any, Any]]:
    try: from camoufox.async_api import AsyncCamoufox
    except ImportError: return None
    proxy_cfg = _build_proxy_cfg()
    kwargs = {"headless": True, "humanize": True, "i_know_what_im_doing": True}
    if proxy_cfg: kwargs["proxy"] = proxy_cfg
    try:
        cm = AsyncCamoufox(**kwargs)
        browser = await cm.__aenter__()
        ctx = await browser.new_context()
        await ctx.add_init_script(_EXTRA_STEALTH_JS)
        page = await ctx.new_page()
        async def _cleanup():
            try: await page.close()
            except: pass
            try: await cm.__aexit__(None, None, None)
            except: pass
        return _cleanup, browser, ctx, page
    except: return None

async def _launch_patchright() -> Optional[Tuple[Any, Any, Any, Any]]:
    try: from patchright.async_api import async_playwright as patchright_pw
    except ImportError: return None
    proxy_cfg = _build_proxy_cfg()
    try:
        pw_inst = await patchright_pw().start()
        kwargs = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        if proxy_cfg: kwargs["proxy"] = proxy_cfg
        browser = await pw_inst.chromium.launch(**kwargs)
        ctx = await browser.new_context(user_agent=random.choice(_STEALTH_UAS))
        await ctx.add_init_script(_EXTRA_STEALTH_JS)
        page = await ctx.new_page()
        async def _cleanup():
            try: await page.close()
            except: pass
            try: await browser.close()
            except: pass
            try: await pw_inst.stop()
            except: pass
        return _cleanup, browser, ctx, page
    except: return None

async def _launch_playwright(pw) -> Tuple[Any, Any, Any, Any]:
    proxy_cfg = _build_proxy_cfg()
    kwargs = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy_cfg: kwargs["proxy"] = proxy_cfg
    browser = await pw.chromium.launch(**kwargs)
    ctx = await browser.new_context(user_agent=random.choice(_STEALTH_UAS))
    await ctx.add_init_script(_EXTRA_STEALTH_JS)
    page = await ctx.new_page()
    async def _cleanup():
        try: await page.close()
        except: pass
        try: await browser.close()
        except: pass
    return _cleanup, browser, ctx, page

# ---------------------------------------------------------------------------
# Rotation Flow Logic (FIXED)
# ---------------------------------------------------------------------------

async def _add_2sv_phone_number(page, phone: str, password: str, user_id: int, on_screenshot) -> bool:
    """FIXED: Adds phone number INSIDE 2SV page as requested."""
    log.info("Adding phone number inside 2SV...")
    await _click_text(page, ["Add a phone number", "إضافة رقم هاتف"], 5000)
    await _hd(2, 3)
    await _reauth_if_needed(page, password)
    
    # Fill phone form
    phone_sel = 'input[type="tel"]'
    try:
        await page.wait_for_selector(phone_sel, timeout=5000)
        await page.locator(phone_sel).fill("")
        # Remove +964 for Iraq as it's usually pre-selected or handled by country picker
        clean_num = phone.replace("+964", "").lstrip("0")
        await page.locator(phone_sel).type(clean_num, delay=100)
        await _hd(1, 2)
        await page.keyboard.press("Enter")
        await _hd(4, 5)
        log.info("Phone number submitted.")
        return True
    except:
        log.warning("Could not fill phone number.")
        return False

async def _setup_new_authenticator(
    page, on_progress: ProgressCallback, current_password: str,
    *, user_id: int, old_password: str, on_screenshot: Optional[ScreenshotCallback] = None,
    on_credentials_ready: Optional[CredentialsCallback] = None,
) -> str:
    """FIXED: Full flow matching user screenshots."""
    await on_progress("step:open_2fa_settings")
    await page.goto("https://myaccount.google.com/signinoptions/two-step-verification?hl=en")
    await _hd(2, 3)
    await _reauth_if_needed(page, [current_password, old_password])

    # 1. Add Phone first if requested/needed
    body = (await page.inner_text("body")).lower()
    if "add a phone number" in body or "إضافة رقم هاتف" in body:
        await on_progress("step:add_phone_number")
        await _add_2sv_phone_number(page, DEFAULT_FALLBACK_PHONE, current_password, user_id, on_screenshot)
        # Refresh 2SV page to see updated state
        await page.goto("https://myaccount.google.com/signinoptions/two-step-verification?hl=en")
        await _hd(2, 3)

    # 2. Add Authenticator
    await on_progress("step:enable_new_authenticator")
    await _click_text(page, ["Authenticator", "Add authenticator app", "تطبيق المصادقة"], 5000)
    await _hd(2, 3)
    await _click_text(page, ["Set up authenticator", "إعداد", "بدء"], 5000)
    await _hd(2, 3)
    await _reauth_if_needed(page, current_password)

    # 3. Get Secret via "Can't scan it?"
    await _click_text(page, ["Can't scan it", "لا يمكنك المسح", "Setup without QR"], 5000)
    await _hd(1, 2)
    
    # Extract Secret
    html = await page.content()
    match = re.search(r"\b([A-Z2-7]{4}(?:\s[A-Z2-7]{4}){7,})\b", html.upper())
    if not match:
        raise RuntimeError("تعذّر استخراج مفتاح Authenticator")
    
    secret = match.group(1).replace(" ", "").upper()
    fb_tools_url = f"https://2fa.fb.tools/{secret}"
    current_otp = _now_totp(secret)

    # FIXED: Send IMMEDIATELY as requested
    if on_credentials_ready:
        await on_credentials_ready("new_totp_secret", secret)
        await on_credentials_ready("totp_url", fb_tools_url)
        await on_credentials_ready("totp_code", current_otp)
        log.info("Credentials sent to user: %s", secret)

    # 4. Confirm Code in Google
    await _click_text(page, ["Next", "التالي"], 4000)
    await _hd(1, 2)
    code_sel = 'input[type="tel"], input#totpPin'
    await page.wait_for_selector(code_sel, timeout=10_000)
    await page.locator(code_sel).type(_now_totp(secret), delay=100)
    await page.keyboard.press("Enter")
    await _hd(3, 4)

    # 5. Final Step: Turn On 2SV (The blue button)
    await page.goto("https://myaccount.google.com/signinoptions/two-step-verification?hl=en")
    await _hd(2, 3)
    if await _click_text(page, ["Turn on", "تفعيل"], 5000):
        await _hd(2, 3)
        log.info("2SV Activated Successfully.")
    
    return secret

# ---------------------------------------------------------------------------
# Boilerplate Helpers (Maintaining your original file logic)
# ---------------------------------------------------------------------------

async def _reauth_if_needed(page, password: str | List[str], timeout_ms: int = 15_000):
    pwd_sel = 'input[type="password"]'
    try:
        if await page.locator(pwd_sel).first.is_visible(timeout=5000):
            p = password[0] if isinstance(password, list) else password
            await page.locator(pwd_sel).first.fill(p)
            await page.keyboard.press("Enter")
            await _hd(3, 4)
            return True
    except: pass
    return False

async def _do_login(page_holder, gmail, old_password, old_totp_secret, on_progress):
    page = page_holder["page"]
    await on_progress("جاري تسجيل الدخول...")
    await page.goto("https://accounts.google.com/signin")
    await page.locator('input[type="email"]').fill(gmail)
    await page.keyboard.press("Enter")
    await _hd(2, 3)
    await page.locator('input[type="password"]').fill(old_password)
    await page.keyboard.press("Enter")
    await _hd(3, 4)
    if old_totp_secret:
        try:
            await page.locator('input[type="tel"]').fill(_now_totp(old_totp_secret))
            await page.keyboard.press("Enter")
            await _hd(3, 4)
        except: pass

async def _change_password(page, on_progress, old_password, custom_new_password=""):
    await on_progress("جاري تغيير كلمة السر...")
    new_pwd = custom_new_password or _generate_strong_password()
    await page.goto("https://myaccount.google.com/signinoptions/password?hl=en")
    await _reauth_if_needed(page, old_password)
    fields = page.locator('input[type="password"]')
    await fields.nth(0).fill(new_pwd)
    await fields.nth(1).fill(new_pwd)
    await _hd(1, 2)
    await page.get_by_role("button", name=re.compile(r"Change password|تغيير", re.I)).click()
    await _hd(4, 5)
    return new_pwd

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def rotate_google_account(
    on_progress: ProgressCallback, *, gmail: str, old_password: str,
    old_totp_secret: str, user_id: int, sms_code_provider: Optional[SmsCodeProvider] = None,
    on_screenshot: Optional[ScreenshotCallback] = None,
    on_credentials_ready: Optional[CredentialsCallback] = None,
    custom_new_password: str = "",
) -> Dict[str, Any]:
    
    result = {"success": False, "gmail": gmail, "old_password": old_password}
    cleanup = browser = ctx = page = None
    
    try:
        # Launch using your original engine logic
        launch_res = await _launch_camoufox() or await _launch_patchright()
        if not launch_res:
            import playwright.async_api as pw_api
            pw_cm = pw_api.async_playwright()
            pw = await pw_cm.__aenter__()
            launch_res = await _launch_playwright(pw)
        
        cleanup, browser, ctx, page = launch_res
        page_holder = {"page": page, "ctx": ctx}

        # 1. Login
        await _do_login(page_holder, gmail, old_password, old_totp_secret, on_progress)
        
        # 2. Change Password
        new_password = await _change_password(page, on_progress, old_password, custom_new_password)
        result["new_password"] = new_password
        if on_credentials_ready: await on_credentials_ready("new_password", new_password)

        # 3. Setup 2FA (The Fixed Flow)
        new_secret = await _setup_new_authenticator(
            page, on_progress, new_password, user_id=user_id, 
            old_password=old_password, on_screenshot=on_screenshot, 
            on_credentials_ready=on_credentials_ready
        )
        
        result["new_totp_secret"] = new_secret
        result["success"] = True
        await on_progress("تمت العملية بنجاح! ✅")

    except Exception as exc:
        log.exception("Rotation failed")
        result["error"] = str(exc)
    finally:
        if cleanup: await cleanup()
    
    return result
