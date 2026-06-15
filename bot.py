import sys
import asyncio
import os
import logging
import time

# Ensure UTF-8 output for emojis in terminal (Windows fix)
try:
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except:
    pass

async def main():
    print("🚀 Bot starting...")

    print("📡 Loading configuration...")
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(level=logging.WARNING,   # Only WARNINGS+ to reduce log noise / overhead
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    # Keep aiogram event logs at INFO so we still see duration
    logging.getLogger("aiogram.event").setLevel(logging.INFO)

    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        print("❌ FATAL: BOT_TOKEN not found in .env file!")
        return

    print("📡 Initializing bot client...")
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=MemoryStorage())

    print("🧹 Clearing pending updates...")
    await bot.delete_webhook(drop_pending_updates=True)

    print("🗄️ Connecting to Database & Migrating...")
    from database.db import init_db, AsyncSessionLocal
    await init_db()

    # ── Pre-warm the connection pool ──────────────────────────────────────────
    # Open several connections in parallel so pool slots are ready before
    # the first real user message arrives (eliminates cold-start latency).
    print("🔥 Pre-warming connection pool...")
    async def _ping():
        from database.db import get_db
        db = get_db()
        await db.command("ping")
    await asyncio.gather(_ping(), _ping(), _ping())

    # ── Pre-warm ALL middleware caches ─────────────────────────────────────────
    # Load users + settings + channels from DB in one parallel batch so that
    # the middleware NEVER needs to hit the DB for any existing user.
    # This is the single biggest latency killer.
    print("🧠 Pre-loading middleware caches...")
    from middlewares.checks import (
        GLOBAL_CACHE, USER_CACHE,
        MAINTENANCE_TTL, CHANNELS_TTL, USER_TTL, JOIN_TTL
    )
    from database.requests import get_channels, get_setting, get_all_users

    # SQLAlchemy async sessions are NOT concurrent-safe, so each parallel
    # query must use its own dedicated session.
    async def _load_maintenance():
        async with AsyncSessionLocal() as s:
            return await get_setting(s, "maintenance_mode", "off")

    async def _load_channels():
        async with AsyncSessionLocal() as s:
            return await get_channels(s)

    async def _load_users():
        async with AsyncSessionLocal() as s:
            return await get_all_users(s)

    _now = time.time()
    _m_mode, _channels, _all_users = await asyncio.gather(
        _load_maintenance(),
        _load_channels(),
        _load_users(),
    )

    GLOBAL_CACHE["maintenance"] = {"val": _m_mode, "expires": _now + MAINTENANCE_TTL}
    GLOBAL_CACHE["channels"]    = {"val": _channels, "expires": _now + CHANNELS_TTL}

    # Mark ALL existing users as joined (they were verified before the restart).
    # Channel membership will be re-checked once per hour per user.
    for _u in _all_users:
        USER_CACHE[_u.id] = {
            "is_admin":    _u.is_admin,
            "is_blocked":  _u.is_blocked,
            "is_joined":   True,          # Existing users are assumed in-channel
            "join_expires": _now + JOIN_TTL,
            "user_expires": _now + USER_TTL,
        }

    print(f"✅ Caches ready — {len(_all_users)} users · {len(_channels)} channels · "
          f"maintenance={'ON' if _m_mode == 'on' else 'OFF'}")

    # ── Background tasks ───────────────────────────────────────────────────────
    print("⚙️ Setting up background tasks...")
    from utils.tasks import cancel_expired_orders, keep_db_warm, check_low_stock
    asyncio.create_task(cancel_expired_orders(bot))
    asyncio.create_task(keep_db_warm())
    asyncio.create_task(check_low_stock(bot))

    # ── Middleware ─────────────────────────────────────────────────────────────
    print("🛡️ Registering middlewares...", flush=True)
    from middlewares.checks import GlobalCheckMiddleware
    mw = GlobalCheckMiddleware()
    dp.message.middleware(mw)
    dp.callback_query.middleware(mw)

    # ── Handlers ───────────────────────────────────────────────────────────────
    print("🔌 Registering handlers...", flush=True)
    from handlers import user, admin
    dp.include_router(user.router)
    dp.include_router(admin.router)

    print("✅ Bot is LIVE and polling.", flush=True)

    # ── Cloud keep-alive ───────────────────────────────────────────────────────
    port = os.getenv("PORT")
    if port:
        print(f"☁️ Cloud environment detected. Starting web server on port {port}...", flush=True)
        try:
            from fastapi import FastAPI
            import uvicorn
            app = FastAPI()

            @app.get("/")
            async def root():
                return {"status": "online", "bot": "@shien_chip_bot"}

            async def run_web():
                config = uvicorn.Config(app, host="0.0.0.0", port=int(port), log_level="error")
                server = uvicorn.Server(config)
                await server.serve()

            await asyncio.gather(
                dp.start_polling(bot, polling_timeout=30, handle_signals=True, close_bot_session=True),
                run_web()
            )
        except ImportError:
            print("❌ fastapi/uvicorn not installed. Polling normally.")
            await dp.start_polling(bot, polling_timeout=30, handle_signals=True, close_bot_session=True)
    else:
        await dp.start_polling(bot, polling_timeout=30, handle_signals=True, close_bot_session=True)


async def run_bot():
    """Wrapper to run the bot with automatic restart on fatal errors."""
    retry_delay = 5
    while True:
        try:
            await main()
        except Exception as e:
            print(f"\n❌ FATAL CRASH: {e}")
            import traceback
            traceback.print_exc()
            print(f"🔄 Restarting in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
            # Exponential backoff up to 60 seconds
            retry_delay = min(retry_delay * 2, 60)
        else:
            # If main() returns normally (e.g. stopped by user), break the loop
            break

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 Bot stopped by user.")
    except Exception as e:
        print(f"\n❌ UNHANDLED EXIT: {e}")
