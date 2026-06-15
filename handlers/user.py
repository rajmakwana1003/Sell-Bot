import os
import time
import random
import string
import secrets
import re
import asyncio
from datetime import datetime, timedelta
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from motor.motor_asyncio import AsyncIOMotorDatabase
from database.db import AsyncSessionLocal
from database.requests import (
    get_or_create_user, get_categories, get_available_coupons,
    get_coupon, create_transaction, get_setting, get_user_by_id, 
    finalize_sale, update_transaction, get_coupons_by_transaction, 
    get_available_reward, use_reward, get_channels, increment_user_warning, 
    get_transaction, find_transaction_robust, get_support_contacts,
    get_category, get_category_stock_summary, get_inventory_summary,
    reserve_coupons_atomic, release_coupons_by_transaction,
    get_user_transactions_completed, get_transaction_with_items,
    count_available_rewards, get_user_redeemed_rewards, update_user,
    get_user_order_counts
)

# --- Configuration & Helpers ---
payment_abuse_tracker: dict = {}
PAYMENT_WINDOW = 3600
PAYMENT_LIMIT  = 5

def generate_order_id():
    return "SHN-" + secrets.token_hex(4).upper()

# Define menu buttons globally to avoid double-click bugs
MENU_BUTTONS = [
    "🛒 Buy Coupons", "📊 Live Inventory",
    "🛍️ My Orders", "👤 My Profile",
    "🤝 Refer & Earn", "🆘 Help & Support",
    "🔄 Recover Voucher", "📢 Join Channel",
    "🔐 Admin Control Panel"
]

router = Router()

# ── Application-level caches (avoid repeated DB hits on rapid taps) ──────────
# Category list for the Buy-Coupons screen — refreshed every 5 seconds
_CAT_CACHE: dict = {"data": None, "expires": 0}
# Live Inventory screen — refreshed every 5 seconds
_INV_CACHE: dict = {"data": None, "expires": 0}

class UserStates(StatesGroup):
    reading_terms = State()
    selecting_quantity = State()
    confirming_order = State()
    waiting_for_payment_screenshot = State()
    waiting_for_utr = State()
    recover_order_id = State()

@router.callback_query(F.data.startswith("paid_"))
async def paid_button_handler(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split("_")[-1])
    await state.update_data(tx_id=tx_id)
    await state.set_state(UserStates.waiting_for_payment_screenshot)
    await callback.message.answer("📸 <b>UPLOAD SCREENSHOT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nPlease upload your <b>Payment Screenshot</b> now.\n\n<i>Note: Once received, I will ask for your UTR number!</i>")
    await callback.answer()

