"""
Personal Finance Telegram Bot
Tracks: Emergency Fund | Monthly Budget | Investments
Data stored in Google Sheets
"""

import os
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
SHEET_ID       = os.environ["SHEET_ID"]
CREDENTIALS    = os.environ["GOOGLE_CREDENTIALS"]   # JSON string of service account key

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Conversation states ───────────────────────────────────────────────────────
(
    MAIN_MENU,
    # Emergency fund
    EF_MENU, EF_ADD_SAVING,
    # Budget
    BUD_MENU, BUD_CAT, BUD_AMOUNT,
    # Investments
    INV_MENU, INV_WALLET, INV_ASSET, INV_VALUE, INV_VIEW,
) = range(11)

# ── Google Sheets helpers ─────────────────────────────────────────────────────
def get_sheet():
    import json
    creds_dict = json.loads(CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def ensure_sheets(book):
    """Create required tabs if they don't exist yet."""
    existing = [ws.title for ws in book.worksheets()]

    if "EmergencyFund" not in existing:
        ws = book.add_worksheet("EmergencyFund", rows=200, cols=4)
        ws.append_row(["Date", "Amount Saved (₸)", "Running Total (₸)", "Note"])
        ws.append_row(["TARGET", 2988000, "", "6-month emergency fund target"])

    if "Budget" not in existing:
        ws = book.add_worksheet("Budget", rows=200, cols=4)
        ws.append_row(["Date", "Category", "Amount Spent (₸)", "Note"])

    if "Investments" not in existing:
        ws = book.add_worksheet("Investments", rows=200, cols=5)
        ws.append_row(["Date", "Wallet", "Asset", "Value (₸)", "Note"])


# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [["💰 Emergency Fund", "📊 Budget"],
         ["📈 Investments",    "📋 Summary"]],
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
    "Emergency fund saving", "Investments",
]

