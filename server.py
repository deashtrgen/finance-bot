"""
Personal Finance — Web App Backend
- Serves static files (the web app)
- REST API for all operations
- Telegram Mini App auth verification (HMAC)
- Runs the minimal Telegram bot that opens the Mini App
"""

import os
import io
import json
import hmac
import hashlib
import logging
import asyncio
import secrets
import time
from urllib.parse import parse_qs, unquote
from datetime import datetime
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict

import gspread

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from telegram import Update, WebAppInfo, MenuButtonWebApp, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")   # without @, for the Login Widget
SHEET_ID    = os.environ.get("SHEET_ID", "")
CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
EF_TARGET   = int(os.environ.get("EF_TARGET", "2988000"))
WEBAPP_URL  = os.environ.get("WEBAPP_URL", "")
ALLOWED_USER_IDS = [
    int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
]
PORT = int(os.environ.get("PORT", "8080"))

# ── Session store (in-memory; sessions expire on server restart) ─────────────
# For a single-user app this is fine. For multi-user, use Redis.
SESSIONS: dict[str, dict] = {}   # session_id -> {user_id, first_name, expires_at}
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
SESSION_COOKIE_NAME = "finance_session"

# ── Rate limiter (per IP, sliding window) ─────────────────────────────────────
# Key: (bucket_name, ip)   →   list of recent request timestamps
_RATE_LIMIT: dict[tuple[str, str], list[float]] = {}
_RATE_LIMITS = {
    "login":   (10, 60),    # 10 login attempts per minute
    "write":   (60, 60),    # 60 writes per minute
    "read":    (300, 60),   # 300 reads per minute
}


def _check_rate_limit(bucket: str, ip: str) -> None:
    limit, window = _RATE_LIMITS.get(bucket, (100, 60))
    now = time.time()
    key = (bucket, ip)
    timestamps = _RATE_LIMIT.get(key, [])
    # Drop expired timestamps
    timestamps = [t for t in timestamps if now - t < window]
    if len(timestamps) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests — slow down")
    timestamps.append(now)
    _RATE_LIMIT[key] = timestamps


def _client_ip(request: Request) -> str:
    # Railway sits behind a proxy; trust X-Forwarded-For first IP
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

if not all([BOT_TOKEN, SHEET_ID, CREDENTIALS]):
    raise RuntimeError("Missing required env vars: BOT_TOKEN, SHEET_ID, GOOGLE_CREDENTIALS")

# Sheet names
SHEET_EF       = "EmergencyFund"
SHEET_BUDGET   = "Budget"
SHEET_INCOME   = "Income"
SHEET_INV      = "Investments"
SHEET_ACCOUNTS = "Accounts"

# ── Google Sheets helpers ────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(CREDENTIALS)
    client = gspread.service_account_from_dict(creds_dict)
    return client.open_by_key(SHEET_ID)


def ensure_sheets(book):
    existing = [ws.title for ws in book.worksheets()]
    if SHEET_EF not in existing:
        ws = book.add_worksheet(SHEET_EF, rows=500, cols=4)
        ws.append_row(["Date", "Amount Saved (₸)", "Running Total (₸)", "Note"])
    if SHEET_BUDGET not in existing:
        ws = book.add_worksheet(SHEET_BUDGET, rows=500, cols=4)
        ws.append_row(["Date", "Category", "Amount Spent (₸)", "Note"])
    if SHEET_INCOME not in existing:
        ws = book.add_worksheet(SHEET_INCOME, rows=500, cols=4)
        ws.append_row(["Date", "Source", "Amount (₸)", "Note"])
    if SHEET_INV not in existing:
        ws = book.add_worksheet(SHEET_INV, rows=500, cols=5)
        ws.append_row(["Date", "Wallet", "Asset", "Value (₸)", "Note"])
    if SHEET_ACCOUNTS not in existing:
        ws = book.add_worksheet(SHEET_ACCOUNTS, rows=500, cols=4)
        ws.append_row(["Date", "Account", "Balance (₸)", "Note"])


