from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from database.db import get_db
from database.requests import get_setting, get_channels, get_user_by_id
import os
import time
import asyncio

# ─── Cache configuration ──────────────────────────────────────────────────────
GLOBAL_CACHE = {
    "maintenance": {"val": None, "expires": 0},
    "channels":    {"val": None, "expires": 0},
}
USER_CACHE: dict = {}

MAINTENANCE_TTL = 600
CHANNELS_TTL    = 7200
USER_TTL        = 900
JOIN_TTL        = 86400

SUPER_ADMINS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "0").split(",") if x.strip()]

# Rate-limiting
SPAM_TRACKER: dict = {}
_SPAM_WINDOW = 1.0
_SPAM_LIMIT  = 5

# Per-user lock for join checks
_JOIN_LOCK: dict = {}


def _get_join_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _JOIN_LOCK:
        _JOIN_LOCK[user_id] = asyncio.Lock()
    return _JOIN_LOCK[user_id]


class GlobalCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message | CallbackQuery, data: dict):
        if not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        now     = time.time()

        # ── 1. Rate limiting ──────────────────────────────────────────────────
        if user_id not in SUPER_ADMINS:
            hist = SPAM_TRACKER.get(user_id, [])
            if len(hist) >= _SPAM_LIMIT:
                hist = [t for t in hist if now - t < _SPAM_WINDOW]
                if len(hist) >= _SPAM_LIMIT:
                    SPAM_TRACKER[user_id] = hist
                    return
            hist.append(now)
            SPAM_TRACKER[user_id] = hist

        # ── 2. Fast path: fully cached user who has joined ────────────────────
        uc = USER_CACHE.get(user_id)
        db = get_db()

        if uc and now < uc.get("user_expires", 0):
            if uc["is_blocked"]:
                return
            if now < GLOBAL_CACHE["maintenance"]["expires"]:
                if GLOBAL_CACHE["maintenance"]["val"] == "on":
                    await _send_maintenance(event)
                    return
            if uc["is_admin"] or user_id in SUPER_ADMINS:
                data["db"] = data["session"] = db
                return await handler(event, data)
            if uc.get("is_joined") and now < uc.get("join_expires", 0):
                data["db"] = data["session"] = db
                return await handler(event, data)

        # ── 3. Full check (cache miss) ─────────────────────────────────────────
        data["db"] = data["session"] = db

        if not uc or now >= uc.get("user_expires", 0):
            user = await get_user_by_id(db, user_id)
            if not user:
                referred_by = None
                if isinstance(event, Message) and event.text and event.text.startswith("/start"):
                    parts = event.text.split()
                    if len(parts) > 1:
                        try: referred_by = int(parts[1])
                        except ValueError: pass

                from database.requests import get_or_create_user
                user, created = await get_or_create_user(db, user_id, 
                                               event.from_user.username, 
                                               event.from_user.full_name,
                                               referred_by=referred_by)
            else:
                created = False
            
            is_admin   = user.is_admin
            is_blocked = user.is_blocked
            USER_CACHE[user_id] = uc = {
                "is_admin":    is_admin,
                "is_blocked":  is_blocked,
                "is_joined":   uc.get("is_joined",   False) if uc else False,
                "join_expires":uc.get("join_expires", 0)    if uc else 0,
                "user_expires":now + USER_TTL,
                "is_new":      created
            }
        else:
            is_admin   = uc["is_admin"]
            is_blocked = uc["is_blocked"]

        if is_blocked:
            return
        if is_admin or user_id in SUPER_ADMINS:
            return await handler(event, data)

        # Maintenance
        if now > GLOBAL_CACHE["maintenance"]["expires"]:
            m = await get_setting(db, "maintenance_mode", "off")
            GLOBAL_CACHE["maintenance"] = {"val": m, "expires": now + MAINTENANCE_TTL}
        if GLOBAL_CACHE["maintenance"]["val"] == "on":
            await _send_maintenance(event)
            return

        # FSM bypass
        state = data.get("state")
        if state and await state.get_state():
            return await handler(event, data)

        # Force re-check on "I have joined" tap
        if isinstance(event, CallbackQuery) and event.data == "check_join":
            uc["is_joined"]    = False
            uc["join_expires"] = 0
            # Let the handler handle this specifically for better feedback
            return await handler(event, data)

        if uc.get("is_joined") and now < uc.get("join_expires", 0):
            return await handler(event, data)

        # Channel list
        if now > GLOBAL_CACHE["channels"]["expires"]:
            channels = await get_channels(db)
            GLOBAL_CACHE["channels"] = {"val": channels, "expires": now + CHANNELS_TTL}
        else:
            channels = GLOBAL_CACHE["channels"]["val"]

        if not channels:
            _mark_joined(user_id, now)
            return await handler(event, data)

        lock = _get_join_lock(user_id)
        if lock.locked():
            return await handler(event, data)

        async with lock:
            uc2 = USER_CACHE.get(user_id, {})
            if uc2.get("is_joined") and now < uc2.get("join_expires", 0):
                return await handler(event, data)
            not_joined = await _check_channels_parallel(data["bot"], user_id, channels)

        if not_joined:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📢 Join {ch.name}", url=ch.invite_link)]
                for ch in not_joined
            ] + [[InlineKeyboardButton(text="🔄 I have joined", callback_data="check_join")]])
            deny = "⚠️ <b>Access Denied</b>\n\nYou must join our official channels to use this bot:"
            if isinstance(event, Message):
                await event.answer(deny, reply_markup=kb)
            else:
                await event.answer("❌ Join our channels first!", show_alert=True)
            return

        _mark_joined(user_id, now)
        return await handler(event, data)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mark_joined(user_id: int, now: float):
    uc = USER_CACHE.setdefault(user_id, {"is_admin": False, "is_blocked": False, "user_expires": 0})
    uc["is_joined"]    = True
    uc["join_expires"] = now + JOIN_TTL


async def _send_maintenance(event):
    msg = "🛠 <b>Under Maintenance</b>\n\nWe are currently updating our systems. Please check back later!"
    if isinstance(event, Message):
        await event.answer(msg)
    else:
        await event.answer(msg, show_alert=True)


async def _check_channels_parallel(bot, user_id: int, channels) -> list:
    async def _check(ch):
        try:
            m = await bot.get_chat_member(chat_id=ch.chat_id, user_id=user_id)
            if m.status not in ("member", "administrator", "creator"):
                return ch
        except Exception:
            pass
        return None
    results = await asyncio.gather(*[_check(ch) for ch in channels])
    return [ch for ch in results if ch is not None]
