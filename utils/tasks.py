import asyncio
from datetime import datetime
from aiogram import Bot
from database.db import get_db
from database.requests import get_expired_pending_transactions, update_transaction, get_inventory_summary


async def keep_db_warm():
    """
    Prevent MongoDB Atlas cluster from going idle.
    """
    while True:
        try:
            db = get_db()
            if db is not None:
                await db.command("ping")
        except Exception:
            pass
        await asyncio.sleep(60)


async def cancel_expired_orders(bot: Bot):
    """
    Background task: cancel pending orders that have expired
    and have NO payment proof, release their stock, and notify users.
    """
    while True:
        try:
            db = get_db()
            if db is None:
                await asyncio.sleep(10)
                continue

            expired_txs = await get_expired_pending_transactions(db)

            for tx in expired_txs:
                # Cancel transaction
                await update_transaction(db, tx.id, status="cancelled")
                
                # Release coupons stock
                await db.coupons.update_many(
                    {"transaction_id": tx.id},
                    {"$set": {"transaction_id": None}}
                )

                try:
                    expiry_msg = (
                        "⌛ <b>QR expire ho gaya.</b>\n\n"
                        "Agar aapne payment kar di thi toh:\n"
                        "👉 🔄 <b>Recover Order</b> use karo — codes mil jayenge.\n\n"
                        "Nahi ki thi toh naya order banao 👇"
                    )
                    await bot.send_message(tx.user_id, expiry_msg)
                except Exception:
                    pass  # User may have blocked the bot

            if len(expired_txs) > 0:
                print(f"⏰ [Auto-Cancel] Cancelled {len(expired_txs)} expired orders.")

        except Exception as e:
            print(f"❌ [Auto-Cancel Error] {e}")

        await asyncio.sleep(60)   # Check every minute


# ─── Low Stock Alert ─────────────────────────────────────────────────────────
alerted_categories: set = set()


async def check_low_stock(bot: Bot):
    """
    Proactive monitoring: notify admin when stock drops below 5 units.
    """
    import os
    admin_id_raw = os.getenv("ADMIN_ID", "")
    primary_admin = admin_id_raw.split(",")[0].strip() if admin_id_raw else None

    if not primary_admin:
        return

    # Small startup delay so the pool is warm before this runs
    await asyncio.sleep(30)

    while True:
        try:
            db = get_db()
            if db is None:
                await asyncio.sleep(10)
                continue

            # get_inventory_summary returns: (cat_id, name, is_active, avail, pend, min_price)
            inventory = await get_inventory_summary(db)

            for cat_id, cat_name, is_active, avail, pend, price in inventory:
                if not is_active:
                    continue
                
                if avail <= 5:
                    if cat_id not in alerted_categories:
                        alert_msg = (
                            f"⚠️ <b>LOW STOCK ALERT</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n\n"
                            f"📁 <b>Category:</b> {cat_name}\n"
                            f"📦 <b>Remaining:</b> <u><b>{avail} units</b></u>\n\n"
                            f"🚀 <i>Please restock soon to avoid losing sales!</i>"
                        )
                        await bot.send_message(primary_admin, alert_msg)
                        alerted_categories.add(cat_id)
                else:
                    alerted_categories.discard(cat_id)

        except Exception as e:
            print(f"❌ [Low-Stock Task Error] {e}")

        await asyncio.sleep(1800)  # Every 30 minutes