def cell_int(val) -> int:
    try:
        return int(str(val).replace(",", "").replace("₸", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _parse_sub_note(note: str) -> tuple[str, str]:
    """Parse 'Group | Name' or 'Name' — returns (group, name). Defaults to 'Other'/'Unnamed'."""
    if not note:
        return ("Other", "Unnamed")
    if "|" in note:
        parts = [p.strip() for p in note.split("|", 1)]
        return (parts[0] or "Other", parts[1] or "Unnamed")
    return ("Other", note.strip() or "Unnamed")


# ── Telegram Mini App auth ────────────────────────────────────────────────────
# Docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
def verify_init_data(init_data: str) -> dict:
    """Validate the initData string from Telegram.WebApp.initData and return parsed fields."""
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    try:
        parsed = dict(pair.split("=", 1) for pair in init_data.split("&"))
        received_hash = parsed.pop("hash", "")
        # Build data_check_string: keys alphabetically, each as 'key=value', joined by newline
        data_check_string = "\n".join(
            f"{k}={unquote(v)}" for k, v in sorted(parsed.items())
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed initData")

    # secret_key = HMAC_SHA256(BOT_TOKEN, "WebAppData")
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    # Reject stale initData (> 24h old) to prevent replay attacks
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid auth_date")

    if auth_date == 0 or time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="initData expired — reopen the app")

    # Parse user field (JSON)
    user_field = unquote(parsed.get("user", "{}"))
    try:
        user = json.loads(user_field)
    except json.JSONDecodeError:
        user = {}

    return {"user": user, "auth_date": auth_date}


async def auth(request: Request) -> dict:
    # Rate limit all authenticated requests per IP (generous)
    ip = _client_ip(request)
    # Use 'write' bucket for POST, 'read' for others — cheaper on reads
    bucket = "write" if request.method == "POST" else "read"
    _check_rate_limit(bucket, ip)

    # Option 1: Telegram Mini App — verify initData
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        data = verify_init_data(init_data)
        user = data.get("user", {})
        user_id = user.get("id")
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            raise HTTPException(status_code=403, detail="User not allowed")
        return user

    # Option 2: Browser session cookie
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id and session_id in SESSIONS:
        sess = SESSIONS[session_id]
        if sess["expires_at"] > time.time():
            if ALLOWED_USER_IDS and sess["user_id"] not in ALLOWED_USER_IDS:
                raise HTTPException(status_code=403, detail="User not allowed")
            return {"id": sess["user_id"], "first_name": sess.get("first_name", "")}
        else:
            SESSIONS.pop(session_id, None)

    raise HTTPException(status_code=401, detail="Not authenticated")


# ── Telegram Login Widget verification ───────────────────────────────────────
# Docs: https://core.telegram.org/widgets/login#checking-authorization
def verify_login_widget(data: dict) -> dict:
    """Validate a Telegram Login Widget payload."""
    received_hash = data.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    # Build data_check_string: key=value sorted alphabetically, joined by newline
    data_check_string = "\n".join(
        f"{k}={data[k]}" for k in sorted(data.keys())
    )

    # secret_key = SHA256(BOT_TOKEN)
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Check auth_date freshness (reject if older than 1 day)
    try:
        auth_date = int(data.get("auth_date", "0"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid auth_date")

    if time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Login data too old — please log in again")

    return data


# ── Run blocking gspread calls in a thread pool ──────────────────────────────
async def run_sync(func, *args, **kwargs):
    return await asyncio.get_event_loop().run_in_executor(None, lambda: func(*args, **kwargs))


# ────────────────────────────────────────────────────────────────────────────
# API Models
# ────────────────────────────────────────────────────────────────────────────
class EmergencyFundIn(BaseModel):
    amount: int = Field(gt=0, le=1_000_000_000)

class ExpenseIn(BaseModel):
    category: str = Field(min_length=1, max_length=50)
    amount:   int = Field(gt=0, le=1_000_000_000)
    sub_group: str | None = Field(default=None, max_length=50)
    sub_name:  str | None = Field(default=None, max_length=100)

class IncomeIn(BaseModel):
    source: str = Field(min_length=1, max_length=50)
    amount: int = Field(gt=0, le=1_000_000_000)

class InvestmentIn(BaseModel):
    wallet: str = Field(min_length=1, max_length=80)
    asset:  str = Field(min_length=1, max_length=80)
    value:  int = Field(gt=0, le=1_000_000_000)

class AccountIn(BaseModel):
    account: str = Field(min_length=1, max_length=80)
    balance: int = Field(gt=0, le=1_000_000_000)

class EditIn(BaseModel):
    kind:       str = Field(min_length=1, max_length=10)
    row:        int = Field(ge=2, le=10000)
    new_amount: int = Field(gt=0, le=1_000_000_000)


class DeleteIn(BaseModel):
    kind: str = Field(min_length=1, max_length=10)
    row:  int = Field(ge=2, le=10000)

EDIT_SHEETS = {
    "ef":  (SHEET_EF,       1),  # amount col is index 1 (0-based) → column B
    "exp": (SHEET_BUDGET,   2),
    "inc": (SHEET_INCOME,   2),
    "acc": (SHEET_ACCOUNTS, 2),
    "inv": (SHEET_INV,      3),
}


# ────────────────────────────────────────────────────────────────────────────
# FastAPI + Telegram bot lifecycle
# ────────────────────────────────────────────────────────────────────────────
tg_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    # Ensure sheets exist on startup
    try:
        await run_sync(lambda: ensure_sheets(get_sheet()))
    except Exception as e:
        logger.warning(f"Could not ensure sheets on startup: {e}")

    # Start the minimal Telegram bot in the background
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", bot_start))
    tg_app.add_handler(CommandHandler("app",   bot_start))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Configure the persistent "menu button" that opens the Mini App
    if WEBAPP_URL:
        try:
            await tg_app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Open App", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            logger.info(f"Menu button set to {WEBAPP_URL}")
        except Exception as e:
            logger.warning(f"Could not set menu button: {e}")
    else:
        logger.warning("WEBAPP_URL not set — users won't have an 'Open App' menu button.")

    logger.info("Bot started (polling).")
    try:
        yield
    finally:
        logger.info("Shutting down bot...")
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)

# CORS — restrict to the app's own origin. Telegram Mini App runs on same origin.
_allowed_origins = []
if WEBAPP_URL:
    _allowed_origins.append(WEBAPP_URL.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)


# ── Bot handlers (minimal) ───────────────────────────────────────────────────
async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEBAPP_URL:
        await update.message.reply_text(
            "⚠️ The web app URL is not configured yet. Contact admin."
        )
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💼 Open Finance App", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(
        "👋 Welcome to your *Personal Finance App*!\n\n"
        "Tap the button below to open it.",
        parse_mode="Markdown",
        reply_markup=kb
    )


# ────────────────────────────────────────────────────────────────────────────
# STATIC FILES (the Mini App itself)
# ────────────────────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root(request: Request):
    # If this is a Telegram Mini App (initData is added client-side via JS),
    # serve the app and let JS handle auth.
    # If a browser without session, redirect to /login.
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    has_session = session_id and session_id in SESSIONS and SESSIONS[session_id]["expires_at"] > time.time()

    # Telegram Mini App sends a Telegram-WebApp-Init-Data hint in referrer/navigator,
    # but the simplest signal is whether the URL contains "#tgWebAppData" (Telegram appends this).
    # The HTML itself works both ways — serving index.html always is fine because
    # the app JS checks for initData and redirects to /login if missing and no session.
    return FileResponse("static/index.html")


@app.get("/login")
async def login_page():
    return FileResponse("static/login.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ── Public config (unauthenticated) — tells the login page which bot to use ──
@app.get("/api/config")
async def api_config():
    return {"bot_username": BOT_USERNAME}


# ── Telegram Login Widget callback — browsers only ───────────────────────────
class LoginPayload(BaseModel):
    id: int = Field(gt=0)
    first_name: str | None = Field(default=None, max_length=100)
    last_name:  str | None = Field(default=None, max_length=100)
    username:   str | None = Field(default=None, max_length=50)
    photo_url:  str | None = Field(default=None, max_length=500)
    auth_date:  int
    hash:       str = Field(min_length=64, max_length=64)


@app.post("/api/auth/login")
async def api_login(payload: LoginPayload, request: Request, response: Response):
    _check_rate_limit("login", _client_ip(request))

    # Rebuild dict for verification
    data = payload.model_dump(exclude_none=True)
    data_str = {k: str(v) for k, v in data.items()}
    verify_login_widget(data_str)

    user_id = payload.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        raise HTTPException(status_code=403, detail="User not allowed")

    # Issue a new session
    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = {
        "user_id":    user_id,
        "first_name": payload.first_name or "",
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    }

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=SESSION_TTL_SECONDS,
    )
    return {"ok": True, "user": {"id": user_id, "first_name": payload.first_name}}


@app.post("/api/auth/logout")
async def api_logout(response: Response, session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME)):
    if session:
        SESSIONS.pop(session, None)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}


