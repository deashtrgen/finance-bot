"""
Personal Finance Telegram Bot
Tracks: Emergency Fund | Expenses | Income | Investments
Features: Edit/Delete entries | Charts & History | Summary
Data stored in Google Sheets
"""

import os
import io
import logging
from datetime import datetime
from collections import defaultdict
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
import gspread

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — required for servers
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
SHEET_ID    = os.environ.get("SHEET_ID", "")
CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
EF_TARGET   = int(os.environ.get("EF_TARGET", "2988000"))

if not all([BOT_TOKEN, SHEET_ID, CREDENTIALS]):
    raise RuntimeError("Missing required env vars: BOT_TOKEN, SHEET_ID, GOOGLE_CREDENTIALS")

# ── Conversation states ──────────────────────────────────────────────────────
(
    MAIN_MENU,
    EF_ADD_SAVING,
    BUD_CAT, BUD_AMOUNT,
    INC_SOURCE, INC_AMOUNT,
    INV_WALLET, INV_ASSET, INV_VALUE,
    EDIT_PICK_SHEET, EDIT_PICK_ROW, EDIT_PICK_ACTION, EDIT_NEW_AMOUNT,
    CHARTS_PICK,
) = range(14)

# Sheet names
SHEET_EF     = "EmergencyFund"
SHEET_BUDGET = "Budget"
SHEET_INCOME = "Income"
SHEET_INV    = "Investments"

# ── Google Sheets helpers ────────────────────────────────────────────────────
def get_sheet():
    import json
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


# ── Helpers ───────────────────────────────────────────────────────────────────
def esc_md(text) -> str:
    if text is None:
        return ""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def parse_amount(text: str) -> int:
    cleaned = text.replace(",", "").replace(" ", "").replace("₸", "")
    value = int(cleaned)
    if value <= 0:
        raise ValueError("must be positive")
    return value


def cell_int(val) -> int:
    try:
        return int(str(val).replace(",", "").replace("₸", "").strip() or 0)
    except:
        return 0


# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [["💰 Emergency Fund", "💵 Income"],
         ["📊 Expense",        "📈 Investment"],
         ["📋 Summary",         "📉 Charts"],
         ["✏️ Edit entries"]],
        resize_keyboard=True
    )

def back_keyboard():
    return ReplyKeyboardMarkup([["⬅️ Back to menu"]], resize_keyboard=True)

BUDGET_CATEGORIES = [
    "Rent / mortgage", "Utilities", "Internet & phone",
    "Groceries", "Cafes & restaurants",
    "Car / transport", "Taxi / public transport",
    "Subscriptions", "Health / gym", "Clothing & care",
    "Entertainment", "Family / parents", "Miscellaneous",
]

INCOME_SOURCES = [
    "Salary", "Freelance / side income",
    "Bonus", "Investment income",
    "Gift", "Refund",
    "Other",
]

def pick_keyboard(items, back_label="⬅️ Back to menu", cols=2):
    rows = [items[i:i+cols] for i in range(0, len(items), cols)]
    rows.append([back_label])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ════════════════════════════════════════════════════════════════════════════