def main_reply_keyboard(user_id: int, is_admin: bool = False):
    keyboard = [
        [KeyboardButton(text="🛒 Buy Coupons"), KeyboardButton(text="📊 Live Inventory")],
        [KeyboardButton(text="🛍️ My Orders"), KeyboardButton(text="👤 My Profile")],
        [KeyboardButton(text="🤝 Refer & Earn"), KeyboardButton(text="🆘 Help & Support")],
        [KeyboardButton(text="🔄 Recover Voucher"), KeyboardButton(text="📢 Join Channel")],
    ]
    super_admins = [int(id.strip()) for id in os.getenv("ADMIN_ID", "0").split(",") if id.strip()]
    if user_id in super_admins or is_admin:
        keyboard.append([KeyboardButton(text="🔐 Admin Control Panel")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, input_field_placeholder="How can we help you today?")

# --- Core Handlers ---

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    referred_by = None
    if command.args:
        try: referred_by = int(command.args)
        except ValueError: pass

    # Fast path: if we already know this user (middleware pre-warm / cache),
    # skip the SELECT query and use cached admin status.
    from middlewares.checks import USER_CACHE
    _cached = USER_CACHE.get(message.from_user.id)
    if _cached and not referred_by:  # referral needs DB to credit referrer
        is_admin = _cached["is_admin"]
        # Update username/name silently in the background (non-blocking)
        asyncio.create_task(_update_user_bg(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        ))
    else:
        user, created = await get_or_create_user(
            session, message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
            referred_by=referred_by,
        )
        is_admin = user.is_admin
        
        # Proper Referral System: Notify the referrer
        is_new_user = created or (_cached.get("is_new") if _cached else False)
        if is_new_user and referred_by and referred_by != message.from_user.id:
            if _cached: _cached["is_new"] = False # Only notify once
            try:
                await message.bot.send_message(
                    chat_id=referred_by,
                    text=f"🎊 <b>New Referral!</b>\n\n<b>{message.from_user.full_name}</b> joined using your link. Keep going! 🚀"
                )
            except Exception:
                pass

    welcome_text = (
        f"🚀 <b>Welcome to PREMIUM SHOP!</b> 🎟️\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Hello <b>{message.from_user.full_name}</b>! Ready to unlock some amazing deals?\n\n"
        f"We are your trusted source for <b>verified SHEIN discount vouchers</b>. "
        f"Get premium codes with <b>instant delivery</b> and 24/7 protection.\n\n"
        f"✨ <b>WHY CHOOSE US?</b>\n"
        f"├─ ⚡ <b>Instant Delivery:</b> No waiting, get codes now.\n"
        f"├─ 💰 <b>Unbeatable Prices:</b> Best rates in the market.\n"
        f"├─ ✅ <b>100% Verified:</b> All vouchers are pre-checked.\n"
        f"└─ 📞 <b>24/7 Support:</b> Real human help 1-on-1.\n\n"
        f"🛍️ <b>WHAT YOU CAN DO:</b>\n"
        f"🛒 <b>Browse:</b> Check the latest available coupons.\n"
        f"💳 <b>Buy:</b> Purchase instantly using secure UPI.\n"
        f"📦 <b>Orders:</b> View your purchased vouchers.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚨 <b>MANDATORY RULE:</b>\n"
        f"You <u>MUST</u> record an <b>uncut screen recording</b> from the moment you pay until you apply the code. "
        f"No video = No replacement. No exceptions! ❌\n\n"
        f"👇 <b>Select an option below to begin:</b>"
    )
    await message.answer(welcome_text, reply_markup=main_reply_keyboard(message.from_user.id, is_admin))


async def _update_user_bg(user_id: int, username: str, full_name: str):
    """Fire-and-forget background update of user display info (no await in hot path)."""
    try:
        from database.db import AsyncSessionLocal as _ASL
        async with _ASL() as _s:
            await _s.users.update_one(
                {"_id": user_id},
                {"$set": {"username": username, "full_name": full_name}}
            )
    except Exception:
        pass

@router.message(F.text == "🔐 Admin Control Panel")
async def user_admin_btn(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    super_admins = [int(id.strip()) for id in os.getenv("ADMIN_ID", "0").split(",") if id.strip()]
    user = await get_user_by_id(session, message.from_user.id)
    is_admin = user.is_admin if user else False
    if message.from_user.id in super_admins or is_admin:
        from handlers.admin import cmd_admin
        await cmd_admin(message, state, session)

@router.callback_query(F.data == "check_join")
async def check_join_callback(callback: CallbackQuery, bot: Bot, session: AsyncIOMotorDatabase):
    channels = await get_channels(session)
    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch.chat_id, user_id=callback.from_user.id)
            if member.status not in ["member", "administrator", "creator"]: not_joined.append(ch)
        except: continue
    if not_joined:
        await callback.answer("⚠️ Please join all channels first!", show_alert=True)
        return
    await callback.message.answer("✅ <b>Access Granted!</b>\n\nYou are now authorized to use the bot.", reply_markup=main_reply_keyboard(callback.from_user.id))
    await callback.message.delete(); await callback.answer()

@router.message(F.text == "🛒 Buy Coupons")
async def user_browse(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    now = time.time()
    if now < _CAT_CACHE["expires"] and _CAT_CACHE["data"] is not None:
        cat_stats = _CAT_CACHE["data"]
    else:
        cat_stats = await get_category_stock_summary(session)
        _CAT_CACHE["data"]    = cat_stats
        _CAT_CACHE["expires"] = now + 5   # Cache for 5 seconds
        
    if not cat_stats:
        await message.answer("🛒 <b>Store Temporarily Closed</b>\n\nWe are currently restocking our digital inventory. Check back in 5-10 minutes! 🔄")
        return
        
    keyboard = []
    for cat, stock, price in cat_stats:
        if stock > 0: keyboard.append([InlineKeyboardButton(text=f"🎫 {cat.name} — ₹{price}", callback_data=f"agree_terms_{cat.id}")])
        else: keyboard.append([InlineKeyboardButton(text=f"❌ {cat.name} — Out of Stock", callback_data="sold_out_alert")])
        
    msg_text = ("🛒 <b>SELECT YOUR PACKAGE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\nAll codes are pre-verified and valid for the current month. Choose your desired discount below:")
    await message.answer(msg_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data == "sold_out_alert")
async def sold_out_alert_cb(callback: CallbackQuery):
    await callback.answer("🔴 This item is currently sold out. Restocking soon!", show_alert=True)

@router.callback_query(F.data == "back_to_cats")
async def back_to_cats_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear(); await user_browse(callback.message, state, session); await callback.message.delete(); await callback.answer()

@router.callback_query(F.data.startswith("agree_terms_"))
async def show_category_terms(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear() 
    cat_id = int(callback.data.split("_")[-1])
    cat = await get_category(session, cat_id)
    if not cat: return await callback.answer("Error.")
    default_terms = (
        "📜 <b>READ BEFORE BUYING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ <b>VIDEO PROOF:</b> Start screen recording <u>NOW</u> (before payment).\n\n"
        "2️⃣ <b>REDEEM NOW:</b> Vouchers must be used immediately after receiving.\n\n"
        "3️⃣ <b>SUPPORT:</b> No replacement without full uncut video proof.\n\n"
        "4️⃣ <b>REQUISITES:</b> Your SHEIN cart must be <b>₹1000+</b>.\n\n"
        "✅ <i>Do you agree to follow these rules?</i>"
    )
    await state.update_data(selected_cat_id=cat_id)
    await state.set_state(UserStates.reading_terms)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yes, I Understand", callback_data=f"buy_cat_{cat_id}")], [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_cats")]])
    await callback.message.edit_text(cat.terms if cat.terms else default_terms, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()

@router.callback_query(F.data.startswith("buy_cat_"), UserStates.reading_terms)
async def start_buy_flow(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    cat_id = int(callback.data.split("_")[-1])
    available = await get_available_coupons(session, cat_id)
    if not available: return await callback.answer("❌ Sold out!", show_alert=True)
    price = available[0].price_inr; cat = await get_category(session, cat_id); cat_name = cat.name
    await state.update_data(cat_id=cat_id, cat_name=cat_name, unit_price=float(price), max_stock=len(available))
    await state.set_state(UserStates.selecting_quantity)
    kb = [[InlineKeyboardButton(text=f"Buy {s}", callback_data=f"qty_set_{s}") for s in [1, 2, 5] if s <= len(available)], [InlineKeyboardButton(text="⌨️ Custom Qty", callback_data="qty_custom")], [InlineKeyboardButton(text="🔙 Back", callback_data=f"agree_terms_{cat_id}")]]
    await callback.message.edit_text(f"🔢 <b>SELECT QUANTITY</b>\n\n📂 Item: <b>{cat_name}</b>\n💰 Price: <b>₹{price} each</b>\n📦 Available: <b>{len(available)}</b>\n\n<i>How many codes would you like to purchase?</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data.startswith("qty_set_"), UserStates.selecting_quantity)
async def process_qty_preset(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await show_order_confirmation(callback.message, state, int(callback.data.split("_")[-1])); await callback.answer()

@router.callback_query(F.data == "qty_custom", UserStates.selecting_quantity)
async def process_qty_custom_trigger(callback: CallbackQuery):
    await callback.message.answer("⌨️ <b>Send the number of coupons you want:</b>"); await callback.answer()

@router.message(UserStates.selecting_quantity)
async def process_custom_quantity(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    if message.text in MENU_BUTTONS: await state.clear(); return 
    data = await state.get_data()
    try:
        qty = int(message.text)
        if qty < 1 or qty > data['max_stock']: raise ValueError
    except: return await message.answer(f"❌ Enter a valid quantity (1-{data['max_stock']}):")
    await show_order_confirmation(message, state, qty)

async def show_order_confirmation(msg_obj: Message, state: FSMContext, qty: int):
    data = await state.get_data(); total_price = qty * data['unit_price']
    await state.update_data(quantity=qty, total_price=total_price)
    await state.set_state(UserStates.confirming_order)
    summary = (
        f"🛒 <b>ORDER SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Voucher:</b> {data['cat_name']}\n"
        f"🔢 <b>Quantity:</b> {qty} code(s)\n"
        f"💸 <b>Rate:</b> ₹{data['unit_price']}\n"
        f"💰 <b>Total Amount:</b> <b>₹{total_price}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ <i>Ready to pay and receive your codes?</i>"
    )
    kb = [[InlineKeyboardButton(text=f"💳 Pay ₹{total_price} Now", callback_data="confirm_order")], [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_order")]]
    if msg_obj.text: await msg_obj.answer(summary, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else: await msg_obj.edit_text(summary, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# Global Cache for Payment Image to prevent re-uploading
PAYMENT_IMAGE_CACHE = {"file_id": None}

@router.callback_query(F.data == "confirm_order", UserStates.confirming_order)
async def confirm_payment_step(callback: CallbackQuery, state: FSMContext, bot: Bot, session: AsyncIOMotorDatabase):
    data = await state.get_data(); user_id = callback.from_user.id
    if data.get("_processing"): return await callback.answer("⏳ Processing your order...", show_alert=True)
    
    await callback.answer("⚡ Securing your stock...")
    await state.update_data(_processing=True)

    try:
        order_id = generate_order_id()
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        
        tx = await create_transaction(
            session, user_id=user_id, 
            amount=data['total_price'], 
            quantity=data['quantity'], 
            provider_payment_charge_id=order_id,
            expires_at=expires_at
        )
        tx_id = tx.id

        coupons = await reserve_coupons_atomic(session, data['cat_id'], data['quantity'], tx_id)

        if not coupons:
            await update_transaction(session, tx_id, status='failed')
            await state.update_data(_processing=False)
            return await callback.message.answer("❌ <b>Low Stock!</b> Someone else just bought these. Please try a smaller quantity.")

    except Exception as e:
        await state.update_data(_processing=False)
        print(f"Checkout Error: {e}")
        return await callback.message.answer("❌ <b>Server Busy.</b> Please try again in 5 seconds.")

    upi_id = await get_setting(session, "upi_id", "payment.shein@upi")
    pay_msg = (
        f"💳 <b>PAYMENT INSTRUCTIONS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
        f"💰 <b>Amount:</b> <b>₹{data['total_price']}</b>\n"
        f"🔗 <b>UPI:</b> <code>{upi_id}</code>\n"
        f"⏳ <b>Expires in:</b> 10 Minutes\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"1️⃣ Pay the <u>EXACT</u> amount to the UPI ID.\n"
        f"2️⃣ Save the screenshot and note the <b>12-digit UTR/Ref Number</b>.\n"
        f"3️⃣ <b>RECORD YOUR SCREEN:</b> No replacement without full uncut video proof!\n\n"
        f"👇 <b>Once paid, click the button below:</b>"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I Have Paid", callback_data=f"paid_{tx_id}")],
        [InlineKeyboardButton(text="❌ Cancel Order", callback_data=f"cancel_order_tx_{tx_id}")]
    ])

    try:
        if PAYMENT_IMAGE_CACHE["file_id"]:
            sent_msg = await bot.send_photo(chat_id=user_id, photo=PAYMENT_IMAGE_CACHE["file_id"], caption=pay_msg, reply_markup=kb)
        else:
            qr_path = os.getenv("PAYMENT_METHOD_IMAGE_PATH", "assets/payment.jpg")
            if os.path.exists(qr_path):
                photo = FSInputFile(qr_path)
                sent_msg = await bot.send_photo(chat_id=user_id, photo=photo, caption=pay_msg, reply_markup=kb)
                PAYMENT_IMAGE_CACHE["file_id"] = sent_msg.photo[-1].file_id
            else:
                await bot.send_message(chat_id=user_id, text=pay_msg, reply_markup=kb)
        
        await state.update_data(tx_id=tx_id, order_id=order_id, _processing=False)
        await state.set_state(UserStates.waiting_for_payment_screenshot)
        await callback.message.delete()
    except Exception as e:
        await state.update_data(_processing=False)
        await bot.send_message(chat_id=user_id, text=pay_msg, reply_markup=kb)

@router.callback_query(F.data.startswith("cancel_order_tx_"))
async def cancel_order_tx_btn(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    tx_id = int(callback.data.split("_")[-1]); user_id = callback.from_user.id
    tx = await get_transaction(session, tx_id)
    if tx and tx.user_id == user_id and tx.status == 'pending':
        await update_transaction(session, tx_id, status='cancelled')
        await release_coupons_by_transaction(session, tx_id)
    await state.clear(); msg_text = "❌ <b>Order Cancelled.</b> Stock has been released. You can try again anytime."
    if callback.message.photo: await callback.message.edit_caption(caption=msg_text)
    else: await callback.message.edit_text(msg_text)
    await callback.answer("Cancelled.")

@router.callback_query(F.data == "cancel_order")
async def cancel_order_btn(callback: CallbackQuery, state: FSMContext):
    await state.clear(); await callback.message.answer("❌ <b>Session Aborted.</b>", reply_markup=main_reply_keyboard(callback.from_user.id))
    await callback.message.delete(); await callback.answer()

@router.message(UserStates.waiting_for_payment_screenshot, F.photo)
async def process_screenshot(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    data = await state.get_data()
    tx_id = data.get('tx_id')
    if tx_id:
        await update_transaction(session, int(tx_id), payment_proof_id=f"FILE:{photo_id}")
    
    await state.set_state(UserStates.waiting_for_utr)
    await message.answer(f"📸 <b>SCREENSHOT RECEIVED!</b> ✅\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>Order ID:</b> <code>{data.get('order_id')}</code>\n━━━━━━━━━━━━━━━━━━━━━━\n\n<b>FINAL STEP:</b> Please send your <b>12-digit UTR Number</b>.\n\n📍 <i>Example: 312345678901</i>")

@router.message(UserStates.waiting_for_payment_screenshot)
async def process_screenshot_invalid(message: Message):
    if message.text in MENU_BUTTONS: return
    await message.answer("⚠️ <b>Invalid Input!</b> Please send the <b>Payment Screenshot</b> (as a photo) to proceed.")

@router.message(UserStates.waiting_for_utr, F.text)
async def process_utr_final(message: Message, state: FSMContext, bot: Bot, session: AsyncIOMotorDatabase):
    if message.text in MENU_BUTTONS: await state.clear(); return
    user_id = message.from_user.id
    
    # Robust UTR Extraction
    utr_match = re.search(r'\d{12}', message.text)
    if not utr_match:
        return await message.answer("⚠️ <b>INVALID UTR</b>\nPlease send the <b>12-digit UTR/Reference number</b> from your payment app.")
    
    utr = utr_match.group(0)

    # Check for Duplicate UTR
    existing_utr = await find_transaction_robust(session, utr)
    if existing_utr:
        count = await increment_user_warning(session, user_id)
        await state.clear()
        if count >= 5:
            return await message.answer("🚫 <b>PERMANENT BAN</b>\n\nReason: Multiple duplicate payment references (UTR) detected.")
        return await message.answer(f"⚠️ <b>DUPLICATE UTR DETECTED</b>\n\nThis UTR (<code>{utr}</code>) has already been used for another order. Attempt logged ({count}/5 strikes).\n\n<i>Do not reuse UTRs from old payments. Repeat offenses lead to a permanent ban!</i>")

    now_ts = time.time(); tracker = payment_abuse_tracker.setdefault(user_id, [])
    payment_abuse_tracker[user_id] = [t for t in tracker if now_ts - t < PAYMENT_WINDOW]; payment_abuse_tracker[user_id].append(now_ts)
    
    if len(payment_abuse_tracker[user_id]) >= PAYMENT_LIMIT:
        await update_user(session, user_id, is_blocked=True, is_suspicious=True, warning_count=PAYMENT_LIMIT)
        await state.clear(); return await message.answer("🚫 <b>PERMANENT BAN</b>\n\nReason: Suspicious payment attempts detected. Contact support for appeal.")

    data = await state.get_data(); order_id = data.get('order_id'); tx_id = data.get('tx_id'); photo_id = data.get('photo_id')
    
    if tx_id:
        tx = await get_transaction(session, int(tx_id))
        if not tx or tx.status != 'pending': return await message.answer("❌ This order window has closed.")
        
        # If photo_id is missing from state (restart), try to recover from DB
        if not photo_id and tx.payment_proof_id and tx.payment_proof_id.startswith("FILE:"):
            photo_id = tx.payment_proof_id.split(":", 1)[1]

        if not photo_id:
            await state.set_state(UserStates.waiting_for_payment_screenshot)
            return await message.answer("❌ <b>Session error.</b> Please re-upload your screenshot first.")

        await update_transaction(session, int(tx_id), utr=utr, payment_proof_id=f"UTR: {utr} | File: {photo_id}")
    else: return await message.answer("❌ Session Error. Please try again.")

    # Safe retrieval of Admin ID for notifications
    admin_id_raw = os.getenv("ADMIN_ID", "")
    primary_admin = admin_id_raw.split(",")[0].strip() if admin_id_raw else None
    
    if primary_admin:
        kb = [[InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve_{tx_id}"), InlineKeyboardButton(text="❌ Reject",  callback_data=f"admin_reject_{tx_id}")]]
        try:
            await bot.send_photo(chat_id=primary_admin, photo=photo_id, caption=f"🔔 <b>NEW PENDING ORDER</b>\n━━━━━━━━━━━━━━━━━━\n👤 <b>Client:</b> {message.from_user.full_name}\n🆔 <b>ID:</b> <code>{user_id}</code>\n📦 <b>Item:</b> {data.get('quantity', tx.quantity)} × {data.get('cat_name', '(Voucher)')}\n💸 <b>Paid:</b> ₹{data.get('total_price', tx.amount)}\n━━━━━━━━━━━━━━━━━━\n📋 <b>ID:</b> <code>{order_id}</code>\n🔑 <b>UTR:</b> <code>{utr}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        except Exception as e:
            print(f"Admin Notification Error: {e}")
            
    await state.clear()
    await message.answer(f"✅ <b>ORDER SUBMITTED!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>Order ID:</b> <code>{order_id}</code>\n💰 <b>Total Paid:</b> ₹{data.get('total_price', tx.amount)}\n━━━━━━━━━━━━━━━━━━━━━━\n⏳ <b>STATUS:</b> Verifying Payment...\n\n🚀 <i>Vouchers will be delivered here within 5-15 minutes. Start your screen recording now!</i>")

@router.message(UserStates.waiting_for_utr)
async def process_utr_invalid(message: Message):
    if message.text in MENU_BUTTONS: return
    await message.answer("⚠️ <b>Invalid Input!</b> Please send your <b>12-digit UTR Number</b> (digits only).")

@router.message(F.text == "📊 Live Inventory")
async def user_stocks(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    now = time.time()
    if now < _INV_CACHE["expires"] and _INV_CACHE["data"] is not None:
        cat_stats = _INV_CACHE["data"]
    else:
        cat_stats = await get_inventory_summary(session)
        _INV_CACHE["data"]    = cat_stats
        _INV_CACHE["expires"] = now + 5   # Cache for 5 seconds
    
    if not cat_stats:
        return await message.answer("📊 <b>Stock Status</b>\n\nNo active registries found.")
        
    text = "📊 <b>REAL-TIME INVENTORY</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    total_avail = 0
    
    for row in cat_stats:
        cat_id, cat_name, is_active, avail, pend, price = row
        status_icon = "🟢" if is_active and avail > 0 else "🟡" if is_active else "🚨"
        text += f"{status_icon} <b>{cat_name.upper()}</b>"
        if price: text += f" — <b>₹{price}</b>"
        text += f"\n├─ 📦 Available: <code>{avail}</code> units\n├─ ⏳ Processing: <code>{pend}</code> units\n└─ 🛡 Status: <i>{'OPERATIONAL' if is_active else 'MAINTENANCE'}</i>\n\n"
        total_avail += avail
        
    text += "━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"💎 <b>Total Vouchers:</b> <code>{total_avail}</code> units\n"
    text += f"🕒 <b>Last Sync:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
    text += "<i>💡 Stock data is live. Reserved units auto-release in 10 mins.</i>"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Refresh Stock", callback_data="refresh_inventory")]])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "refresh_inventory")
async def refresh_inventory_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    now = time.time()
    # Invalidate cache so user gets fresh data on explicit refresh
    _INV_CACHE["expires"] = 0
    cat_stats = await get_inventory_summary(session)
    _INV_CACHE["data"]    = cat_stats
    _INV_CACHE["expires"] = now + 5
    
    if not cat_stats:
        return await callback.message.edit_text("📊 <b>Stock Status</b>\n\nNo active registries found.")
        
    text = "📊 <b>REAL-TIME INVENTORY</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    total_avail = 0
    
    for row in cat_stats:
        cat_id, cat_name, is_active, avail, pend, price = row
        status_icon = "🟢" if is_active and avail > 0 else "🟡" if is_active else "🚨"
        text += f"{status_icon} <b>{cat_name.upper()}</b>"
        if price: text += f" — <b>₹{price}</b>"
        text += f"\n├─ 📦 Available: <code>{avail}</code> units\n├─ ⏳ Processing: <code>{pend}</code> units\n└─ 🛡 Status: <i>{'OPERATIONAL' if is_active else 'MAINTENANCE'}</i>\n\n"
        total_avail += avail
        
    text += "━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"💎 <b>Total Vouchers:</b> <code>{total_avail}</code> units\n"
    text += f"🕒 <b>Last Sync:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
    text += "<i>💡 Stock data is live. Reserved units auto-release in 10 mins.</i>"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Refresh Stock", callback_data="refresh_inventory")]])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except:
        pass # Content identical
    await callback.answer("Inventory Refreshed!")

async def show_user_orders(session: AsyncIOMotorDatabase, user_id: int, message_or_callback):
    txs = await get_user_transactions_completed(session, user_id)
    
    if not txs:
        text = (
            "🛍️ <b>PURCHASE LOGS</b>\n\n"
            "You haven't made any successful purchases yet.\n\n"
            "🚀 <b>Tip:</b> Tap 'Buy Coupons' to grab your first deal!"
        )
        if isinstance(message_or_callback, Message): await message_or_callback.answer(text)
        else: await message_or_callback.message.edit_text(text)
        return
    
    text = (
        f"🛍️ <b>ORDER HISTORY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"You have <b>{len(txs)}</b> completed orders.\n"
        f"Tap an order below to view your voucher codes:"
    )
    
    keyboard = [[InlineKeyboardButton(text=f"📅 {tx.created_at.strftime('%d %b')} | ₹{tx.amount} | ID: {tx.provider_payment_charge_id[-8:] if tx.provider_payment_charge_id else f'TX-{tx.id}'}", callback_data=f"view_order_{tx.id}")] for tx in txs[:15]]
    
    if isinstance(message_or_callback, Message): await message_or_callback.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else: await message_or_callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.message(F.text == "🛍️ My Orders")
async def user_orders(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear(); await show_user_orders(session, message.from_user.id, message)

@router.callback_query(F.data.startswith("view_order_"))
async def view_order_details_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    tx_id = int(callback.data.split("_")[-1])
    tx, items = await get_transaction_with_items(session, tx_id)
    if not tx or tx.user_id != callback.from_user.id: return await callback.answer("❌ Order not found.", show_alert=True)
    
    text = (f"📋 <b>ORDER DETAILS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n🧾 <b>Order ID:</b> <code>{tx.provider_payment_charge_id}</code>\n📅 <b>Date:</b> {tx.created_at.strftime('%d %b %Y, %H:%M')}\n💸 <b>Amount:</b> ₹{tx.amount}\n━━━━━━━━━━━━━━━━━━━━━━\n\n🎁 <b>YOUR VOUCHERS:</b>\n")
    for c, cat in items: text += f"▪️ {cat.name}: <code>{c.code}</code>\n"
    text += f"\n━━━━━━━━━━━━━━━━━━━━━━\n🚀 <b>HOW TO USE:</b>\nVisit SHEIN, apply code at checkout. Remember to record your <b>uncut video</b> for safety!"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to History", callback_data="back_to_history")]])); await callback.answer()

@router.callback_query(F.data == "back_to_history")
async def back_to_history_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await show_user_orders(session, callback.from_user.id, callback); await callback.answer()

@router.message(F.text == "🤝 Refer & Earn")
async def refer_earn(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    await show_referral_menu(message, session, message.from_user.id)

async def show_referral_menu(message: Message, session: AsyncIOMotorDatabase, user_id: int):
    user = await get_user_by_id(session, user_id); status = await get_setting(session, "refer_earn_status", "on"); goal = int(await get_setting(session, "refer_goal", "3"))
    pool = await count_available_rewards(session)
    if status == "off": return await message.answer("👥 <b>REFERRAL SYSTEM</b>\n\nThe program is currently offline. 🔄")
    
    progress = user.referral_count if user else 0
    bar_size = 10
    filled = min(int((progress / goal) * bar_size), bar_size) if goal > 0 else 0
    bar = "🟩" * filled + "⬜" * (bar_size - filled)
    
    kb = [
        [InlineKeyboardButton(text=f"🎁 Claim Reward {'✅' if progress >= goal else ''}", callback_data="redeem_referral")],
        [InlineKeyboardButton(text="🔗 Get Invite Link", callback_data="view_referral_link"), InlineKeyboardButton(text="📜 Rules", callback_data="view_refer_rules")],
        [InlineKeyboardButton(text="🏆 Top Leaders", callback_data="view_leaderboard"), InlineKeyboardButton(text="🎟️ My Rewards", callback_data="my_redeemed_rewards")]
    ]
    
    text = (
        f"👥 <b>REFER & EARN PROGRAM</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Invite your friends and earn premium vouchers for <b>FREE</b>! 🎁\n\n"
        f"🎯 <b>Goal:</b> {goal} Referrals = 1 Voucher\n"
        f"📦 <b>Pool:</b> {pool or 0} rewards remaining\n\n"
        f"📊 <b>YOUR PROGRESS:</b>\n"
        f"<code>{bar}</code> <b>{progress}/{goal}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 <i>The more you invite, the more you earn!</i>"
    )
    
    if message.from_user.id == (await message.bot.get_me()).id:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "view_refer_rules")
async def view_refer_rules_cb(callback: CallbackQuery):
    rules = (
        "📜 <b>REFERRAL PROGRAM RULES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ <b>Unique Users:</b> Only new users who have never used the bot count.\n\n"
        "2️⃣ <b>No Self-Refer:</b> Creating multiple accounts to refer yourself will lead to a <b>Permanent Ban</b>. 🚫\n\n"
        "3️⃣ <b>Fake Referrals:</b> Use of bots or fake accounts is strictly prohibited.\n\n"
        "4️⃣ <b>Rewards:</b> Once you reach the goal, click 'Claim Reward' to get your voucher instantly.\n\n"
        "✅ <i>Fair play ensures everyone gets their rewards!</i>"
    )
    await callback.message.edit_text(rules, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="back_to_refer")]]))
    await callback.answer()

@router.callback_query(F.data == "view_leaderboard")
async def view_leaderboard_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    from database.requests import get_top_referrers
    top_users = await get_top_referrers(session)
    
    if not top_users:
        return await callback.answer("🏆 Leaderboard is empty. Start referring to lead!", show_alert=True)
        
    text = "🏆 <b>TOP REFERRAL LEADERS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, user in enumerate(top_users, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "▪️"
        # Mask name for privacy if it's too long
        name = user.full_name[:15] + ".." if len(user.full_name) > 15 else user.full_name
        text += f"{medal} <b>{name}</b> — <code>{user.referral_count}</code> Invites\n"
        
    text += "\n━━━━━━━━━━━━━━━━━━━━━━\n🚀 <i>Can you make it to the top 10?</i>"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="back_to_refer")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "back_to_refer")
async def back_to_refer_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    await show_referral_menu(callback.message, session, callback.from_user.id)
    await callback.answer()

@router.callback_query(F.data == "view_referral_link")
async def view_referral_link_cb(callback: CallbackQuery):
    bot_me = await callback.bot.get_me(); link = f"https://t.me/{bot_me.username}?start={callback.from_user.id}"
    await callback.message.answer(f"🔗 <b>YOUR INVITE LINK</b>\n\n<code>{link}</code>\n\n<i>Share this. Once your friends join, your progress is updated automatically!</i>"); await callback.answer()

@router.callback_query(F.data == "my_redeemed_rewards")
async def my_redeemed_rewards_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    rewards = await get_user_redeemed_rewards(session, callback.from_user.id)
    if not rewards: return await callback.answer("❌ No rewards redeemed yet.", show_alert=True)
    text = "🎟️ <b>REDEEMED VOUCHERS</b>\n\n"
    for r in rewards: text += f"▪️ <code>{r.code}</code> ({r.created_at.strftime('%d %b')})\n"
    await callback.message.answer(text); await callback.answer()

@router.callback_query(F.data == "redeem_referral")
async def redeem_referral_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    user = await get_user_by_id(session, callback.from_user.id); goal = int(await get_setting(session, "refer_goal", "3"))
    if user.referral_count < goal: return await callback.answer(f"❌ Need {goal} referrals!", show_alert=True)
    reward = await get_available_reward(session)
    if not reward: return await callback.answer("❌ Reward pool empty. Admin notified!", show_alert=True)
    await use_reward(session, reward.id, user.id); await update_user(session, user.id, referral_count=user.referral_count - goal)
    await callback.message.answer(f"🎉 <b>SUCCESS!</b>\n\nYour reward voucher: <code>{reward.code}</code>\n\nUse it now! 🚀")
    await callback.answer("Claimed!"); await callback.message.delete()

@router.message(F.text == "🔄 Recover Voucher")
async def recover_voucher_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(UserStates.recover_order_id)
    await message.answer("🔍 <b>Voucher Recovery</b>\n\nTo recover your codes or check order status, please send your <b>Order ID</b> or <b>UTR Number</b>:")

@router.message(UserStates.recover_order_id)
async def recover_order_process(message: Message, state: FSMContext, bot: Bot, session: AsyncIOMotorDatabase):
    if message.text in MENU_BUTTONS: await state.clear(); return
    text = message.text.strip() if message.text else ""
    
    # Pre-process: Extract UTR if present
    utr_match = re.search(r'\d{12}', text)
    if utr_match: search_term = utr_match.group(0)
    else: search_term = text

    tx = await find_transaction_robust(session, search_term)
    if tx and tx.user_id != message.from_user.id:
        count = await increment_user_warning(session, message.from_user.id); await state.clear()
        if count >= 5: await message.answer("🚫 <b>BANNED</b>\n\nFraudulent intent detected."); return
        return await message.answer(f"🚨 <b>SECURITY VIOLATION</b>\n\nThat ID does not belong to you. Attempt logged. ({count}/5 strikes)")
    
    if not tx: return await message.answer(f"❌ <b>ORDER NOT FOUND</b>\n\nCheck your ID and re-send. Make sure you are using the correct Order ID (SHN-...) or UTR.")
    
    tx, items = await get_transaction_with_items(session, tx.id)
    order_id = tx.provider_payment_charge_id or f'TXN-{tx.id}'; status = tx.status; await state.clear()
    
    if status == 'completed':
        codes = "\n".join([f"   ▪️ {cat.name}: <code>{c.code}</code>" for c, cat in items])
        await message.answer(f"✅ <b>VOUCHER RECOVERED</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>ID:</b> <code>{order_id}</code>\n💰 <b>Value:</b> ₹{tx.amount}\n━━━━━━━━━━━━━━━━━━━━━━\n🎫 <b>CODES:</b>\n{codes}\n━━━━━━━━━━━━━━━━━━━━━━\n🚀 <i>Happy Shopping!</i>")
    elif status == 'pending' and tx.payment_proof_id and ("UTR:" in tx.payment_proof_id):
        await message.answer(f"⏳ <b>UNDER VERIFICATION</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>ID:</b> <code>{order_id}</code>\n━━━━━━━━━━━━━━━━━━━━━━\n✅ Proof received.\n⏳ Admin is verifying (5-15 mins).\n\n<i>You'll receive codes as soon as approved!</i>")
    elif status == 'pending':
        # Check if we have a partial proof (screenshot but no UTR)
        photo_id = None
        if tx.payment_proof_id and tx.payment_proof_id.startswith("FILE:"):
            photo_id = tx.payment_proof_id.split(":", 1)[1]
            await state.update_data(order_id=order_id, tx_id=tx.id, total_price=float(tx.amount), photo_id=photo_id)
            await state.set_state(UserStates.waiting_for_utr)
            await message.answer(f"🔄 <b>RESUME: UTR REQUIRED</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>ID:</b> <code>{order_id}</code>\n━━━━━━━━━━━━━━━━━━━━━━\n\n✅ Screenshot was saved.\n📍 <b>Please send your 12-digit UTR now:</b>")
        else:
            await state.update_data(order_id=order_id, tx_id=tx.id, total_price=float(tx.amount), quantity=tx.quantity, cat_name="(reserved order)", cat_id=None); await state.set_state(UserStates.waiting_for_payment_screenshot)
            img_path = os.getenv("PAYMENT_METHOD_IMAGE_PATH", "assets/payment.jpg"); cap = f"🔄 <b>RESUME CHECKOUT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>ID:</b> <code>{order_id}</code>\n💰 <b>Payable:</b> <b>₹{tx.amount}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n1️⃣ Scan QR & Pay\n2️⃣ Upload Screenshot\n3️⃣ Send UTR\n\n<i>Pay quickly to keep your stock!</i>"
            if os.path.exists(img_path): await message.answer_photo(photo=FSInputFile(img_path), caption=cap, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_order_tx_{tx.id}")]]))
            else: await message.answer(cap, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_order_tx_{tx.id}")]]))
    else: await message.answer(f"❌ <b>STATUS: {status.upper()}</b>\n\nThis session has expired. Start a fresh purchase.")

@router.message(F.text == "🆘 Help & Support")
async def user_faq(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    contacts = await get_support_contacts(session)
    text = (f"💡 <b>SHEIN SHOP HELP CENTER</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n💎 <b>How to Buy?</b> Select package → Pay → Submit UTR → Get Codes.\n\n💳 <b>Payments:</b> We accept all UPI apps. Scan & Pay the exact total.\n\n🔢 <b>UTR:</b> The 12-digit number from your payment confirmation.\n\n🎥 <b>Policy:</b> You <u>MUST</u> have an uncut screen recording to get support.\n\n🔄 <b>Lost Codes?</b> Use 'Recover Voucher' with your Order ID.\n\n━━━━━━━━━━━━━━━━━━━━━━\n👇 <b>Select a support agent below:</b>")
    kb = [[InlineKeyboardButton(text=f"💬 {c.label}", url=f"https://t.me/{c.username}")] for c in contacts]
    if not kb: kb = [[InlineKeyboardButton(text="💬 Helpdesk Bot", url="https://t.me/helpdesk_coupon_bot")]]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "📢 Join Channel")
async def user_channel(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    channels = await get_channels(session)
    if not channels: return await message.answer("📢 <b>OFFICIAL CHANNELS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nStay tuned! We are setting up our official communication hubs. 🔄")
    text = (f"📢 <b>OFFICIAL ANNOUNCEMENT CHANNELS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nDon't miss out! Join our community to stay ahead with:\n\n⚡ <b>Instant Restock Alerts:</b> Buy before stock ends.\n🎁 <b>Exclusive Giveaways:</b> Free vouchers for members.\n🔥 <b>Secret Discount Codes:</b> Flash sales & extra % off.\n🚀 <b>Shop Updates:</b> Be the first to know about new stock.\n\n━━━━━━━━━━━━━━━━━━━━━━\n👇 <b>Select a channel below to join:</b>")
    kb = [[InlineKeyboardButton(text=f"📢 {ch.name.upper()}", url=ch.invite_link)] for ch in channels]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "👤 My Profile")
async def user_profile(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    user_q   = get_user_by_id(session, message.from_user.id)
    counts_q = get_user_order_counts(session, message.from_user.id)
    user, (bought, spent) = await asyncio.gather(user_q, counts_q)
    if not user:
        return await message.answer("❌ Error. Try /start")
    text = (f"👤 <b>USER DASHBOARD</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📛 <b>Name:</b> {message.from_user.full_name}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"📅 <b>Joined:</b> {user.created_at.strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛍️ <b>Successful Orders:</b> {bought}\n"
            f"💸 <b>Total Invested:</b> ₹{float(spent):.2f}\n"
            f"👥 <b>Total Referrals:</b> {user.referral_count}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Status:</b> {'🔴 Blacklisted' if user.is_blocked else '⚠️ Flagged' if user.is_suspicious else '🟢 Active'}")
    await message.answer(text)