# ────────────────────────────────────────────────────────────────────────────
# API ENDPOINTS
# ────────────────────────────────────────────────────────────────────────────
@app.get("/api/me")
async def api_me(user: dict = Depends(auth)):
    return {"user": user, "ef_target": EF_TARGET}


@app.get("/api/summary")
async def api_summary(user: dict = Depends(auth)):
    def _load():
        book  = get_sheet()
        month = datetime.now().strftime("%Y-%m")

        # Income
        income_total = 0
        income_rows_month = []
        try:
            ws = book.worksheet(SHEET_INCOME)
            all_rows = ws.get_all_values()[1:]
            for r in all_rows:
                if len(r) >= 3 and r[0].startswith(month):
                    income_rows_month.append(r)
                    income_total += cell_int(r[2])
        except Exception as e:
            logger.warning(f"Summary income read failed: {e}")

        # Expenses
        expense_total = 0
        by_cat = defaultdict(int)
        # Subscriptions breakdown (from Note column: "Group | Name")
        subs_by_group = defaultdict(int)
        subs_by_name  = defaultdict(int)
        subs_total    = 0
        try:
            ws = book.worksheet(SHEET_BUDGET)
            for r in ws.get_all_values()[1:]:
                if len(r) >= 3 and r[0].startswith(month):
                    amt = cell_int(r[2])
                    by_cat[r[1]] += amt
                    expense_total += amt
                    if r[1] == "Subscriptions":
                        subs_total += amt
                        note = r[3] if len(r) >= 4 else ""
                        group, name = _parse_sub_note(note)
                        subs_by_group[group] += amt
                        subs_by_name[name]   += amt
        except Exception as e:
            logger.warning(f"Summary expense read failed: {e}")

        # EF
        ef_total = 0
        try:
            ws = book.worksheet(SHEET_EF)
            ef_total = sum(cell_int(r[1]) for r in ws.get_all_values()[1:] if len(r) > 1)
        except Exception as e:
            logger.warning(f"Summary EF read failed: {e}")

        # Accounts — latest balance per account
        accounts = {}
        try:
            ws = book.worksheet(SHEET_ACCOUNTS)
            for r in ws.get_all_values()[1:]:
                if len(r) >= 3 and r[0]:
                    accounts[r[1]] = cell_int(r[2])
        except Exception as e:
            logger.warning(f"Summary accounts read failed: {e}")
        accounts_total = sum(accounts.values())

        # Investments — latest per (wallet, asset)
        inv = {}
        try:
            ws = book.worksheet(SHEET_INV)
            for r in ws.get_all_values()[1:]:
                if len(r) >= 4 and r[0]:
                    inv[f"{r[2]} ({r[1]})"] = cell_int(r[3])
        except Exception as e:
            logger.warning(f"Summary investments read failed: {e}")
        inv_total = sum(inv.values())

        return {
            "month": datetime.now().strftime("%B %Y"),
            "income_month": income_total,
            "expense_month": expense_total,
            "net_month": income_total - expense_total,
            "expenses_by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
            "subs_total":    subs_total,
            "subs_by_group": dict(sorted(subs_by_group.items(), key=lambda x: -x[1])),
            "subs_by_name":  dict(sorted(subs_by_name.items(),  key=lambda x: -x[1])),
            "ef_total": ef_total,
            "ef_target": EF_TARGET,
            "ef_pct": min(round(ef_total / EF_TARGET * 100, 1), 100) if EF_TARGET else 0,
            "accounts": accounts,
            "accounts_total": accounts_total,
            "investments": inv,
            "investments_total": inv_total,
            "net_worth": ef_total + accounts_total + inv_total,
        }
    return await run_sync(_load)