# /start
# ════════════════════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        book = get_sheet()
        ensure_sheets(book)
    except Exception as e:
        logger.error(f"Sheet connection error: {e}")
        await update.message.reply_text(
            "⚠️ Could not connect to Google Sheets. Check that:\n"
            "• Google Sheets API and Drive API are enabled\n"
            "• The Sheet is shared with the service account email\n"
            "• SHEET_ID and GOOGLE_CREDENTIALS are correct"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Welcome to your *Personal Finance Bot*!\n\n"
        "What would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# MAIN MENU router
# ════════════════════════════════════════════════════════════════════════════
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "💰 Emergency Fund":
        await update.message.reply_text(
            f"💰 *Emergency Fund*\n\nTarget: ₸{EF_TARGET:,}\n\nHow much did you save? (e.g. `498000`)",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return EF_ADD_SAVING

    if text == "📊 Expense":
        await update.message.reply_text(
            "📊 *Log an Expense*\n\nChoose a category:",
            parse_mode="Markdown", reply_markup=pick_keyboard(BUDGET_CATEGORIES)
        )
        return BUD_CAT

    if text == "💵 Income":
        await update.message.reply_text(
            "💵 *Log Income*\n\nWhere did the money come from?",
            parse_mode="Markdown", reply_markup=pick_keyboard(INCOME_SOURCES)
        )
        return INC_SOURCE

    if text == "📈 Investment":
        await update.message.reply_text(
            "📈 *Investment Snapshot*\n\nWhich wallet / account? (e.g. `Kaspi Gold`, `Freedom Finance`)",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return INV_WALLET

    if text == "📋 Summary":
        return await show_summary(update, context)

    if text == "📉 Charts":
        return await charts_menu(update, context)

    if text == "✏️ Edit entries":
        return await edit_menu(update, context)

    await update.message.reply_text("Please use the menu buttons below.", reply_markup=main_keyboard())
    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# EMERGENCY FUND
# ════════════════════════════════════════════════════════════════════════════
async def ef_add_saving(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        amount = parse_amount(text)
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `498000`", parse_mode="Markdown")
        return EF_ADD_SAVING

    try:
        book = get_sheet()
        ws   = book.worksheet(SHEET_EF)
        rows = ws.get_all_values()[1:]

        prev_total = sum(cell_int(r[1]) for r in rows if len(r) > 1)
        running    = prev_total + amount
        pct        = min(round(running / EF_TARGET * 100, 1), 100)
        bar        = "🟩" * int(pct // 10) + "⬜" * (10 - int(pct // 10))

        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([date_str, amount, running, ""])

        msg = (
            f"✅ Saved *₸{amount:,}* on {date_str}\n\n"
            f"Running total: *₸{running:,}*\n"
            f"Target: ₸{EF_TARGET:,}\n"
            f"{bar} {pct}%\n\n"
        )
        if running >= EF_TARGET:
            msg += "🎉 *Emergency fund fully funded!*"
        else:
            msg += f"Remaining: ₸{EF_TARGET - running:,}"

        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception as e:
        logger.error(f"EF error: {e}")
        await update.message.reply_text("⚠️ Error saving. Try again.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# EXPENSES
# ════════════════════════════════════════════════════════════════════════════
async def bud_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    if text not in BUDGET_CATEGORIES:
        await update.message.reply_text(
            "Please choose a category from the list.",
            reply_markup=pick_keyboard(BUDGET_CATEGORIES)
        )
        return BUD_CAT

    context.user_data["bud_cat"] = text
    await update.message.reply_text(
        f"*{esc_md(text)}*\n\nHow much did you spend? (e.g. `15000`)",
        parse_mode="Markdown", reply_markup=back_keyboard()
    )
    return BUD_AMOUNT


async def bud_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        amount = parse_amount(text)
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `15000`", parse_mode="Markdown")
        return BUD_AMOUNT

    cat = context.user_data.get("bud_cat", "Other")

    try:
        book     = get_sheet()
        ws       = book.worksheet(SHEET_BUDGET)
        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([date_str, cat, amount, ""])

        month = datetime.now().strftime("%Y-%m")
        rows  = ws.get_all_values()[1:]
        cat_total = sum(
            cell_int(r[2])
            for r in rows
            if len(r) >= 3 and r[0].startswith(month) and r[1] == cat
        )

        await update.message.reply_text(
            f"✅ Logged *₸{amount:,}* under *{esc_md(cat)}*\n\n"
            f"Your total for *{esc_md(cat)}* this month: ₸{cat_total:,}",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except Exception as e:
        logger.error(f"Budget error: {e}")
        await update.message.reply_text("⚠️ Error saving. Try again.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# INCOME
# ════════════════════════════════════════════════════════════════════════════
async def inc_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    if text not in INCOME_SOURCES:
        await update.message.reply_text(
            "Please choose a source from the list.",
            reply_markup=pick_keyboard(INCOME_SOURCES)
        )
        return INC_SOURCE

    context.user_data["inc_source"] = text
    await update.message.reply_text(
        f"*{esc_md(text)}*\n\nHow much? (e.g. `1500000`)",
        parse_mode="Markdown", reply_markup=back_keyboard()
    )
    return INC_AMOUNT


async def inc_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        amount = parse_amount(text)
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `1500000`", parse_mode="Markdown")
        return INC_AMOUNT

    source = context.user_data.get("inc_source", "Other")

    try:
        book     = get_sheet()
        ws       = book.worksheet(SHEET_INCOME)
        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([date_str, source, amount, ""])

        month = datetime.now().strftime("%Y-%m")
        rows  = ws.get_all_values()[1:]
        month_total = sum(
            cell_int(r[2])
            for r in rows
            if len(r) >= 3 and r[0].startswith(month)
        )

        await update.message.reply_text(
            f"✅ Income logged: *₸{amount:,}* from *{esc_md(source)}*\n\n"
            f"Total income this month: ₸{month_total:,}",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except Exception as e:
        logger.error(f"Income error: {e}")
        await update.message.reply_text("⚠️ Error saving. Try again.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# INVESTMENTS
# ════════════════════════════════════════════════════════════════════════════
async def inv_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    context.user_data["inv_wallet"] = text
    await update.message.reply_text(
        f"Wallet: *{esc_md(text)}*\n\nWhat asset? (e.g. `USD savings`, `US T-Bills ETF`, `BTC`)",
        parse_mode="Markdown", reply_markup=back_keyboard()
    )
    return INV_ASSET


async def inv_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    context.user_data["inv_asset"] = text
    await update.message.reply_text(
        f"Asset: *{esc_md(text)}*\n\nCurrent value in ₸? (e.g. `1500000`)",
        parse_mode="Markdown", reply_markup=back_keyboard()
    )
    return INV_VALUE


async def inv_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        value = parse_amount(text)
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `1500000`", parse_mode="Markdown")
        return INV_VALUE

    wallet = context.user_data.get("inv_wallet", "")
    asset  = context.user_data.get("inv_asset", "")

    try:
        book     = get_sheet()
        ws       = book.worksheet(SHEET_INV)
        date_str = datetime.now().strftime("%Y-%m-%d")

        existing   = ws.get_all_values()[1:]
        asset_rows = [r for r in existing if len(r) >= 4 and r[1] == wallet and r[2] == asset]

        ws.append_row([date_str, wallet, asset, value, ""])

        growth_msg = ""
        if asset_rows:
            prev_val = cell_int(asset_rows[-1][3])
            if prev_val > 0:
                change     = value - prev_val
                change_pct = round(change / prev_val * 100, 1)
                arrow      = "📈" if change >= 0 else "📉"
                sign       = "+" if change >= 0 else ""
                growth_msg = f"\n{arrow} vs last snapshot: {sign}₸{change:,} ({sign}{change_pct}%)"

        await update.message.reply_text(
            f"✅ Snapshot saved!\n\n"
            f"*{esc_md(asset)}* @ {esc_md(wallet)}\n"
            f"Value: *₸{value:,}* on {date_str}"
            f"{growth_msg}",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except Exception as e:
        logger.error(f"Investment error: {e}")
        await update.message.reply_text("⚠️ Error saving. Try again.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        book  = get_sheet()
        month = datetime.now().strftime("%Y-%m")
        lines = [f"📋 *Summary — {datetime.now().strftime('%B %Y')}*\n"]

        # Income this month
        income_total = 0
        try:
            ws   = book.worksheet(SHEET_INCOME)
            rows = [r for r in ws.get_all_values()[1:] if len(r) >= 3 and r[0].startswith(month)]
            income_total = sum(cell_int(r[2]) for r in rows)
            lines.append(f"💵 *Income this month:* ₸{income_total:,}")
        except:
            lines.append("💵 Income: no data")

        # Expenses this month
        expense_total = 0
        try:
            ws   = book.worksheet(SHEET_BUDGET)
            rows = [r for r in ws.get_all_values()[1:] if len(r) >= 3 and r[0].startswith(month)]
            if rows:
                by_cat = defaultdict(int)
                for r in rows:
                    by_cat[r[1]] += cell_int(r[2])
                expense_total = sum(by_cat.values())
                lines.append(f"📊 *Expenses this month:* ₸{expense_total:,}")
                for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])[:5]:
                    lines.append(f"  • {esc_md(cat)}: ₸{amt:,}")
            else:
                lines.append("📊 Expenses: none this month")
        except:
            lines.append("📊 Expenses: no data")

        # Net
        net = income_total - expense_total
        net_emoji = "🟢" if net >= 0 else "🔴"
        lines.append(f"\n{net_emoji} *Net this month:* ₸{net:,}\n")

        # Emergency Fund
        try:
            ws   = book.worksheet(SHEET_EF)
            rows = ws.get_all_values()[1:]
            total = sum(cell_int(r[1]) for r in rows if len(r) > 1)
            pct   = min(round(total / EF_TARGET * 100, 1), 100)
            bar   = "🟩" * int(pct // 10) + "⬜" * (10 - int(pct // 10))
            lines.append(f"💰 *Emergency Fund*\n{bar} {pct}%\n₸{total:,} / ₸{EF_TARGET:,}")
        except:
            lines.append("💰 Emergency Fund: no data")

        # Investments
        try:
            ws   = book.worksheet(SHEET_INV)
            rows = [r for r in ws.get_all_values()[1:] if len(r) >= 4]
            if rows:
                latest = {}
                for r in rows:
                    latest[(r[1], r[2])] = cell_int(r[3])
                total_inv = sum(latest.values())
                lines.append(f"\n📈 *Investments — latest:* ₸{total_inv:,}")
                for (wallet, asset), val in sorted(latest.items(), key=lambda x: -x[1]):
                    lines.append(f"  • {esc_md(asset)} ({esc_md(wallet)}): ₸{val:,}")
            else:
                lines.append("\n📈 Investments: no snapshots")
        except:
            lines.append("\n📈 Investments: no data")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except Exception as e:
        logger.error(f"Summary error: {e}")
        await update.message.reply_text("⚠️ Could not load summary.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# CHARTS
# ════════════════════════════════════════════════════════════════════════════
async def charts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Emergency fund growth",          callback_data="chart:ef")],
        [InlineKeyboardButton("📊 Expenses by category (this month)", callback_data="chart:exp_cat")],
        [InlineKeyboardButton("💵 Income vs Expenses (last 6 months)", callback_data="chart:inc_exp")],
        [InlineKeyboardButton("📈 Investment portfolio over time",    callback_data="chart:inv")],
    ])
    await update.message.reply_text(
        "📉 *Which chart would you like?*",
        parse_mode="Markdown", reply_markup=kb
    )
    return CHARTS_PICK


def fig_to_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=140, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def chart_ef(book):
    ws   = book.worksheet(SHEET_EF)
    rows = [r for r in ws.get_all_values()[1:] if len(r) >= 2 and r[0]]
    if not rows:
        return None

    dates, totals, running = [], [], 0
    for r in rows:
        try:
            d = datetime.strptime(r[0], "%Y-%m-%d")
        except:
            continue
        running += cell_int(r[1])
        dates.append(d)
        totals.append(running)

    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(dates, totals, marker="o", color="#1D9E75", linewidth=2)
    ax.fill_between(dates, totals, alpha=0.15, color="#1D9E75")
    ax.axhline(EF_TARGET, color="#888", linestyle="--", linewidth=1, label=f"Target ₸{EF_TARGET:,}")
    ax.set_title("Emergency Fund Growth", fontsize=14, fontweight="bold")
    ax.set_ylabel("₸")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    return fig_to_bytes(fig)


def chart_expenses_by_category(book):
    ws    = book.worksheet(SHEET_BUDGET)
    month = datetime.now().strftime("%Y-%m")
    rows  = [r for r in ws.get_all_values()[1:] if len(r) >= 3 and r[0].startswith(month)]
    if not rows:
        return None

    by_cat = defaultdict(int)
    for r in rows:
        by_cat[r[1]] += cell_int(r[2])

    cats, vals = zip(*sorted(by_cat.items(), key=lambda x: x[1]))
    fig, ax = plt.subplots(figsize=(8, max(4, len(cats) * 0.5)))
    bars = ax.barh(cats, vals, color="#1D9E75")
    ax.set_title(f"Expenses — {datetime.now().strftime('%B %Y')}", fontsize=14, fontweight="bold")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    for bar, val in zip(bars, vals):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height()/2,
                f" ₸{val:,}", va="center", fontsize=9)
    ax.grid(True, alpha=0.3, axis="x")
    return fig_to_bytes(fig)


def chart_income_vs_expenses(book):
    now = datetime.now()
    months = []
    for i in range(5, -1, -1):
        y = now.year
        m = now.month - i
        while m <= 0:
            m += 12
            y -= 1
        months.append(f"{y:04d}-{m:02d}")

    income_by_m  = {m: 0 for m in months}
    expense_by_m = {m: 0 for m in months}

    try:
        ws = book.worksheet(SHEET_INCOME)
        for r in ws.get_all_values()[1:]:
            if len(r) < 3 or not r[0]:
                continue
            mkey = r[0][:7]
            if mkey in income_by_m:
                income_by_m[mkey] += cell_int(r[2])
    except:
        pass

    try:
        ws = book.worksheet(SHEET_BUDGET)
        for r in ws.get_all_values()[1:]:
            if len(r) < 3 or not r[0]:
                continue
            mkey = r[0][:7]
            if mkey in expense_by_m:
                expense_by_m[mkey] += cell_int(r[2])
    except:
        pass

    if not any(income_by_m.values()) and not any(expense_by_m.values()):
        return None

    labels   = [datetime.strptime(m, "%Y-%m").strftime("%b %y") for m in months]
    incomes  = [income_by_m[m]  for m in months]
    expenses = [expense_by_m[m] for m in months]

    import numpy as np
    x = np.arange(len(labels))
    width = 0.38

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar(x - width/2, incomes,  width, label="Income",   color="#1D9E75")
    ax.bar(x + width/2, expenses, width, label="Expenses", color="#D9534F")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title("Income vs Expenses — last 6 months", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"₸{int(v):,}"))
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")
    return fig_to_bytes(fig)


def chart_investments(book):
    ws   = book.worksheet(SHEET_INV)
    rows = [r for r in ws.get_all_values()[1:] if len(r) >= 4 and r[0]]
    if not rows:
        return None

    series = defaultdict(list)
    for r in rows:
        try:
            d = datetime.strptime(r[0], "%Y-%m-%d")
        except:
            continue
        key = f"{r[2]} ({r[1]})"
        series[key].append((d, cell_int(r[3])))

    if not series:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.5))
    colors = plt.cm.tab10.colors
    for i, (name, pts) in enumerate(series.items()):
        pts.sort(key=lambda p: p[0])
        dates = [p[0] for p in pts]
        vals  = [p[1] for p in pts]
        ax.plot(dates, vals, marker="o", label=name, color=colors[i % len(colors)], linewidth=2)

    ax.set_title("Investment Portfolio Over Time", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₸{int(x):,}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    fig.autofmt_xdate()
    return fig_to_bytes(fig)


async def charts_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]

    try:
        book = get_sheet()
        if   kind == "ef":       img = chart_ef(book)
        elif kind == "exp_cat":  img = chart_expenses_by_category(book)
        elif kind == "inc_exp":  img = chart_income_vs_expenses(book)
        elif kind == "inv":      img = chart_investments(book)
        else:                    img = None

        if img is None:
            await query.message.reply_text(
                "📭 Not enough data yet for this chart.",
                reply_markup=main_keyboard()
            )
        else:
            await query.message.reply_photo(photo=img, reply_markup=main_keyboard())
    except Exception as e:
        logger.error(f"Chart error: {e}")
        await query.message.reply_text("⚠️ Error creating chart.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# EDIT / DELETE ENTRIES
# ════════════════════════════════════════════════════════════════════════════
# (sheet name, display label, 0-based index of the amount column)
EDIT_SHEETS = {
    "ef":  (SHEET_EF,     "💰 Emergency Fund", 1),
    "exp": (SHEET_BUDGET, "📊 Expenses",       2),
    "inc": (SHEET_INCOME, "💵 Income",         2),
    "inv": (SHEET_INV,    "📈 Investments",    3),
}


async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"editpick:{k}")]
        for k, (_, label, _) in EDIT_SHEETS.items()
    ])
    await update.message.reply_text(
        "✏️ *Which log do you want to edit?*",
        parse_mode="Markdown", reply_markup=kb
    )
    return EDIT_PICK_SHEET


async def edit_pick_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]
    if kind not in EDIT_SHEETS:
        await query.message.reply_text("Unknown.", reply_markup=main_keyboard())
        return MAIN_MENU

    sheet_name, label, _ = EDIT_SHEETS[kind]
    context.user_data["edit_kind"] = kind

    try:
        book = get_sheet()
        ws   = book.worksheet(sheet_name)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            await query.message.reply_text(f"No entries in {label} yet.", reply_markup=main_keyboard())
            return MAIN_MENU

        data    = rows[1:]
        recent  = data[-10:][::-1]  # newest first
        buttons = []
        for i, r in enumerate(recent):
            # Sheet rows are 1-indexed; data starts at sheet row 2 (after header)
            sheet_row = len(data) + 1 - i  # the last data row is at (len(data)+1), decreasing as i grows
            date = r[0] if len(r) > 0 else "?"
            if   kind == "ef":  desc = f"{date} — ₸{r[1] if len(r)>1 else '?'}"
            elif kind == "exp": desc = f"{date} — {r[1] if len(r)>1 else '?'}: ₸{r[2] if len(r)>2 else '?'}"
            elif kind == "inc": desc = f"{date} — {r[1] if len(r)>1 else '?'}: ₸{r[2] if len(r)>2 else '?'}"
            elif kind == "inv": desc = f"{date} — {r[2] if len(r)>2 else '?'}/{r[1] if len(r)>1 else '?'}: ₸{r[3] if len(r)>3 else '?'}"
            # Truncate long labels to fit Telegram button limits
            desc = desc[:60]
            buttons.append([InlineKeyboardButton(desc, callback_data=f"editrow:{sheet_row}")])

        buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="editrow:cancel")])
        await query.message.reply_text(
            f"✏️ *Last {len(recent)} entries in {label}*\nTap one to edit/delete:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Edit list error: {e}")
        await query.message.reply_text("⚠️ Error loading entries.", reply_markup=main_keyboard())
        return MAIN_MENU

    return EDIT_PICK_ROW