def budget_cat_keyboard():
    rows = [BUDGET_CATEGORIES[i:i+2] for i in range(0, len(BUDGET_CATEGORIES), 2)]
    rows.append(["⬅️ Back to menu"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ── /start ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        book = get_sheet()
        ensure_sheets(book)
    except Exception as e:
        logger.error(f"Sheet connection error: {e}")
        await update.message.reply_text("⚠️ Could not connect to Google Sheets. Check your config.")
        return MAIN_MENU

    await update.message.reply_text(
        "👋 Welcome to your *Personal Finance Bot*!\n\n"
        "What would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU


# ── MAIN MENU routing ────────────────────────────────────────────────────────
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "💰 Emergency Fund":
        await update.message.reply_text(
            "💰 *Emergency Fund*\n\n"
            "Target: ₸2,988,000 (6 months)\n\n"
            "Send me the amount you saved this month (e.g. `498000`), "
            "or type /view_ef to see your log.",
            parse_mode="Markdown",
            reply_markup=back_keyboard()
        )
        return EF_ADD_SAVING

    elif text == "📊 Budget":
        await update.message.reply_text(
            "📊 *Budget — Log an Expense*\n\nChoose a category:",
            parse_mode="Markdown",
            reply_markup=budget_cat_keyboard()
        )
        return BUD_CAT

    elif text == "📈 Investments":
        await update.message.reply_text(
            "📈 *Investments*\n\n"
            "I'll record a snapshot of one of your holdings.\n\n"
            "Which wallet / account? (e.g. `Kaspi Gold`, `Freedom Finance`, `Personal wallet`)",
            parse_mode="Markdown",
            reply_markup=back_keyboard()
        )
        return INV_WALLET

    elif text == "📋 Summary":
        return await show_summary(update, context)

    else:
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
        amount = int(text.replace(",", "").replace(" ", "").replace("₸", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `498000`", parse_mode="Markdown")
        return EF_ADD_SAVING

    try:
        book  = get_sheet()
        ws    = book.worksheet("EmergencyFund")
        rows  = ws.get_all_values()

        # Running total = sum of all previous Amount Saved rows (skip header + TARGET row)
        prev_total = 0
        for row in rows[2:]:  # skip header and TARGET
            try:
                prev_total += int(str(row[1]).replace(",", "").replace("₸", "").strip() or 0)
            except:
                pass

        running = prev_total + amount
        target  = 2988000
        pct     = min(round(running / target * 100, 1), 100)
        bar     = "🟩" * int(pct // 10) + "⬜" * (10 - int(pct // 10))

        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([date_str, amount, running, ""])

        msg = (
            f"✅ Saved *₸{amount:,}* on {date_str}\n\n"
            f"Running total: *₸{running:,}*\n"
            f"Target: ₸{target:,}\n"
            f"{bar} {pct}%\n\n"
        )
        if running >= target:
            msg += "🎉 *Emergency fund fully funded!*"
        else:
            remaining = target - running
            msg += f"Remaining: ₸{remaining:,}"

        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

    except Exception as e:
        logger.error(f"EF error: {e}")
        await update.message.reply_text("⚠️ Error saving to sheet. Try again.", reply_markup=main_keyboard())

    return MAIN_MENU


# ════════════════════════════════════════════════════════════════════════════
# BUDGET
# ════════════════════════════════════════════════════════════════════════════
async def bud_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    if text not in BUDGET_CATEGORIES:
        await update.message.reply_text("Please choose a category from the list.", reply_markup=budget_cat_keyboard())
        return BUD_CAT

    context.user_data["bud_cat"] = text
    await update.message.reply_text(
        f"*{text}*\n\nHow much did you spend? (e.g. `15000`)",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )
    return BUD_AMOUNT


async def bud_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        amount = int(text.replace(",", "").replace(" ", "").replace("₸", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `15000`", parse_mode="Markdown")
        return BUD_AMOUNT

    cat = context.user_data.get("bud_cat", "Other")

    try:
        book     = get_sheet()
        ws       = book.worksheet("Budget")
        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([date_str, cat, amount, ""])

        # Monthly total for this category
        month    = datetime.now().strftime("%Y-%m")
        rows     = ws.get_all_values()[1:]  # skip header
        cat_total = sum(
            int(str(r[2]).replace(",","").strip() or 0)
            for r in rows
            if r[0].startswith(month) and r[1] == cat
        )

        await update.message.reply_text(
            f"✅ Logged *₸{amount:,}* under *{cat}*\n\n"
            f"Your total for *{cat}* this month: ₸{cat_total:,}",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        logger.error(f"Budget error: {e}")
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
        f"Wallet: *{text}*\n\nWhat asset? (e.g. `USD savings`, `US T-Bills ETF`, `BTC`)",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )
    return INV_ASSET


async def inv_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    context.user_data["inv_asset"] = text
    await update.message.reply_text(
        f"Asset: *{text}*\n\nWhat is the current value in ₸? (e.g. `1500000`)",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )
    return INV_VALUE


async def inv_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⬅️ Back to menu":
        await update.message.reply_text("Main menu:", reply_markup=main_keyboard())
        return MAIN_MENU

    try:
        value = int(text.replace(",", "").replace(" ", "").replace("₸", ""))
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `1500000`", parse_mode="Markdown")
        return INV_VALUE

    wallet = context.user_data.get("inv_wallet", "")
    asset  = context.user_data.get("inv_asset", "")

    try:
        book     = get_sheet()
        ws       = book.worksheet("Investments")
        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([date_str, wallet, asset, value, ""])

        # Find previous snapshot for this asset to show growth
        rows = ws.get_all_values()[1:]  # skip header
        asset_rows = [r for r in rows if r[1] == wallet and r[2] == asset]

        growth_msg = ""
        if len(asset_rows) >= 2:
            prev_val = int(str(asset_rows[-2][3]).replace(",", "").strip() or 0)
            if prev_val > 0:
                change    = value - prev_val
                change_pct = round(change / prev_val * 100, 1)
                arrow     = "📈" if change >= 0 else "📉"
                sign      = "+" if change >= 0 else ""
                growth_msg = f"\n{arrow} vs last snapshot: {sign}₸{change:,} ({sign}{change_pct}%)"

        await update.message.reply_text(
            f"✅ Snapshot saved!\n\n"
            f"*{asset}* @ {wallet}\n"
            f"Value: *₸{value:,}* on {date_str}"
            f"{growth_msg}",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
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

        # Emergency Fund
        try:
            ws   = book.worksheet("EmergencyFund")
            rows = ws.get_all_values()[2:]  # skip header + TARGET
            total = sum(int(str(r[1]).replace(",","").strip() or 0) for r in rows if r[1])
            target = 2988000
            pct  = min(round(total / target * 100, 1), 100)
            bar  = "🟩" * int(pct // 10) + "⬜" * (10 - int(pct // 10))
            lines.append(f"💰 *Emergency Fund*\n{bar} {pct}%\n₸{total:,} / ₸{target:,}\n")
        except:
            lines.append("💰 Emergency Fund: no data\n")

        # Budget this month
        try:
            ws   = book.worksheet("Budget")
            rows = [r for r in ws.get_all_values()[1:] if r[0].startswith(month)]
            if rows:
                by_cat = {}
                for r in rows:
                    cat = r[1]
                    amt = int(str(r[2]).replace(",","").strip() or 0)
                    by_cat[cat] = by_cat.get(cat, 0) + amt
                total_spent = sum(by_cat.values())
                lines.append(f"📊 *Budget — Spent this month*\nTotal: ₸{total_spent:,}\n")
                for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])[:5]:
                    lines.append(f"  • {cat}: ₸{amt:,}")
                lines.append("")
            else:
                lines.append("📊 Budget: no entries this month\n")
        except:
            lines.append("📊 Budget: no data\n")

        # Investments — latest snapshot per asset
        try:
            ws   = book.worksheet("Investments")
            rows = ws.get_all_values()[1:]
            if rows:
                latest = {}
                for r in rows:
                    key = (r[1], r[2])  # wallet + asset
                    latest[key] = int(str(r[3]).replace(",","").strip() or 0)
                total_inv = sum(latest.values())
                lines.append(f"📈 *Investments — Latest values*\nTotal: ₸{total_inv:,}\n")
                for (wallet, asset), val in sorted(latest.items(), key=lambda x: -x[1]):
                    lines.append(f"  • {asset} ({wallet}): ₸{val:,}")
            else:
                lines.append("📈 Investments: no snapshots yet")
        except:
            lines.append("📈 Investments: no data")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    except Exception as e:
        logger.error(f"Summary error: {e}")
        await update.message.reply_text("⚠️ Could not load summary.", reply_markup=main_keyboard())

    return MAIN_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_keyboard())
    return MAIN_MENU


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu)
            ],
            EF_ADD_SAVING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ef_add_saving)
            ],
            BUD_CAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bud_cat)
            ],
            BUD_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bud_amount)
            ],
            INV_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, inv_wallet)
            ],
            INV_ASSET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, inv_asset)
            ],
            INV_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, inv_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("summary", show_summary))

    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