@app.get("/api/entries/{kind}")
async def api_entries(kind: str, user: dict = Depends(auth)):
    """Return the last 20 entries for the given sheet, newest first."""
    if kind not in EDIT_SHEETS:
        raise HTTPException(400, "unknown kind")

    def _load():
        sheet_name, _ = EDIT_SHEETS[kind]
        ws = get_sheet().worksheet(sheet_name)
        rows = ws.get_all_values()
        data = rows[1:]
        recent = data[-20:][::-1]
        out = []
        for i, r in enumerate(recent):
            sheet_row = len(data) + 1 - i
            out.append({
                "row":    sheet_row,
                "values": r,
            })
        return {"entries": out}
    return await run_sync(_load)


@app.post("/api/ef/add")
async def api_ef_add(payload: EmergencyFundIn, user: dict = Depends(auth)):
    if payload.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    def _save():
        book = get_sheet()
        ws = book.worksheet(SHEET_EF)
        rows = ws.get_all_values()[1:]
        prev_total = sum(cell_int(r[1]) for r in rows if len(r) > 1)
        running = prev_total + payload.amount
        ws.append_row([datetime.now().strftime("%Y-%m-%d"), payload.amount, running, ""])
        return {"saved": payload.amount, "running": running, "target": EF_TARGET}
    return await run_sync(_save)


