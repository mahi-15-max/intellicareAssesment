# -*- coding: utf-8 -*-
"""
IntelliCare — St. Urael Clinic Internal Implementation Assessment Bot
Bilingual (English / አማርኛ) Telegram bot. Python 3.10+, python-telegram-bot 21.x
"""
import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from Content import QUESTIONS, TOTAL, UI

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("assessment-bot")

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "assessment.db")
MAX_CHARS = 3500
HTML = ParseMode.HTML

# ───────────────────────────── Database ─────────────────────────────
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        uid INTEGER PRIMARY KEY,
        lang TEXT DEFAULT 'en',
        display_name TEXT,
        anonymous INTEGER DEFAULT 0,
        cur INTEGER DEFAULT 1,
        status TEXT DEFAULT 'new',          -- new | draft | submitted
        started_at TEXT,
        submitted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS answers (
        uid INTEGER, q INTEGER, text TEXT, na INTEGER DEFAULT 0, updated_at TEXT,
        PRIMARY KEY (uid, q)
    );
    """
)
conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_user(uid):
    return conn.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()


def ensure_user(update: Update):
    u = update.effective_user
    if not get_user(u.id):
        name = u.full_name + (f" (@{u.username})" if u.username else "")
        conn.execute("INSERT INTO users (uid, display_name) VALUES (?, ?)", (u.id, name))
        conn.commit()
    return get_user(u.id)


def set_user(uid, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE users SET {cols} WHERE uid=?", (*fields.values(), uid))
    conn.commit()


def get_answers(uid) -> dict:
    rows = conn.execute("SELECT * FROM answers WHERE uid=?", (uid,)).fetchall()
    return {r["q"]: r for r in rows}


def save_answer(uid, q, text=None, na=False):
    conn.execute(
        "INSERT INTO answers (uid, q, text, na, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(uid, q) DO UPDATE SET text=excluded.text, na=excluded.na, updated_at=excluded.updated_at",
        (uid, q, text, 1 if na else 0, now()),
    )
    conn.commit()


def is_resolved(a) -> bool:
    return a is not None and (a["na"] == 1 or bool(a["text"]))


# ───────────────────────────── UI helpers ─────────────────────────────
def L(user) -> str:
    return user["lang"] if user and user["lang"] in UI else "en"


def progress_bar(done: int) -> str:
    filled = round(10 * done / TOTAL)
    return "▰" * filled + "▱" * (10 - filled) + f"  {done}/{TOTAL}"


def lang_keyboard():
    return Markup([[Btn("🇬🇧 English", callback_data="lang:en"), Btn("🇪🇹 አማርኛ", callback_data="lang:am")]])


def welcome_keyboard(user):
    t = UI[L(user)]
    anon = user["anonymous"] == 1
    return Markup(
        [
            [Btn(t["start"], callback_data="q:1")],
            [Btn(t["anon_on"] if not anon else t["anon_off"], callback_data="anon")],
            [Btn(t["lang_btn"], callback_data="togglelang:welcome")],
        ]
    )


def welcome_text(user) -> str:
    t = UI[L(user)]
    mode = t["mode_anon"] if user["anonymous"] else t["mode_named"]
    return t["welcome"].format(n=TOTAL) + "\n\n" + t["anon_hint"].format(mode=mode)


def question_text(user, n, answers) -> str:
    lang = L(user)
    t, q = UI[lang], QUESTIONS[n - 1]
    done = sum(1 for a in answers.values() if is_resolved(a))
    txt = (
        f"{progress_bar(done)}\n\n"
        f"<b>{t['q_head'].format(i=n, n=TOTAL)}</b>\n"
        f"🔹 <b>{q[lang + '_title']}</b>\n\n"
        f"{q[lang]}\n\n"
        f"{t['q_hint']}"
    )
    a = answers.get(n)
    if a is not None and a["na"]:
        txt += f"\n\n{t['marked_na']}"
    elif a is not None and a["text"]:
        txt += "\n\n" + t["answered_note"].format(chars=len(a["text"]))
    return txt


def question_keyboard(user, n):
    t = UI[L(user)]
    nav = []
    if n > 1:
        nav.append(Btn(t["back"], callback_data=f"q:{n - 1}"))
    nav.append(Btn(t["skip"] if n < TOTAL else t["next"], callback_data=f"q:{n + 1}" if n < TOTAL else "review"))
    return Markup(
        [
            [Btn(t["na"], callback_data=f"na:{n}")],
            nav,
            [Btn(t["lang_btn"], callback_data=f"togglelang:{n}"), Btn(t["exit"], callback_data="exit")],
        ]
    )


def review_text(user, answers) -> str:
    lang = L(user)
    t = UI[lang]
    lines = [t["review_title"], ""]
    for i, q in enumerate(QUESTIONS, 1):
        a = answers.get(i)
        icon = t["status_na"] if (a is not None and a["na"]) else t["status_done"] if is_resolved(a) else t["status_pending"]
        lines.append(f"{icon}  {i}. {q[lang + '_title']}")
    done = sum(1 for a in answers.values() if is_resolved(a))
    lines += ["", progress_bar(done), t["legend"], "", f"<i>{t['review_hint']}</i>"]
    return "\n".join(lines)


def review_keyboard(user, answers):
    t = UI[L(user)]
    nums = []
    for i in range(1, TOTAL + 1):
        a = answers.get(i)
        icon = "⊘" if (a is not None and a["na"]) else "✅" if is_resolved(a) else "⬜"
        nums.append(Btn(f"{icon}{i}", callback_data=f"e:{i}"))
    rows = [nums[i : i + 5] for i in range(0, len(nums), 5)]
    rows.append([Btn(t["submit"], callback_data="submit")])
    rows.append([Btn(t["lang_btn"], callback_data="togglelang:review"), Btn(t["exit"], callback_data="exit")])
    return Markup(rows)


async def render(update: Update, text: str, markup, edit: bool):
    """Edit the existing message when coming from a button, otherwise send a new one."""
    q = update.callback_query
    if edit and q:
        try:
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await update.effective_chat.send_message(text, parse_mode=HTML, reply_markup=markup)


async def show_welcome(update, user, edit=False):
    await render(update, welcome_text(user), welcome_keyboard(user), edit)


async def show_question(update, uid, n, edit=True, prefix=""):
    user = get_user(uid)
    answers = get_answers(uid)
    set_user(uid, cur=n, status="draft", started_at=user["started_at"] or now())
    text = (prefix + "\n\n" if prefix else "") + question_text(user, n, answers)
    await render(update, text, question_keyboard(user, n), edit)


async def show_review(update, uid, edit=True):
    user = get_user(uid)
    answers = get_answers(uid)
    set_user(uid, cur=0)
    await render(update, review_text(user, answers), review_keyboard(user, answers), edit)


# ───────────────────────────── Commands ─────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    context.user_data.pop("edit_mode", None)
    if user["status"] == "submitted":
        await update.message.reply_text(UI[L(user)]["already"])
        return
    answers = get_answers(user["uid"])
    done = sum(1 for a in answers.values() if is_resolved(a))
    if user["status"] == "draft" and done > 0:
        t = UI[L(user)]
        kb = Markup(
            [
                [Btn(t["resume"], callback_data="resume")],
                [Btn(t["lang_btn"], callback_data="togglelang:resume")],
            ]
        )
        await update.message.reply_text(
            t["resume_title"].format(done=done, n=TOTAL) + "\n\n<i>" + t["restart_note"] + "</i>",
            parse_mode=HTML,
            reply_markup=kb,
        )
        return
    await update.message.reply_text(UI["en"]["pick_lang"], parse_mode=HTML, reply_markup=lang_keyboard())


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_text(UI["en"]["pick_lang"], parse_mode=HTML, reply_markup=lang_keyboard())


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    if user["status"] == "submitted":
        await update.message.reply_text(UI[L(user)]["already"])
        return
    await show_review(update, user["uid"], edit=False)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    await update.message.reply_text(UI[L(user)]["help"], parse_mode=HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    t = UI[L(user)]
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(t["admin_only"])
        return
    c = lambda s: conn.execute("SELECT COUNT(*) FROM users WHERE status=?", (s,)).fetchone()[0]
    submitted, draft = c("submitted"), c("draft")
    await update.message.reply_text(
        t["stats"].format(started=submitted + draft, submitted=submitted, draft=draft), parse_mode=HTML
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    t = UI[L(user)]
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(t["admin_only"])
        return
    users = conn.execute("SELECT * FROM users WHERE status='submitted' ORDER BY submitted_at").fetchall()
    if not users:
        await update.message.reply_text(t["no_data"])
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["respondent", "language", "submitted_at"] + [f"Q{i}: {q['en_title']}" for i, q in enumerate(QUESTIONS, 1)])
    for u in users:
        ans = get_answers(u["uid"])
        row = ["Anonymous" if u["anonymous"] else u["display_name"], u["lang"], u["submitted_at"]]
        for i in range(1, TOTAL + 1):
            a = ans.get(i)
            row.append("Not Applicable" if (a is not None and a["na"]) else (a["text"] if a else ""))
        w.writerow(row)
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM so Excel renders Amharic correctly
    data.name = f"st_urael_assessment_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await update.message.reply_document(data, caption=t["export_caption"])


# ───────────────────────────── Buttons ─────────────────────────────
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    user = ensure_user(update)
    action, _, arg = q.data.partition(":")

    if user["status"] == "submitted":
        await q.answer(UI[L(user)]["already"], show_alert=True)
        return

    if action == "lang":
        set_user(uid, lang=arg)
        await q.answer()
        await show_welcome(update, get_user(uid), edit=True)

    elif action == "togglelang":
        set_user(uid, lang="am" if L(user) == "en" else "en")
        await q.answer()
        new = get_user(uid)
        if arg == "welcome":
            await show_welcome(update, new, edit=True)
        elif arg == "review":
            await show_review(update, uid)
        elif arg == "resume":
            await cmd_resume_view(update, new)
        else:
            await show_question(update, uid, int(arg))

    elif action == "anon":
        set_user(uid, anonymous=0 if user["anonymous"] else 1)
        await q.answer()
        await show_welcome(update, get_user(uid), edit=True)

    elif action in ("resume",):
        await q.answer()
        n = user["cur"] if user["cur"] and user["cur"] >= 1 else 1
        if user["cur"] == 0:
            await show_review(update, uid)
        else:
            await show_question(update, uid, n)

    elif action == "q":
        await q.answer()
        n = max(1, min(TOTAL, int(arg)))
        context.user_data.pop("edit_mode", None)
        await show_question(update, uid, n)

    elif action == "e":  # edit from review
        await q.answer()
        context.user_data["edit_mode"] = True
        await show_question(update, uid, int(arg))

    elif action == "na":
        n = int(arg)
        save_answer(uid, n, None, na=True)
        await q.answer(UI[L(user)]["marked_na"])
        await advance(update, context, uid, n)

    elif action == "review":
        await q.answer()
        await show_review(update, uid)

    elif action == "submit":
        answers = get_answers(uid)
        pending = [str(i) for i in range(1, TOTAL + 1) if not is_resolved(answers.get(i))]
        t = UI[L(user)]
        if pending:
            await q.answer(t["incomplete"].format(pending=", ".join(pending)), show_alert=True)
            return
        await q.answer()
        kb = Markup(
            [[Btn(t["confirm_yes"], callback_data="confirm")], [Btn(t["confirm_no"], callback_data="review")]]
        )
        await render(update, t["confirm_title"], kb, edit=True)

    elif action == "confirm":
        answers = get_answers(uid)
        if any(not is_resolved(answers.get(i)) for i in range(1, TOTAL + 1)):
            await q.answer("Incomplete", show_alert=True)
            return
        set_user(uid, status="submitted", submitted_at=now(), cur=0)
        await q.answer()
        await render(update, UI[L(user)]["thanks"], None, edit=True)

    elif action == "exit":
        await q.answer()
        await render(update, UI[L(user)]["exit_msg"], None, edit=True)

    else:
        await q.answer()


async def cmd_resume_view(update: Update, user):
    t = UI[L(user)]
    done = sum(1 for a in get_answers(user["uid"]).values() if is_resolved(a))
    kb = Markup(
        [[Btn(t["resume"], callback_data="resume")], [Btn(t["lang_btn"], callback_data="togglelang:resume")]]
    )
    await render(update, t["resume_title"].format(done=done, n=TOTAL) + "\n\n<i>" + t["restart_note"] + "</i>", kb, True)


async def advance(update, context, uid, n):
    """After answering question n: go back to review if editing, else to the next question."""
    if context.user_data.get("edit_mode") or n >= TOTAL:
        context.user_data.pop("edit_mode", None)
        await show_review(update, uid)
    else:
        await show_question(update, uid, n + 1)


# ───────────────────────────── Text answers ─────────────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    t = UI[L(user)]
    if user["status"] == "submitted":
        await update.message.reply_text(t["already"])
        return
    n = user["cur"]
    if user["status"] != "draft" or not n or n < 1:
        await cmd_start(update, context)
        return
    text = (update.message.text or "").strip()
    if len(text) > MAX_CHARS:
        await update.message.reply_text(t["too_long"].format(max=MAX_CHARS))
        return
    save_answer(user["uid"], n, text, na=False)
    await update.message.reply_text(t["saved"])
    await advance(update, context, user["uid"], n)


async def on_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update)
    await update.message.reply_text(UI[L(user)]["no_text"])


# ───────────────────────────── Main ─────────────────────────────
async def post_init(app: Application):
    from telegram import BotCommand

    for code, cmds in {
        "en": [("start", "Start / resume"), ("review", "Review answers"), ("language", "Language"), ("help", "Help")],
        "am": [("start", "ጀምር / ቀጥል"), ("review", "መልሶችን ገምግም"), ("language", "ቋንቋ"), ("help", "እገዛ")],
    }.items():
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in cmds], language_code=code if code == "am" else None)


def main():
    if not TOKEN:
        raise SystemExit("Set BOT_TOKEN in your .env file (get one from @BotFather).")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, on_non_text))
    log.info("Bot started.")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()