async def edit_pick_row(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]

    if data == "cancel":
        await query.message.reply_text("Cancelled.", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        row_num = int(data)
    except ValueError:
        await query.message.reply_text("Unknown row.", reply_markup=main_keyboard())
        return MAIN_MENU

    context.user_data["edit_row"] = row_num

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Change amount", callback_data="editact:amount"),
         InlineKeyboardButton("🗑️ Delete",        callback_data="editact:delete")],
        [InlineKeyboardButton("⬅️ Cancel",         callback_data="editact:cancel")],
    ])
    await query.message.reply_text("What would you like to do?", reply_markup=kb)
    return EDIT_PICK_ACTION


async def edit_pick_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    kind    = context.user_data.get("edit_kind")
    row_num = context.user_data.get("edit_row")

    if action == "cancel" or not kind or not row_num:
        await query.message.reply_text("Cancelled.", reply_markup=main_keyboard())
        return MAIN_MENU

    if action == "delete":
        try:
            book = get_sheet()
            sheet_name, label, _ = EDIT_SHEETS[kind]
            ws = book.worksheet(sheet_name)
            ws.delete_rows(row_num)

            if kind == "ef":
                rebuild_ef_running_totals(ws)

            await query.message.reply_text(
                f"🗑️ Entry deleted from {label}.",
                reply_markup=main_keyboard()
            )
        except Exception as e:
            logger.error(f"Delete error: {e}")
            await query.message.reply_text("⚠️ Error deleting.", reply_markup=main_keyboard())
        return MAIN_MENU

    if action == "amount":
        await query.message.reply_text(
            "Enter the new amount (e.g. `15000`):",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return EDIT_NEW_AMOUNT

    await query.message.reply_text("Unknown action.", reply_markup=main_keyboard())
    return MAIN_MENU


async def edit_new_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Cancelled.", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        new_val = parse_amount(text)
    except ValueError:
        await update.message.reply_text("Please enter a valid number.", parse_mode="Markdown")
        return EDIT_NEW_AMOUNT

    kind    = context.user_data.get("edit_kind")
    row_num = context.user_data.get("edit_row")
    if not kind or not row_num:
        await update.message.reply_text("Lost context. Start over.", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        book = get_sheet()
        sheet_name, label, amt_col_idx = EDIT_SHEETS[kind]
        ws = book.worksheet(sheet_name)
        # gspread columns are 1-based
        ws.update_cell(row_num, amt_col_idx + 1, new_val)

        if kind == "ef":
            rebuild_ef_running_totals(ws)

        await update.message.reply_text(
            f"✅ Updated entry in {label} to *₸{new_val:,}*.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except Exception as e:
        logger.error(f"Edit amount error: {e}")
        await update.message.reply_text("⚠️ Error updating.", reply_markup=main_keyboard())

    return MAIN_MENU


def rebuild_ef_running_totals(ws):
    """After editing/deleting EF rows, rebuild the Running Total column C."""
    rows    = ws.get_all_values()
    running = 0
    updates = []
    for i, r in enumerate(rows[1:], start=2):  # data starts at sheet row 2
        running += cell_int(r[1] if len(r) > 1 else 0)
        updates.append({"range": f"C{i}", "values": [[running]]})
    if updates:
        ws.batch_update(updates)


# ════════════════════════════════════════════════════════════════════════════
# FALLBACKS
# ════════════════════════════════════════════════════════════════════════════
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_keyboard())
    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    summary_cmd = CommandHandler("summary", show_summary)
    cancel_cmd  = CommandHandler("cancel", cancel)

    text_handlers = {
        MAIN_MENU:       main_menu,
        EF_ADD_SAVING:   ef_add_saving,
        BUD_CAT:         bud_cat,
        BUD_AMOUNT:      bud_amount,
        INC_SOURCE:      inc_source,
        INC_AMOUNT:      inc_amount,
        INV_WALLET:      inv_wallet,
        INV_ASSET:       inv_asset,
        INV_VALUE:       inv_value,
        EDIT_NEW_AMOUNT: edit_new_amount,
    }

    states = {
        state: [summary_cmd, MessageHandler(filters.TEXT & ~filters.COMMAND, handler)]
        for state, handler in text_handlers.items()
    }

    states[CHARTS_PICK]      = [CallbackQueryHandler(charts_handle,    pattern=r"^chart:")]
    states[EDIT_PICK_SHEET]  = [CallbackQueryHandler(edit_pick_sheet,  pattern=r"^editpick:")]
    states[EDIT_PICK_ROW]    = [CallbackQueryHandler(edit_pick_row,    pattern=r"^editrow:")]
    states[EDIT_PICK_ACTION] = [CallbackQueryHandler(edit_pick_action, pattern=r"^editact:")]

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states=states,
        fallbacks=[cancel_cmd, summary_cmd],
        allow_reentry=True,
    )

    app.add_handler(conv)

    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