@app.post("/api/expense/add")
async def api_expense_add(payload: ExpenseIn, user: dict = Depends(auth)):
    if payload.amount <= 0:
        raise HTTPException(400, "amount must be positive")

    note = ""
    if payload.category == "Subscriptions":
        group = (payload.sub_group or "").strip()
        name  = (payload.sub_name  or "").strip()
        if group and name:
            note = f"{group} | {name}"
        elif name:
            note = name
        elif group:
            note = group

    def _save():
        ws = get_sheet().worksheet(SHEET_BUDGET)
        ws.append_row([datetime.now().strftime("%Y-%m-%d"), payload.category, payload.amount, note])
        return {"ok": True}
    return await run_sync(_save)


@app.post("/api/income/add")
async def api_income_add(payload: IncomeIn, user: dict = Depends(auth)):
    if payload.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    def _save():
        ws = get_sheet().worksheet(SHEET_INCOME)
        ws.append_row([datetime.now().strftime("%Y-%m-%d"), payload.source, payload.amount, ""])
        return {"ok": True}
    return await run_sync(_save)


@app.post("/api/investment/add")
async def api_investment_add(payload: InvestmentIn, user: dict = Depends(auth)):
    if payload.value <= 0:
        raise HTTPException(400, "value must be positive")
    def _save():
        book = get_sheet()
        ws = book.worksheet(SHEET_INV)
        existing = ws.get_all_values()[1:]
        asset_rows = [r for r in existing if len(r) >= 4 and r[1] == payload.wallet and r[2] == payload.asset]
        ws.append_row([datetime.now().strftime("%Y-%m-%d"), payload.wallet, payload.asset, payload.value, ""])
        prev = cell_int(asset_rows[-1][3]) if asset_rows else 0
        return {"saved": payload.value, "prev": prev}
    return await run_sync(_save)


@app.post("/api/account/add")
async def api_account_add(payload: AccountIn, user: dict = Depends(auth)):
    if payload.balance <= 0:
        raise HTTPException(400, "balance must be positive")
    def _save():
        book = get_sheet()
        ws = book.worksheet(SHEET_ACCOUNTS)
        existing = ws.get_all_values()[1:]
        account_rows = [r for r in existing if len(r) >= 3 and r[1] == payload.account]
        ws.append_row([datetime.now().strftime("%Y-%m-%d"), payload.account, payload.balance, ""])
        prev = cell_int(account_rows[-1][2]) if account_rows else 0
        return {"saved": payload.balance, "prev": prev}
    return await run_sync(_save)


@app.post("/api/edit")
async def api_edit(payload: EditIn, user: dict = Depends(auth)):
    if payload.kind not in EDIT_SHEETS:
        raise HTTPException(400, "unknown kind")
    if payload.new_amount <= 0:
        raise HTTPException(400, "amount must be positive")
    if payload.row < 2:
        raise HTTPException(400, "row must be ≥ 2 (row 1 is the header)")

    def _save():
        sheet_name, amt_col_idx = EDIT_SHEETS[payload.kind]
        ws = get_sheet().worksheet(sheet_name)
        # Validate row exists
        if payload.row > ws.row_count:
            raise HTTPException(400, "row out of range")
        ws.update_cell(payload.row, amt_col_idx + 1, payload.new_amount)
        if payload.kind == "ef":
            _rebuild_ef_running(ws)
        return {"ok": True}
    return await run_sync(_save)


