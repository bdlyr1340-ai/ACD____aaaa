"""Gmail account creator — Pro v13.

Automates https://accounts.google.com/signup using Playwright (Camoufox/
Chromium with stealth, same engine as `google_account.py`).

Public API (matches what `bot/handlers/create_gmail.py` expects):

    async def create_gmail_account(
        *,
        on_progress,                # async (str) -> None
        human_input_provider,       # async (str) -> str        (CAPTCHA / SMS)
        first_name: str,
        last_name: str,
        birthday: str,              # "YYYY-MM-DD"
        gender: str,                # "m" | "f"
        desired_password: str,
        user_id: int,
    ) -> dict:
        # returns {success, email, password, error, step}

The function is intentionally tolerant: Google A/B-tests its signup form
constantly, so every selector has 2-3 fallbacks and every step has a
retry. When Google asks for a phone number, SMS code, or shows reCAPTCHA,
the bot calls `human_input_provider(prompt)` and waits up to 5 minutes for
the user to reply — exactly like the rotation flow.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

ProgressCB = Callable[[str], Awaitable[None]]
HumanCB = Callable[[str], Awaitable[str]]

SIGNUP_URL = "https://accounts.google.com/signup/v2/createaccount?flowName=GlifWebSignIn&flowEntry=SignUp"

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _candidate_usernames(first: str, last: str) -> list[str]:
    f, l = _slug(first), _slug(last)
    base = [
        f + l,
        f + "." + l,
        f + l + str(random.randint(10, 99)),
        f + "." + l + str(random.randint(100, 999)),
        f[0] + l + str(random.randint(100, 9999)),
        f + str(random.randint(1000, 9999)),
        f + l + str(random.randint(10000, 99999)),
    ]
    # de-duplicate, keep order
    seen: set[str] = set()
    out: list[str] = []
    for b in base:
        if b and b not in seen:
            seen.add(b); out.append(b)
    return out


async def _safe_fill(page, selectors: list[str], value: str, timeout: int = 8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill("")
            await loc.type(value, delay=random.randint(40, 110))
            return True
        except Exception:
            continue
    return False


async def _safe_click(page, selectors: list[str], timeout: int = 8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(delay=random.randint(20, 60))
            return True
        except Exception:
            continue
    return False


async def _click_next(page) -> bool:
    return await _safe_click(page, [
        "button:has-text('Next')",
        "button:has-text('التالي')",
        "div[role='button']:has-text('Next')",
        "#collectNameNext button",
        "#birthdaygenderNext button",
        "#accountDetailsNext button",
        "#createpasswordNext button",
    ])


# ════════════════════════════════════════════════════════════════════
# Browser bootstrap (mirrors google_account.py logic, tolerant fallbacks)
# ════════════════════════════════════════════════════════════════════

async def _open_browser():
    """Returns (playwright, browser, context, page). Uses Camoufox if
    available, otherwise stealth Chromium."""
    # Try Camoufox first (best fingerprint)
    try:
        from camoufox.async_api import AsyncCamoufox
        cam = AsyncCamoufox(headless=True, humanize=True)
        browser = await cam.__aenter__()
        ctx = await browser.new_context(locale="en-US",
                                        viewport={"width": 1280, "height": 820})
        page = await ctx.new_page()
        return ("camoufox", cam, browser, ctx, page)
    except Exception as e:
        log.warning("Camoufox unavailable (%s) — falling back to Chromium", e)

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    ctx = await browser.new_context(
        locale="en-US",
        viewport={"width": 1280, "height": 820},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    # Light stealth
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    page = await ctx.new_page()
    return ("chromium", pw, browser, ctx, page)


async def _close_browser(kind, owner, browser, ctx, page):
    try: await page.close()
    except Exception: pass
    try: await ctx.close()
    except Exception: pass
    try: await browser.close()
    except Exception: pass
    try:
        if kind == "camoufox":
            await owner.__aexit__(None, None, None)
        else:
            await owner.stop()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Main entry
# ════════════════════════════════════════════════════════════════════

async def create_gmail_account(
    *,
    on_progress: ProgressCB,
    human_input_provider: HumanCB,
    first_name: str,
    last_name: str,
    birthday: str,
    gender: str,
    desired_password: str,
    user_id: int,
) -> dict:
    step = "launch_browser"
    kind = owner = browser = ctx = page = None
    try:
        await on_progress("🚀 تشغيل المتصفح…")
        kind, owner, browser, ctx, page = await _open_browser()

        # ── Birthday parsing ───────────────────────────────────────
        try:
            yyyy, mm, dd = birthday.split("-")
            mm_i = int(mm); dd_i = int(dd); yyyy_i = int(yyyy)
        except Exception:
            return {"success": False, "step": "parse_birthday",
                    "error": f"تاريخ ميلاد غير صالح: {birthday}",
                    "email": None, "password": None}

        # ── 1. Open signup ─────────────────────────────────────────
        step = "open_signup"
        await on_progress("🌐 فتح صفحة التسجيل…")
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)

        # ── 2. Names ───────────────────────────────────────────────
        step = "fill_names"
        await on_progress(f"✍️ إدخال الاسم: {first_name} {last_name}")
        ok1 = await _safe_fill(page, ["input[name='firstName']", "#firstName"], first_name)
        ok2 = await _safe_fill(page, ["input[name='lastName']", "#lastName"], last_name)
        if not (ok1 and ok2):
            raise RuntimeError("لم يتم العثور على حقول الاسم")
        await _click_next(page)
        await page.wait_for_timeout(2000)

        # ── 3. Birthday + gender ───────────────────────────────────
        step = "birthday_gender"
        await on_progress("📅 إدخال تاريخ الميلاد والجنس…")
        # Some variants use selects, others native inputs.
        try:
            await page.locator("#month").select_option(value=str(mm_i), timeout=4000)
        except Exception:
            try:
                await page.locator("select[id*='month']").first.select_option(label=MONTHS[mm_i - 1], timeout=4000)
            except Exception:
                pass
        await _safe_fill(page, ["input[name='day']", "#day"], str(dd_i))
        await _safe_fill(page, ["input[name='year']", "#year"], str(yyyy_i))
        gender_value = "1" if gender == "m" else "2"
        try:
            await page.locator("#gender").select_option(value=gender_value, timeout=4000)
        except Exception:
            try:
                lbl = "Male" if gender == "m" else "Female"
                await page.locator("select[id*='gender']").first.select_option(label=lbl, timeout=4000)
            except Exception:
                pass
        await _click_next(page)
        await page.wait_for_timeout(2500)

        # ── 4. Username (with retries on "taken") ──────────────────
        step = "username"
        chosen_username: Optional[str] = None
        for cand in _candidate_usernames(first_name, last_name):
            await on_progress(f"🔎 تجربة اسم المستخدم: {cand}")
            # Some flows show a radio "Create your own Gmail address" first
            try:
                await page.locator("div[role='radio']:has-text('Create your own')").first.click(timeout=2500)
                await page.wait_for_timeout(600)
            except Exception:
                pass

            ok = await _safe_fill(page, [
                "input[name='Username']", "input[name='username']",
                "input[aria-label*='username']", "#username",
            ], cand, timeout=5000)
            if not ok:
                continue
            await _click_next(page)
            await page.wait_for_timeout(2200)

            # Check for "that username is taken" error
            taken = False
            for sel in [
                "div:has-text('That username is taken')",
                "div:has-text('username is taken')",
                "div:has-text('غير متاح')",
            ]:
                try:
                    if await page.locator(sel).first.is_visible(timeout=800):
                        taken = True; break
                except Exception:
                    continue
            if not taken:
                chosen_username = cand
                break

        if not chosen_username:
            raise RuntimeError("تعذّر إيجاد اسم مستخدم متاح")

        email = f"{chosen_username}@gmail.com"
        await on_progress(f"✅ اسم المستخدم متاح: {email}")

        # ── 5. Password ────────────────────────────────────────────
        step = "password"
        await on_progress("🔑 إدخال كلمة السر…")
        await _safe_fill(page, [
            "input[name='Passwd']", "input[name='Password']",
            "input[type='password'][aria-label*='Password']",
        ], desired_password)
        await _safe_fill(page, [
            "input[name='ConfirmPasswd']", "input[name='PasswdAgain']",
            "input[name='confirm-passwd']",
        ], desired_password)
        await _click_next(page)
        await page.wait_for_timeout(2500)

        # ── 6. Phone / SMS / CAPTCHA loops (human-in-the-loop) ─────
        step = "phone_or_captcha"
        for attempt in range(6):
            # Phone-number prompt
            phone_input = None
            for sel in ["input[type='tel']", "#phoneNumberId",
                        "input[name='phoneNumber']"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1500):
                        phone_input = loc; break
                except Exception:
                    continue
            if phone_input:
                await on_progress("📱 يطلب Google رقم هاتف…")
                phone = await human_input_provider(
                    "أدخل رقم هاتف يستقبل SMS (بصيغة دولية مثل +9647xxxxxxxxx):"
                )
                phone = (phone or "").strip()
                if not phone:
                    raise RuntimeError("لم يُرسل رقم الهاتف")
                await phone_input.fill("")
                await phone_input.type(phone, delay=70)
                await _click_next(page)
                await page.wait_for_timeout(3500)

                # Now expect SMS code
                code_input = None
                for sel in ["input[name='code']", "input[id*='code']",
                            "input[aria-label*='code']"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=8000):
                            code_input = loc; break
                    except Exception:
                        continue
                if code_input:
                    await on_progress("📩 إدخال كود SMS…")
                    code = await human_input_provider(
                        "أدخل كود التحقق المُرسَل عبر SMS (6 أرقام):"
                    )
                    code = (code or "").strip()
                    if not code:
                        raise RuntimeError("لم يُرسل كود SMS")
                    await code_input.fill(code)
                    await _click_next(page)
                    await page.wait_for_timeout(3500)
                continue

            # reCAPTCHA / "I'm not a robot"
            captcha = False
            for sel in ["iframe[src*='recaptcha']",
                        "div:has-text(\"I'm not a robot\")",
                        "img[alt*='captcha']", "img[alt*='Captcha']"]:
                try:
                    if await page.locator(sel).first.is_visible(timeout=800):
                        captcha = True; break
                except Exception:
                    continue
            if captcha:
                await on_progress("🧩 ظهر CAPTCHA — يحتاج تدخّل بشري")
                # If it's an image captcha we can OCR-relay; otherwise just wait
                ans = await human_input_provider(
                    "ظهر CAPTCHA. اكتب الحل النصي إن كان نصياً، "
                    "أو اكتب `done` بعد حلّه يدوياً (إن أمكن)."
                )
                ans = (ans or "").strip()
                # Try to type into a visible captcha-answer field
                for sel in ["input[name='captcha']", "input[aria-label*='captcha']"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=1000):
                            await loc.fill(ans)
                            await _click_next(page)
                            break
                    except Exception:
                        continue
                await page.wait_for_timeout(3500)
                continue

            # No phone, no captcha — assume we moved on
            break

        # ── 7. "Confirm you're not a robot" / Review / ToS ─────────
        step = "review_terms"
        await on_progress("📜 الموافقة على الشروط…")
        # Skip "Add phone for account security?"
        await _safe_click(page, [
            "button:has-text('Skip')", "button:has-text('تخطي')",
            "div[role='button']:has-text('Skip')",
        ], timeout=3500)
        await page.wait_for_timeout(1500)

        # Review screen → Next
        await _click_next(page)
        await page.wait_for_timeout(1500)

        # Terms: scroll then accept
        for _ in range(6):
            try:
                await page.mouse.wheel(0, 600)
                await page.wait_for_timeout(400)
            except Exception:
                break
        await _safe_click(page, [
            "button:has-text('I agree')", "button:has-text('Agree')",
            "button:has-text('أوافق')",
            "div[role='button']:has-text('I agree')",
        ], timeout=8000)
        await page.wait_for_timeout(5000)

        # ── 8. Verify success ──────────────────────────────────────
        step = "verify_success"
        await on_progress("🔎 التحقق من نجاح الإنشاء…")
        try:
            await page.wait_for_url(
                re.compile(r"(myaccount|welcome|mail)\.google\.com"),
                timeout=20000,
            )
        except Exception:
            # Sometimes lands on a "Welcome <Name>" page on accounts.google.com
            url = page.url or ""
            if "signup" in url:
                # Best-effort: grab any visible error
                err_text = ""
                try:
                    err_text = await page.locator("body").inner_text(timeout=2000)
                    err_text = err_text[:300]
                except Exception:
                    pass
                raise RuntimeError(f"لم تكتمل عملية الإنشاء. آخر URL: {url}\n{err_text}")

        return {
            "success": True,
            "email": email,
            "password": desired_password,
            "step": "done",
            "error": None,
        }

    except Exception as exc:
        log.exception("create_gmail_account failed at step=%s", step)
        return {
            "success": False,
            "email": None,
            "password": None,
            "step": step,
            "error": str(exc),
        }
    finally:
        if browser is not None:
            await _close_browser(kind, owner, browser, ctx, page)