@app.post("/api/delete")
async def api_delete(payload: DeleteIn, user: dict = Depends(auth)):
    if payload.kind not in EDIT_SHEETS:
        raise HTTPException(400, "unknown kind")
    if payload.row < 2:
        raise HTTPException(400, "row must be ≥ 2 (cannot delete the header)")

    def _save():
        sheet_name, _ = EDIT_SHEETS[payload.kind]
        ws = get_sheet().worksheet(sheet_name)
        if payload.row > ws.row_count:
            raise HTTPException(400, "row out of range")
        ws.delete_rows(payload.row)
        if payload.kind == "ef":
            _rebuild_ef_running(ws)
        return {"ok": True}
    return await run_sync(_save)


def _rebuild_ef_running(ws):
    rows    = ws.get_all_values()
    running = 0
    updates = []
    for i, r in enumerate(rows[1:], start=2):
        running += cell_int(r[1] if len(r) > 1 else 0)
        updates.append({"range": f"C{i}", "values": [[running]]})
    if updates:
        ws.batch_update(updates)


# ────────────────────────────────────────────────────────────────────────────
# CHART ENDPOINTS — return PNG images
# ────────────────────────────────────────────────────────────────────────────
def _fig_png(fig) -> bytes:
    # Dark theme styling
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=140,
                facecolor="#17212B")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _style_ax(ax):
    ax.set_facecolor("#17212B")
    ax.tick_params(colors="#8A99A8")
    for spine in ax.spines.values():
        spine.set_color("#2B3A4A")
    ax.xaxis.label.set_color("#E1E8ED")
    ax.yaxis.label.set_color("#E1E8ED")
    ax.title.set_color("#E1E8ED")
    ax.grid(True, alpha=0.15, color="#8A99A8")


@app.get("/api/charts/{kind}")
async def api_chart(kind: str, user: dict = Depends(auth)):
    def _build():
        book = get_sheet()
        if kind == "ef":
            return _chart_ef(book)
        elif kind == "exp_cat":
            return _chart_expenses(book)
        elif kind == "inc_exp":
            return _chart_inc_exp(book)
        elif kind == "acc":
            return _chart_accounts(book)
        elif kind == "inv":
            return _chart_investments(book)
        else:
            raise HTTPException(400, "unknown chart")

    img = await run_sync(_build)
    if img is None:
        raise HTTPException(404, "not enough data")
    return StreamingResponse(io.BytesIO(img), media_type="image/png")


def _chart_ef(book):
    ws = book.worksheet(SHEET_EF)
    rows = [r for r in ws.get_all_values()[1:] if len(r) >= 2 and r[0]]
    if not rows: return None

    dates, totals, running = [], [], 0
    for r in rows:
        try:
            d = datetime.strptime(r[0], "%Y-%m-%d")
        except ValueError:
            continue
        running += cell_int(r[1])
        dates.append(d); totals.append(running)
    if not dates: return None

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(dates, totals, marker="o", color="#2DAE85", linewidth=2)
    ax.fill_between(dates, totals, alpha=0.2, color="#2DAE85")
    ax.axhline(EF_TARGET, color="#888", linestyle="--", linewidth=1, label=f"Target ₸{EF_TARGET:,}")
    ax.set_title("Emergency Fund Growth", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    _style_ax(ax)
    legend = ax.legend()
    legend.get_frame().set_facecolor("#17212B")
    for t in legend.get_texts(): t.set_color("#E1E8ED")
    fig.autofmt_xdate()
    return _fig_png(fig)


def _chart_expenses(book):
    ws = book.worksheet(SHEET_BUDGET)
    month = datetime.now().strftime("%Y-%m")
    rows = [r for r in ws.get_all_values()[1:] if len(r) >= 3 and r[0].startswith(month)]
    if not rows: return None

    by_cat = defaultdict(int)
    for r in rows:
        by_cat[r[1]] += cell_int(r[2])

    cats, vals = zip(*sorted(by_cat.items(), key=lambda x: x[1]))
    fig, ax = plt.subplots(figsize=(8, max(4, len(cats) * 0.5)))
    bars = ax.barh(cats, vals, color="#2DAE85")
    ax.set_title(f"Expenses — {datetime.now().strftime('%B %Y')}", fontsize=14, fontweight="bold")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    for bar, val in zip(bars, vals):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height()/2,
                f" ₸{val:,}", va="center", fontsize=9, color="#E1E8ED")
    _style_ax(ax)
    return _fig_png(fig)


def _chart_inc_exp(book):
    now = datetime.now()
    months = []
    for i in range(5, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12; y -= 1
        months.append(f"{y:04d}-{m:02d}")

    income_by_m  = {m: 0 for m in months}
    expense_by_m = {m: 0 for m in months}

    try:
        for r in book.worksheet(SHEET_INCOME).get_all_values()[1:]:
            if len(r) >= 3 and r[0]:
                mkey = r[0][:7]
                if mkey in income_by_m:
                    income_by_m[mkey] += cell_int(r[2])
    except Exception: pass
    try:
        for r in book.worksheet(SHEET_BUDGET).get_all_values()[1:]:
            if len(r) >= 3 and r[0]:
                mkey = r[0][:7]
                if mkey in expense_by_m:
                    expense_by_m[mkey] += cell_int(r[2])
    except Exception: pass

    if not any(income_by_m.values()) and not any(expense_by_m.values()):
        return None

    import numpy as np
    labels   = [datetime.strptime(m, "%Y-%m").strftime("%b %y") for m in months]
    incomes  = [income_by_m[m]  for m in months]
    expenses = [expense_by_m[m] for m in months]
    x = np.arange(len(labels))
    w = 0.38

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar(x - w/2, incomes,  w, label="Income",   color="#2DAE85")
    ax.bar(x + w/2, expenses, w, label="Expenses", color="#E74C3C")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title("Income vs Expenses — last 6 months", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"₸{int(v):,}"))
    _style_ax(ax)
    legend = ax.legend()
    legend.get_frame().set_facecolor("#17212B")
    for t in legend.get_texts(): t.set_color("#E1E8ED")
    return _fig_png(fig)


def _chart_accounts(book):
    ws = book.worksheet(SHEET_ACCOUNTS)
    rows = [r for r in ws.get_all_values()[1:] if len(r) >= 3 and r[0]]
    if not rows: return None

    series = defaultdict(list)
    for r in rows:
        try:
            d = datetime.strptime(r[0], "%Y-%m-%d")
        except ValueError:
            continue
        series[r[1]].append((d, cell_int(r[2])))
    if not series: return None

    fig, ax = plt.subplots(figsize=(9, 4.5))
    colors = ["#2DAE85", "#3498DB", "#E67E22", "#9B59B6", "#F1C40F", "#E74C3C", "#1ABC9C", "#34495E"]
    for i, (name, pts) in enumerate(series.items()):
        pts.sort(key=lambda p: p[0])
        ax.plot([p[0] for p in pts], [p[1] for p in pts],
                marker="o", label=name, color=colors[i % len(colors)], linewidth=2)
    ax.set_title("Account Balances Over Time", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    _style_ax(ax)
    legend = ax.legend(loc="best", fontsize=9)
    legend.get_frame().set_facecolor("#17212B")
    for t in legend.get_texts(): t.set_color("#E1E8ED")
    fig.autofmt_xdate()
    return _fig_png(fig)


def _chart_investments(book):
    ws = book.worksheet(SHEET_INV)
    rows = [r for r in ws.get_all_values()[1:] if len(r) >= 4 and r[0]]
    if not rows: return None

    series = defaultdict(list)
    for r in rows:
        try:
            d = datetime.strptime(r[0], "%Y-%m-%d")
        except ValueError:
            continue
        series[f"{r[2]} ({r[1]})"].append((d, cell_int(r[3])))
    if not series: return None

    fig, ax = plt.subplots(figsize=(9, 4.5))
    colors = ["#2DAE85", "#3498DB", "#E67E22", "#9B59B6", "#F1C40F", "#E74C3C", "#1ABC9C", "#34495E"]
    for i, (name, pts) in enumerate(series.items()):
        pts.sort(key=lambda p: p[0])
        ax.plot([p[0] for p in pts], [p[1] for p in pts],
                marker="o", label=name, color=colors[i % len(colors)], linewidth=2)
    ax.set_title("Investment Portfolio Over Time", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    _style_ax(ax)
    legend = ax.legend(loc="best", fontsize=9)
    legend.get_frame().set_facecolor("#17212B")
    for t in legend.get_texts(): t.set_color("#E1E8ED")
    fig.autofmt_xdate()
    return _fig_png(fig)


# ────────────────────────────────────────────────────────────────────────────
# MAIN (uvicorn)
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
