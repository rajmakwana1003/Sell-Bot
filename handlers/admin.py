import os
import asyncio
from datetime import datetime, timedelta
from aiogram import Router, F, Bot
from aiogram.filters import Command, Filter
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from motor.motor_asyncio import AsyncIOMotorDatabase
from database.db import AsyncSessionLocal
from database.requests import (
    add_category, add_coupon, get_categories, get_transaction, get_coupon, 
    finalize_sale, update_transaction, get_all_users, get_stats, 
    get_channels, add_channel_db, delete_channel_db, get_setting, set_setting,
    delete_category_db, delete_coupon_db, get_all_coupons_by_category,
    get_available_coupons, get_user_by_id, update_category_price,
    find_transaction_robust, get_coupons_by_transaction, update_category_terms,
    update_category_name, update_category_description,
    delete_all_coupons_by_category,
    get_referral_rewards, add_referral_reward, delete_referral_reward,
    get_blocked_users, get_suspicious_users, get_order_dashboard_stats,
    get_referral_reward_by_id, get_category_with_stock, get_category_price,
    get_category, get_admins, set_admin_status, get_user_full_history,
    get_users_with_purchases, get_transactions_by_status, get_pending_transactions,
    get_transaction_with_items, purge_cancelled_transactions, get_channel,
    toggle_category_status, reset_user_warnings, toggle_support_status,
    add_support_contact, delete_support_contact, get_all_support_contacts
)

router = Router()
# Load Super Admins from .env (Manual Management)
SUPER_ADMINS = [int(id.strip()) for id in os.getenv("ADMIN_ID", "0").split(",") if id.strip()]

class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id = event.from_user.id
        if user_id in SUPER_ADMINS:
            return True
            
        # Fast check using the Global Cache populated by the middleware
        from middlewares.checks import USER_CACHE
        cached = USER_CACHE.get(user_id)
        if cached and cached.get("is_admin"):
            return True
            
        # Fallback if cache missed
        async with AsyncSessionLocal() as session:
            user = await get_user_by_id(session, user_id)
            return bool(user and user.is_admin)

router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

class AdminStates(StatesGroup):
    add_category_name = State()
    add_category_desc = State()
    add_category_terms = State()
    add_coupon_category = State()
    add_coupon_code = State()
    add_coupon_price = State()
    broadcast_msg = State()
    add_ch_name = State()
    add_ch_id = State()
    add_ch_link = State()
    lookup_tx = State()
    search_user = State()
    edit_user_ref = State()
    set_ref_goal = State()
    edit_cat_price = State()
    edit_cat_terms = State()
    edit_cat_name = State()
    edit_cat_desc = State()
    add_reward_code = State()
    edit_support_link = State()
    add_support_label = State()
    add_support_user = State()

def admin_main_keyboard(pending_count: int = 0):
    pending_text = f" ({pending_count})" if pending_count > 0 else ""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Statistics"), KeyboardButton(text="🎫 All Vouchers")],
        [KeyboardButton(text="👥 Manage Users"), KeyboardButton(text="📦 Orders Dashboard")],
        [KeyboardButton(text=f"⏳ Pending Orders{pending_text}"), KeyboardButton(text="📣 Broadcast")],
        [KeyboardButton(text="🔍 Lookup Order"), KeyboardButton(text="⚙️ Bot Settings")],
        [KeyboardButton(text="🚪 Exit Admin")]
    ], resize_keyboard=True)

def is_menu_button(text: str):
    if text.startswith("⏳ Pending Orders"): return True
    buttons = ["📊 Statistics", "🎫 All Vouchers", "👥 Manage Users", "📦 Orders Dashboard", "📣 Broadcast", "🔍 Lookup Order", "⚙️ Bot Settings", "🚪 Exit Admin"]
    return text in buttons

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    stats = await get_order_dashboard_stats(session)
    pending = stats["pending"]
    await message.answer("🛠️ <b>Admin Control Panel</b>", reply_markup=admin_main_keyboard(pending))

@router.message(Command("cancel"))
@router.message(F.text.casefold() == "cancel")
async def cancel_handler(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    stats = await get_order_dashboard_stats(session)
    pending = stats["pending"]
    await message.answer("❌ <b>Cancelled.</b>", reply_markup=admin_main_keyboard(pending))

# --- Settings ---
@router.message(F.text == "⚙️ Bot Settings")
async def admin_settings(message: Message, session: AsyncIOMotorDatabase, state: FSMContext = None):
    if state:
        await state.clear()
    
    m_mode = await get_setting(session, "maintenance_mode", "off")
    r_mode = await get_setting(session, "refer_earn_status", "on")
    r_goal = await get_setting(session, "refer_goal", "3")
    
    m_status = "🔴 ON" if m_mode == "on" else "🟢 OFF"
    r_status = "🟢 ON" if r_mode == "on" else "🔴 OFF"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🛠️ Maintenance: {m_status}", callback_data="toggle_m_mode")],
        [InlineKeyboardButton(text="📢 Channel Manager", callback_data="admin_channels")],
        [InlineKeyboardButton(text=f"🎁 Referral Event", callback_data="admin_rewards_main_cb")],
        [InlineKeyboardButton(text=f"🎁 Referral System: {r_status}", callback_data="toggle_r_mode")],
        [InlineKeyboardButton(text=f"🎯 Set Ref Goal: {r_goal}", callback_data="admin_set_ref_goal")],
        [InlineKeyboardButton(text="📞 Support Settings", callback_data="admin_support_settings")],
        [InlineKeyboardButton(text="🚫 Blocked Users", callback_data="admin_blocked_list")],
        [InlineKeyboardButton(text="🔑 Admin Manager", callback_data="admin_list_managers")],
        [InlineKeyboardButton(text="🔙 Close", callback_data="admin_close_settings")]
    ])
    await message.answer("⚙️ <b>Advanced Bot Settings</b>", reply_markup=keyboard)

# --- Referral Rewards (Event) ---
@router.callback_query(F.data == "admin_rewards_main_cb")
async def admin_rewards_main_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await admin_rewards_main(callback.message, state, session)
    await callback.answer()

@router.message(F.text.startswith("⏳ Pending Orders"))
async def admin_pending_btn_handler(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await view_pending_orders(message, state, session)

@router.callback_query(F.data == "admin_close_settings")
async def close_settings_cb(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data == "toggle_m_mode")
async def toggle_m_mode_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    curr = await get_setting(session, "maintenance_mode", "off")
    new = "on" if curr == "off" else "off"
    await set_setting(session, "maintenance_mode", new)
    await admin_settings(callback.message, session, state)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data == "toggle_r_mode")
async def toggle_r_mode_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    curr = await get_setting(session, "refer_earn_status", "on")
    new = "off" if curr == "on" else "on"
    await set_setting(session, "refer_earn_status", new)
    await admin_settings(callback.message, session, state)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data == "admin_set_ref_goal")
async def set_ref_goal_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_ref_goal)
    await callback.message.answer("🎯 <b>Referral Goal</b>\nEnter number (or '/cancel'):")
    await callback.answer()

@router.message(AdminStates.set_ref_goal)
async def set_ref_goal_proc(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    if is_menu_button(message.text):
        await state.clear()
        return
    try:
        goal = int(message.text)
        await set_setting(session, "refer_goal", str(goal))
    except:
        return await message.answer("❌ Invalid number.")
    await state.clear()
    await message.answer(f"✅ Goal set to {goal}!")
    await admin_settings(message, session, state)

# --- Referral Rewards (Event) ---
@router.message(F.text == "🎁 Referral Event")
async def admin_rewards_main(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    rewards = await get_referral_rewards(session)
    text = "🎁 <b>Referral Event Reward Pool</b>\n\n"
    keyboard = []
    if rewards:
        for r in rewards[:15]:
            status = "🔴 USED" if r.is_used else "🟢 OK"
            text += f"▪️ <code>{r.code}</code> ({status})\n"
            if not r.is_used:
                keyboard.append([InlineKeyboardButton(text=f"🗑 Delete {r.code}", callback_data=f"admin_del_reward_ask_{r.id}")])
    else: text += "❌ No codes in pool."
    keyboard.append([InlineKeyboardButton(text="➕ Add Reward Codes", callback_data="admin_add_reward")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data == "admin_add_reward")
async def add_reward_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_reward_code)
    await callback.message.answer("🎁 <b>Import Referral Rewards</b>\n━━━━━━━━━━━━━━━━━━\n\n1️⃣ Paste codes (comma or line separated)\n2️⃣ <b>OR</b> Upload a <code>.txt</code> file\n\n<i>Users will get 1 random code from this pool when they hit the invite goal.</i>")
    await callback.answer()

@router.message(AdminStates.add_reward_code)
async def add_reward_proc(message: Message, state: FSMContext, bot: Bot, session: AsyncIOMotorDatabase):
    if message.text and (is_menu_button(message.text) or message.text.casefold() == "/cancel"): 
        await state.clear(); return
        
    valid_codes = []
    
    # Handle File Upload
    if message.document:
        if not message.document.file_name.endswith(".txt"):
            return await message.answer("❌ <b>Invalid File!</b> Please only upload <code>.txt</code> files.")
        try:
            import io
            file_in_memory = io.BytesIO()
            await bot.download(message.document, destination=file_in_memory)
            content = file_in_memory.getvalue().decode('utf-8')
            raw_codes = content.replace(",", "\n").split("\n")
            valid_codes = [c.strip() for c in raw_codes if c.strip()]
        except Exception as e:
            return await message.answer(f"❌ <b>Error parsing file:</b> {str(e)}")
            
    # Handle Text Input
    elif message.text:
        raw_codes = message.text.replace(",", "\n").split("\n")
        valid_codes = [c.strip() for c in raw_codes if c.strip()]
        
    if not valid_codes:
        return await message.answer("❌ <b>No valid codes found!</b> Try again or send '/cancel'.")

    for code in valid_codes: 
        await add_referral_reward(session, code)
    
    await state.clear()
    await message.answer(f"✅ Successfully added <b>{len(valid_codes)}</b> free vouchers to the referral reward pool!")
    await admin_rewards_main(message, state, session)

@router.callback_query(F.data.startswith("admin_del_reward_ask_"))
async def del_reward_confirm_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    r_id = int(callback.data.split("_")[-1])
    reward = await get_referral_reward_by_id(session, r_id)
    
    if not reward: return await callback.answer("❌ Not found.")
    
    text = f"⚠️ <b>Delete Reward Code?</b>\n\nCode: <code>{reward.code}</code>\n\nAre you sure you want to delete this reward from the pool?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 YES, DELETE", callback_data=f"admin_del_reward_exec_{r_id}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="admin_rewards_back")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data == "admin_rewards_back")
async def admin_rewards_back_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await admin_rewards_main(callback.message, state, session)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_reward_exec_"))
async def del_reward_exec_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    r_id = int(callback.data.split("_")[-1])
    await delete_referral_reward(session, r_id)
    await callback.answer("✅ Reward deleted.")
    await admin_rewards_main(callback.message, state, session)
    await callback.message.delete()

# --- Voucher Management ---
@router.message(F.text == "🎫 All Vouchers")
async def admin_vouchers(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    categories = await get_categories(session)
    keyboard = []
    if categories:
        for cat in categories:
            keyboard.append([InlineKeyboardButton(text=f"📁 {cat.name}", callback_data=f"admin_manage_cat_{cat.id}")])
    keyboard.append([InlineKeyboardButton(text="➕ New Category", callback_data="admin_add_category")])
    await message.answer("🎫 <b>Inventory Control</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data == "admin_add_category")
async def add_category_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_category_name)
    await callback.message.answer("📁 <b>New Category</b>\nEnter the category name (or '/cancel'):")
    await callback.answer()

@router.message(AdminStates.add_category_name)
async def add_category_name_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    await state.update_data(cat_name=message.text)
    await state.set_state(AdminStates.add_category_desc)
    await message.answer("📝 Enter a short description (or send '-' to skip):")

@router.message(AdminStates.add_category_desc)
async def add_category_desc_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    desc = None if message.text.strip() == '-' else message.text
    await state.update_data(cat_desc=desc)
    await state.set_state(AdminStates.add_category_terms)
    await message.answer("📜 Enter custom terms for this category (or send '-' to use default terms):")

@router.message(AdminStates.add_category_terms)
async def add_category_terms_proc(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    if is_menu_button(message.text): await state.clear(); return
    terms = None if message.text.strip() == '-' else message.text
    data = await state.get_data()
    cat = await add_category(session, data['cat_name'], data.get('cat_desc'), terms)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Notify All Users", callback_data=f"admin_notify_cat_new_{cat.id}")],
        [InlineKeyboardButton(text="✅ Done", callback_data="admin_vouchers_back")]
    ])
    await message.answer(f"✅ Category <b>{cat.name}</b> created!\n\nNow set the price: Click the category → Edit Price.", reply_markup=kb)

@router.callback_query(F.data.startswith("admin_manage_cat_"))
async def manage_cat(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    cat_id = int(callback.data.split("_")[-1])
    avail, pend, cat = await get_category_with_stock(session, cat_id)
    
    if not cat: return
    
    status_text = "🟢 Active" if cat.is_active else "🔴 Hidden/Inactive"
    toggle_btn_text = "🚫 Disable/Hide" if cat.is_active else "✅ Enable/Show"
    
    keyboard = [
        [InlineKeyboardButton(text="👁️ View/Delete Codes", callback_data=f"admin_view_codes_{cat_id}")],
        [InlineKeyboardButton(text="➕ Add New Stock", callback_data=f"admin_sel_cat_{cat_id}")],
        [InlineKeyboardButton(text="💰 Edit Price", callback_data=f"admin_edit_price_{cat_id}"), InlineKeyboardButton(text="📝 Edit Name", callback_data=f"admin_edit_name_{cat_id}")],
        [InlineKeyboardButton(text="📜 Edit Terms", callback_data=f"admin_edit_terms_{cat_id}"), InlineKeyboardButton(text="📖 Edit Desc", callback_data=f"admin_edit_desc_{cat_id}")],
        [InlineKeyboardButton(text=toggle_btn_text, callback_data=f"admin_toggle_cat_{cat_id}")],
        [InlineKeyboardButton(text="🗑 Delete All Codes", callback_data=f"admin_clear_codes_confirm_{cat_id}")],
        [InlineKeyboardButton(text="🗑 Delete Category", callback_data=f"admin_del_cat_ask_{cat_id}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_vouchers_back")]
    ]
    
    text = (
        f"📂 <b>Category: {cat.name}</b>\n\n"
        f"📝 <b>Desc:</b> {cat.description or 'N/A'}\n"
        f"📊 Status: <b>{status_text}</b>\n"
        f"🟢 Available: <b>{avail or 0}</b>\n"
        f"⏳ Pending: <b>{pend or 0}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>(Auto-release 10 mins)</i>"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("admin_toggle_cat_"))
async def admin_toggle_cat_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    cat_id = int(callback.data.split("_")[-1])
    from database.requests import toggle_category_status
    cat = await toggle_category_status(session, cat_id)
    
    await callback.answer(f"✅ {cat.name} is now {'Active' if cat.is_active else 'Hidden'}")
    await manage_cat(callback, session)

@router.callback_query(F.data.startswith("admin_sel_cat_"))
async def add_stock_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[-1])
    await state.update_data(target_cat_id=cat_id)
    await state.set_state(AdminStates.add_coupon_code)
    await callback.message.answer("➕ <b>Add Codes</b>\n━━━━━━━━━━━━━━━━━━\n\n1️⃣ Paste codes (comma or line separated)\n2️⃣ <b>OR</b> Upload a <code>.txt</code> file\n\n<i>Type '/cancel' to abort.</i>")
    await callback.answer()

@router.message(AdminStates.add_coupon_code)
async def process_coupon_code(message: Message, state: FSMContext, bot: Bot):
    if message.text and (is_menu_button(message.text) or message.text.casefold() == "/cancel"): 
        await state.clear(); return
    
    data = await state.get_data()
    cat_id = data.get('target_cat_id')
    if not cat_id: return await message.answer("❌ Error: No category selected.")

    valid_codes = []
    
    # Handle File Upload
    if message.document:
        if not message.document.file_name.endswith(".txt"):
            return await message.answer("❌ <b>Invalid File!</b> Please only upload <code>.txt</code> files.")
        
        try:
            import io
            file_in_memory = io.BytesIO()
            await bot.download(message.document, destination=file_in_memory)
            content = file_in_memory.getvalue().decode('utf-8')
            raw_codes = content.replace(",", "\n").split("\n")
            valid_codes = [c.strip() for c in raw_codes if c.strip()]
        except Exception as e:
            return await message.answer(f"❌ <b>Error parsing file:</b> {str(e)}")
            
    # Handle Text Input
    elif message.text:
        raw_codes = message.text.replace(",", "\n").split("\n")
        valid_codes = [c.strip() for c in raw_codes if c.strip()]
    
    if not valid_codes:
        return await message.answer("❌ <b>No valid codes found!</b> Try again or send '/cancel'.")

    async with AsyncSessionLocal() as session:
        cat = await get_category(session, cat_id)
        if not cat: return await message.answer("❌ Category not found.")
        # Check if category already has an existing price to show as suggestion
        existing_price = await get_category_price(session, cat_id)

    # Save codes in state and ask for price
    await state.update_data(pending_codes=valid_codes, pending_cat_name=cat.name)
    await state.set_state(AdminStates.add_coupon_price)

    hint = f"\n💡 Current price: <b>₹{existing_price}</b> — type <b>same</b> to keep it." if existing_price else ""
    await message.answer(
        f"📦 <b>{len(valid_codes)} code(s) detected.</b>\n\n"
        f"💰 <b>Enter price (INR) for these coupons:</b>{hint}"
    )

@router.message(AdminStates.add_coupon_price)
async def add_coupon_price_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    data = await state.get_data()
    cat_id = data.get('target_cat_id')
    valid_codes = data.get('pending_codes', [])
    cat_name = data.get('pending_cat_name', '')

    # Resolve price
    if message.text.strip().lower() == 'same':
        # Re-fetch existing price
        async with AsyncSessionLocal() as session:
            price = await get_category_price(session, cat_id)
            if price is None: price = 0.0
    else:
        try:
            price = float(message.text.strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            return await message.answer("❌ Invalid price. Enter a positive number (e.g. <b>99</b> or <b>149.50</b>):")

    # Bulk insert
    async with AsyncSessionLocal() as session:
        for code in valid_codes:
            await add_coupon(session, cat_id, code, price)

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Notify All Users", callback_data=f"admin_notify_stock_{cat_id}_{len(valid_codes)}")],
        [InlineKeyboardButton(text="✅ Done", callback_data="admin_vouchers_back")]
    ])
    await message.answer(f"✅ Added <b>{len(valid_codes)}</b> codes to <b>{cat_name}</b> at <b>₹{price}</b> each.", reply_markup=kb)

@router.callback_query(F.data.startswith("admin_edit_price_"))
async def edit_price_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        curr_price = await get_category_price(session, cat_id) or 0
        cat = await get_category(session, cat_id)
        cat_name = cat.name if cat else "Category"
        
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminStates.edit_cat_price)
    await callback.message.answer(f"💰 <b>Update Price: {cat_name}</b>\n\n<b>Current Price:</b> ₹{curr_price}\n\n<i>Enter new price in INR (or '/cancel'):</i>")
    await callback.answer()

@router.message(AdminStates.edit_cat_price)
async def edit_price_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    try:
        new_price = float(message.text)
        if new_price <= 0: raise ValueError
    except: return await message.answer("❌ Invalid price. Enter a positive number:")
    
    data = await state.get_data()
    cat_id = data.get('edit_cat_id')
    
    async with AsyncSessionLocal() as session:
        cat = await get_category(session, cat_id)
        old_price = await get_category_price(session, cat_id) or 0
    
    await state.update_data(new_price=new_price, old_price=old_price)
    
    text = (
        f"💰 <b>Confirm Price Update</b>\n\n"
        f"Category: <b>{cat.name}</b>\n"
        f"Old Price: ₹{old_price}\n"
        f"New Price: <b>₹{new_price}</b>\n\n"
        f"Apply this change to all UNSOLD coupons?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM UPDATE", callback_data=f"admin_edit_price_exec")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_manage_cat_{cat_id}")]
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "admin_edit_price_exec")
async def edit_price_exec(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cat_id = data.get('edit_cat_id')
    new_price = data.get('new_price')
    old_price = data.get('old_price')
    
    async with AsyncSessionLocal() as session:
        await update_category_price(session, cat_id, new_price)
        cat = await get_category(session, cat_id)
        cat_name = cat.name if cat else "Category"
        
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Notify All Users", callback_data=f"admin_notify_price_{cat_id}_{new_price}_{old_price}")],
        [InlineKeyboardButton(text="✅ Done", callback_data="admin_vouchers_back")]
    ])
    await callback.message.edit_text(f"✅ Price for <b>{cat_name}</b> updated to <b>₹{new_price}</b>!", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("admin_edit_terms_"))
async def edit_terms_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminStates.edit_cat_terms)
    await callback.message.answer("📜 <b>Update Terms</b>\nEnter new terms for this category (or type '/cancel'):")
    await callback.answer()

@router.message(AdminStates.edit_cat_terms)
async def edit_terms_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    data = await state.get_data()
    cat_id = data.get('edit_cat_id')
    
    await state.update_data(new_terms=message.text)
    text = f"📜 <b>Confirm Terms Update</b>\n\nNew Terms:\n<i>{message.text}</i>\n\nAre you sure?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM", callback_data="admin_edit_terms_exec")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_manage_cat_{cat_id}")]
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "admin_edit_terms_exec")
async def edit_terms_exec(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await update_category_terms(session, data['edit_cat_id'], data['new_terms'])
    await state.clear()
    await callback.message.edit_text("✅ Terms updated!")
    await callback.answer()
    # No easy way to return to admin_vouchers with Message object from here without re-triggering, 
    # but the success message is enough. User can click menu.

@router.callback_query(F.data.startswith("admin_edit_name_"))
async def edit_cat_name_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminStates.edit_cat_name)
    await callback.message.answer("📝 <b>Edit Category Name</b>\nEnter the new name for this category:")
    await callback.answer()

@router.message(AdminStates.edit_cat_name)
async def edit_cat_name_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    data = await state.get_data()
    cat_id = data.get('edit_cat_id')
    
    await state.update_data(new_name=message.text)
    text = f"📝 <b>Confirm Name Change</b>\n\nNew Name: <b>{message.text}</b>\n\nAre you sure?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM", callback_data="admin_edit_name_exec")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_manage_cat_{cat_id}")]
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "admin_edit_name_exec")
async def edit_name_exec(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await update_category_name(session, data['edit_cat_id'], data['new_name'])
    await state.clear()
    await callback.message.edit_text(f"✅ Category name updated to: <b>{data['new_name']}</b>")
    await callback.answer()

@router.callback_query(F.data.startswith("admin_edit_desc_"))
async def edit_cat_desc_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminStates.edit_cat_desc)
    await callback.message.answer("📖 <b>Edit Category Description</b>\nEnter the new description (or '-' to clear):")
    await callback.answer()

@router.message(AdminStates.edit_cat_desc)
async def edit_cat_desc_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    data = await state.get_data()
    cat_id = data.get('edit_cat_id')
    desc = None if message.text.strip() == '-' else message.text
    
    await state.update_data(new_desc=desc)
    text = f"📖 <b>Confirm Description Update</b>\n\nNew Description: {desc or '<i>(Empty)</i>'}\n\nAre you sure?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM", callback_data="admin_edit_desc_exec")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_manage_cat_{cat_id}")]
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "admin_edit_desc_exec")
async def edit_desc_exec(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await update_category_description(session, data['edit_cat_id'], data['new_desc'])
    await state.clear()
    await callback.message.edit_text("✅ Category description updated!")
    await callback.answer()

# --- User Management ---
@router.message(F.text == "👥 Manage Users")
async def admin_users_main(message: Message, state: FSMContext):
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Search User", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="📜 User List", callback_data="admin_view_user_list")],
        [InlineKeyboardButton(text="⚠️ Suspicious Users", callback_data="admin_suspicious_users")]
    ])
    await message.answer("👥 <b>User Management</b>", reply_markup=keyboard)

@router.callback_query(F.data == "admin_search_user")
async def search_user_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.search_user)
    await callback.message.answer("🔎 <b>Search User</b>\nEnter Telegram ID (or '/cancel'):")
    await callback.answer()

@router.callback_query(F.data == "admin_list_managers")
async def admin_list_managers_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    from database.requests import get_admins
    admins = await get_admins(session)
    
    text = "🔑 <b>Administrator Manager</b>\n\n"
    text += "🛡 <b>Super Admins (.env):</b>\n"
    for sa in SUPER_ADMINS:
        text += f"▪️ <code>{sa}</code>\n"
    
    text += "\n👥 <b>Promoted Admins (DB):</b>\n"
    keyboard = []
    if not admins:
        text += "<i>No extra admins promoted.</i>"
    else:
        for a in admins:
            text += f"▪️ {a.full_name} (<code>{a.id}</code>)\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ Demote {a.id}", callback_data=f"admin_demote_{a.id}")])
    
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back_to_settings")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("admin_demote_"))
async def admin_demote_cb(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    u_id = int(callback.data.split("_")[-1])
    from database.requests import set_admin_status
    await set_admin_status(session, u_id, False)
    await callback.answer("✅ User demoted to regular user.")
    await admin_list_managers_cb(callback, session)

@router.message(AdminStates.search_user)
async def search_user_proc(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    if is_menu_button(message.text): await state.clear(); return
    try: user_id = int(message.text)
    except: return await message.answer("❌ Numeric ID only.")
    user = await get_user_by_id(session, user_id)
    if not user: return await message.answer("❌ Not found.")
    from database.requests import get_user_full_history
    history = await get_user_full_history(session, user.id)
        
    await state.update_data(target_user_id=user.id)
    block_text = "✅ Unblock User" if user.is_blocked else "🚫 Block User"
    admin_toggle_text = "🔑 Revoke Admin" if user.is_admin else "🔑 Promote to Admin"
    
    keyboard = [
        [InlineKeyboardButton(text="✏️ Edit Referrals", callback_data=f"admin_edit_ref_{user.id}")],
        [InlineKeyboardButton(text="🔄 Reset Warnings", callback_data=f"admin_reset_warn_{user.id}")],
        [InlineKeyboardButton(text=admin_toggle_text, callback_data=f"admin_toggle_admin_{user.id}")],
        [InlineKeyboardButton(text=block_text, callback_data=f"admin_toggle_block_{user.id}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_users_back")]
    ]
    
    status = "🔴 BANNED" if user.is_blocked else "🔑 ADMIN" if user.is_admin else "🟢 ACTIVE"
    if user.id in SUPER_ADMINS: status = "🛡 SUPER ADMIN"
    
    warn_text = f"⚠️ Warnings: {user.warning_count}/5" if user.warning_count > 0 else "✅ No warnings"
    text = f"👤 <b>{user.full_name}</b>\nStatus: {status}\n{warn_text}\nRef: {user.referral_count}\n🆔 ID: <code>{user.id}</code>\n\n🛍 <b>Purchases:</b>\n"
    
    if not history:
        text += "No purchases yet.\n"
    else:
        for tx, c, cat in history[:10]:
            text += f"▪️ [{tx.created_at.strftime('%d %b')}] {cat.name}: <code>{c.code}</code> (TX: <code>{tx.provider_payment_charge_id}</code>)\n"
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("admin_toggle_admin_"))
async def admin_toggle_admin_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    u_id = int(callback.data.split("_")[-1])
    if u_id in SUPER_ADMINS:
        return await callback.answer("❌ Cannot modify Super Admin status via bot.", show_alert=True)
        
    user = await get_user_by_id(session, u_id)
    new_status = not user.is_admin
    from database.requests import set_admin_status
    await set_admin_status(session, u_id, new_status)
    
    await callback.answer(f"✅ User is now {'Admin' if new_status else 'User'}")
    await callback.message.delete()
    msg = callback.message
    msg.text = str(u_id)
    await search_user_proc(msg, state, session)

@router.callback_query(F.data.startswith("admin_reset_warn_"))
async def reset_warn_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    u_id = int(callback.data.split("_")[-1])
    from database.requests import reset_user_warnings
    await reset_user_warnings(session, u_id)
    await callback.answer("✅ Warnings reset to 0.")
    await admin_users_main(callback.message, state)
    await callback.message.delete()

@router.callback_query(F.data == "admin_users_back")
async def admin_users_back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear() 
    await admin_users_main(callback.message, state)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data.startswith("admin_toggle_block_"))
async def toggle_block_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    u_id = int(callback.data.split("_")[-1])
    user = await get_user_by_id(session, u_id)
    new_status = not user.is_blocked
    await session.users.update_one({"_id": u_id}, {"$set": {"is_blocked": new_status}})
    await callback.answer("Status updated!")
    await callback.message.delete()
    await admin_users_main(callback.message, state)

@router.callback_query(F.data.startswith("admin_edit_ref_"))
async def edit_ref_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.edit_user_ref)
    await callback.message.answer("✏️ <b>Enter new count:</b>")
    await callback.answer()

@router.message(AdminStates.edit_user_ref)
async def edit_ref_proc(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    if is_menu_button(message.text): await state.clear(); return
    data = await state.get_data()
    try: count = int(message.text)
    except: return await message.answer("❌ Invalid.")
    await session.users.update_one({"_id": data['target_user_id']}, {"$set": {"referral_count": count}})
    await state.clear()
    await message.answer(f"✅ Updated to {count}")
    await admin_users_main(message, state)

@router.callback_query(F.data == "admin_view_user_list")
async def view_users_list_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    users = await get_users_with_purchases(session)
    text = "👥 <b>Users Log:</b>\n\n"
    for u in users[:20]: text += f"▪️ {u.full_name} | ID: <code>{u.id}</code> | Buy: {u.bought}\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_users_back")]]))
    await callback.answer()

@router.callback_query(F.data == "admin_suspicious_users")
async def suspicious_users_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    from database.requests import get_suspicious_users
    users = await get_suspicious_users(session)
    
    text = "⚠️ <b>Suspicious Users (Payment Abuse):</b>\n\n"
    if not users:
        text += "No suspicious users found."
    else:
        for u in users[:20]:
            status = "🔴 BANNED" if u.is_blocked else "🟢 ACTIVE"
            text += f"▪️ <b>{u.full_name}</b> (<code>{u.id}</code>) | Warns: <b>{u.warning_count}/5</b> | {status}\n"
            
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_users_back")]]))
    await callback.answer()

@router.callback_query(F.data.startswith("admin_notify_"))
async def admin_notify_update_cb(callback: CallbackQuery, bot: Bot, session: AsyncIOMotorDatabase):
    parts = callback.data.split("_")
    action = parts[2]
    cat_id = int(parts[4] if action == "cat" else parts[3])
    
    cat = await get_category(session, cat_id)
    if not cat:
        return await callback.answer("❌ Category not found.", show_alert=True)
    cat_name = cat.name.upper()
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 SHOP NOW / BUY", callback_data=f"agree_terms_{cat_id}")]
    ])
    
    if action == "cat":
        msg = (
            f"💎 <b>PREMIUM COLLECTION ADDED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔥 We are excited to announce a brand new collection: <b>{cat_name}</b>!\n\n"
            f"✨ <b>Exclusive access is now OPEN.</b>\n"
            f"Tap the button below to browse the new stock and grab yours before anyone else! 🚀"
        )
    elif action == "stock":
        stock_count = parts[4]
        msg = (
            f"⚡ <b>MEGA RESTOCK COMPLETED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>Category:</b> <b>{cat_name}</b>\n"
            f"💎 <b>New Units Added:</b> 🔥 <u><b>{stock_count} CODES</b></u> 🔥\n\n"
            f"Our most popular vouchers are back in stock! Demand is high—don't wait for the next sell-out. 🚀"
        )
    elif action == "price":
        new_price = parts[4]
        old_price = parts[5] if len(parts) > 5 else "???"
        msg = (
            f"💰 <b>PRICE DROP ALERT!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Good news! We've just updated the rates for <b>{cat_name}</b>.\n\n"
            f"📉 <b>Old Price:</b> <s>₹{old_price}</s>\n"
            f"💵 <b>NEW PRICE:</b> 🔥 <u><b>₹{new_price} ONLY</b></u> 🔥\n\n"
            f"Premium quality at even better value. Grab your vouchers at the new rate now! 💸"
        )
    else:
        return await callback.answer("Invalid action.")
        
    await callback.answer("Starting Premium Broadcast...")
    await callback.message.edit_reply_markup(reply_markup=None) 
    
    users = await get_all_users(session)
    
    count = 0
    m = await callback.message.answer(f"⏳ Broadcasting PREMIUM update to {len(users)} users...")
    
    for u in users:
        try:
            await bot.send_message(u.id, msg, reply_markup=kb)
            count += 1
            await asyncio.sleep(0.04) 
        except:
            pass
            
    await m.edit_text(f"✅ Premium Broadcast complete! Sent to <b>{count}</b> users.")

# --- Statistics & Broadcast ---
@router.message(F.text == "📊 Statistics")
async def admin_stats_msg(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    s = await get_stats(session)
    from database.requests import get_advanced_stats
    adv = await get_advanced_stats(session)
        
    text = (
        f"📊 <b>SHOP PERFORMANCE REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>FINANCIAL ANALYTICS:</b>\n"
        f"├─ Today: <b>₹{adv['today']:.2f}</b>\n"
        f"├─ Yesterday: <b>₹{adv['yesterday']:.2f}</b>\n"
        f"├─ This Month: <b>₹{adv['month']:.2f}</b>\n"
        f"└─ Total Revenue: <b>₹{adv['total']:.2f}</b>\n\n"

        f"👥 <b>USER BASE:</b>\n"
        f"├─ Total: <b>{s['users']['total']}</b>\n"
        f"├─ Active: <b>{s['users']['total'] - s['users']['blocked']}</b>\n"
        f"├─ Blocked: <b>{s['users']['blocked']}</b>\n"
        f"└─ Admins: <b>{s['users']['admins']}</b>\n\n"
        
        f"🎫 <b>INVENTORY SUMMARY:</b>\n"
        f"├─ Total: <b>{s['coupons']['total']}</b>\n"
        f"├─ Available: <b>{s['coupons']['available']}</b>\n"
        f"└─ Sold: <b>{s['coupons']['sold']}</b>\n\n"
        
        f"📂 <b>CATEGORY BREAKDOWN:</b>\n"
    )
    
    for cat in s['categories']:
        text += (
            f"<b>{cat['name'].upper()}</b>\n"
            f"├─ Stock: <code>{cat['available']}</code> | Sold: <code>{cat['sold']}</code>\n"
            f"└─ Revenue: <b>₹{cat['revenue']:.2f}</b>\n"
        )
    
    text += "━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"🕒 <i>Report generated on {datetime.now().strftime('%d %b, %H:%M')}</i>"
    
    await message.answer(text)

@router.message(F.text == "📣 Broadcast")
async def start_broadcast(message: Message, state: FSMContext):
    await state.clear(); await state.set_state(AdminStates.broadcast_msg)
    await message.answer("📣 <b>PREMIUM BROADCAST</b>\n━━━━━━━━━━━━━━━━━━\n\nSend the message you want to broadcast.\n\n✅ <b>Supports:</b>\n├─ 📝 Text with formatting\n├─ 🖼 Photos\n├─ 📹 Videos\n└─ 📁 Files\n\n<i>Type '/cancel' to abort.</i>")

@router.message(AdminStates.broadcast_msg)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot, session: AsyncIOMotorDatabase):
    if message.text and (is_menu_button(message.text) or message.text.casefold() == "/cancel"): 
        await state.clear(); return

    await state.clear();
    users = await get_all_users(session)
    count = 0; m = await message.answer(f"⏳ Sending to {len(users)} users...")

    for u in users:
        try:
            await bot.copy_message(chat_id=u.id, from_chat_id=message.chat.id, message_id=message.message_id)
            count += 1
            await asyncio.sleep(0.05) # Slightly slower for media safety
        except:
            pass

    await m.edit_text(f"✅ <b>Broadcast complete!</b>\n\nSuccessfully sent to <b>{count}</b> users.")

@router.message(F.text == "🔍 Lookup Order")
async def admin_lookup_start(message: Message, state: FSMContext):
    await state.clear(); await state.set_state(AdminStates.lookup_tx)
    await message.answer("🔍 <b>Global Search</b>\nEnter TX ID or UTR (or '/cancel'):")

@router.message(F.text == "📦 Orders Dashboard")
async def admin_orders_dashboard(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()

    stats = await get_order_dashboard_stats(session)
    today_count = stats["today"]
    yesterday_count = stats["yesterday"]
    pending = stats["pending"]
    completed = stats["completed"]
    failed = stats["failed"]
    cancelled = stats["cancelled"]

    text = (
        "📦 <b>ORDER MANAGEMENT DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 <b>ACTIVITY:</b>\n"
        f"├─ Orders Today: <b>{today_count}</b>\n"
        f"└─ Orders Yesterday: <b>{yesterday_count}</b>\n\n"

        f"🚦 <b>STATUS BREAKDOWN:</b>\n"
        f"├─ ⏳ Pending: <b>{pending}</b>\n"
        f"├─ ✅ Completed: <b>{completed}</b>\n"
        f"├─ ❌ User Errors: <b>{failed}</b>\n"
        f"└─ ⏰ Timeouts: <b>{cancelled}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Manage your orders and client requests:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏳ Manage Pending ({pending})", callback_data="admin_vlist_pending")],
        [InlineKeyboardButton(text="🔍 Search by User ID", callback_data="admin_search_order_user")],
        [InlineKeyboardButton(text="✅ History", callback_data="admin_vlist_completed"), InlineKeyboardButton(text="❌ Errors", callback_data="admin_vlist_failed")],
        [InlineKeyboardButton(text="⏰ Timeouts", callback_data="admin_vlist_cancelled")],
        [InlineKeyboardButton(text="🗑 Purge Cancelled", callback_data="admin_purge_cancelled")],
        [InlineKeyboardButton(text="🔙 Close", callback_data="admin_close_settings")]
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "admin_purge_cancelled")
async def admin_purge_cancelled_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    count = await purge_cancelled_transactions(session)
    
    await callback.answer(f"✅ Cleaned up {count} cancelled records.", show_alert=True)
    await admin_orders_dashboard(callback.message, state, session)
    await callback.message.delete()

@router.callback_query(F.data == "admin_search_order_user")
async def admin_search_order_user_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.search_user) # Re-using search_user state but will handle it in a way that shows orders
    await callback.message.answer("🔍 <b>USER ORDER SEARCH</b>\n\nEnter the <b>Telegram ID</b> of the user to see their full order history:")
    await callback.answer()
@router.callback_query(F.data == "admin_orders_back")
async def admin_orders_back_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await admin_orders_dashboard(callback.message, state, session)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data == "admin_vlist_pending")
async def view_pending_orders_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await view_pending_orders(callback.message, state, session)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data.startswith("admin_vlist_"))
async def view_orders_by_status(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    status = callback.data.split("_")[-1] 
    orders = await get_transactions_by_status(session, status, limit=20)
        
    if not orders:
        return await callback.answer(f"No {status} orders found.", show_alert=True)
        
    status_emoji = "✅" if status == "completed" else "❌" if status == "failed" else "🚫"
    text = f"{status_emoji} <b>{status.upper()} ORDERS (Last 20)</b>\n\n"
    keyboard = []
    for tx in orders:
        btn_text = f"₹{tx.amount} | ID: {tx.provider_payment_charge_id[-8:] if tx.provider_payment_charge_id else tx.id}"
        keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin_lookup_cb_{tx.id}")])
    
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_orders_back")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

async def view_pending_orders(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear()
    pending = await get_pending_transactions(session)
        
    if not pending:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="admin_orders_back")]])
        return await message.answer("✅ <b>No pending orders at the moment!</b>", reply_markup=kb)
        
    text = f"⏳ <b>Pending Orders ({len(pending)})</b>\n\nSelect an order below to view details or approve/reject:"
    keyboard = []
    for tx in pending:
        charge_id = tx.provider_payment_charge_id[-6:] if tx.provider_payment_charge_id else f"ID:{tx.id}"
        btn_text = f"₹{tx.amount} | TXN: {charge_id}"
        keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin_lookup_cb_{tx.id}")])
        
    keyboard.append([InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="admin_orders_back")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("admin_lookup_cb_"))
async def admin_lookup_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    tx_id = int(callback.data.split("_")[-1])
    await send_order_details(callback.message, tx_id, session, is_callback=True)
    await callback.answer()

@router.message(AdminStates.lookup_tx)
async def admin_lookup_proc(message: Message, state: FSMContext, session: AsyncIOMotorDatabase):
    if is_menu_button(message.text): await state.clear(); return
    tx = await find_transaction_robust(session, message.text.strip())
    if not tx: return await message.answer("❌ Order not found.")
    tx_id = tx.id
    await send_order_details(message, tx_id, session, is_callback=False)

async def send_order_details(message_obj: Message, tx_id: int, session: AsyncIOMotorDatabase, is_callback: bool = False):
    tx, items = await get_transaction_with_items(session, tx_id)
    if not tx:
        await message_obj.answer("❌ Order not found.")
        return
            
    user = await get_user_by_id(session, tx.user_id)
        
    status_emoji = "⏳" if tx.status == "pending" else "✅" if tx.status == "completed" else "❌"
    
    text = (
        f"📋 <b>ORDER LOOKUP</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.full_name if user else 'Unknown'}\n"
        f"🆔 <b>User ID:</b> <code>{tx.user_id}</code>\n"
        f"📅 <b>Date:</b> {tx.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧾 <b>Order ID:</b> <code>{tx.provider_payment_charge_id}</code>\n"
        f"💸 <b>Amount:</b> ₹{tx.amount}\n"
        f"💳 <b>Payment Proof / UTR:</b>\n"
        f"<code>{tx.payment_proof_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎫 <b>Coupons:</b>\n"
    )
    
    if items:
        for c, cat in items:
            text += f"▪️ {cat.name}: <code>{c.code}</code>\n"
    else:
        text += "<i>No assigned coupons</i>\n"
        
    text += f"\n📊 <b>Status:</b> {status_emoji} <b>{tx.status.upper()}</b>"
    
    kb_list = []
    if tx.status == 'pending':
        kb_list.append([InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve_{tx.id}"), InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject_{tx.id}")])
    kb_list.append([InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="admin_orders_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_list)
    
    photo_id = None
    if tx.payment_proof_id and "File: " in tx.payment_proof_id:
        try: photo_id = tx.payment_proof_id.split("File: ")[-1].strip()
        except: pass

    try:
        if photo_id:
            if is_callback: await message_obj.delete()
            if len(text) > 1024:
                await message_obj.answer_photo(photo=photo_id)
                await message_obj.answer(text, reply_markup=kb)
            else:
                await message_obj.answer_photo(photo=photo_id, caption=text, reply_markup=kb)
        else:
            if is_callback: await message_obj.edit_text(text, reply_markup=kb)
            else: await message_obj.answer(text, reply_markup=kb)
    except Exception as e:
        print(f"Order Detail Send Error: {e}")
        await message_obj.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("admin_approve_"))
async def approve(callback: CallbackQuery, bot: Bot, session: AsyncIOMotorDatabase):
    tx_id = int(callback.data.split("_")[-1])
    tx, items = await get_transaction_with_items(session, tx_id)
    if not tx or tx.status != 'pending':
        await callback.answer("❌ Already processed.", show_alert=True)
        return
    await finalize_sale(session, tx.id, tx.user_id)
    codes_list = "\n".join([f"▪️ Code: <code>{c.code}</code>" for c, cat in items])
    user_id = tx.user_id
    tx_charge_id = tx.provider_payment_charge_id
    delivery_msg = (
        f"🎉 <b>Payment Approved! Your Coupons Are Ready!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Order ID:</b> <code>{tx_charge_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 <b>Your Coupon Code(s):</b>\n{codes_list}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>USE YOUR CODE IMMEDIATELY!</b>\n\n"
        f"🛒 <b>How to redeem:</b>\n"
        f"├─ Visit: sheinindia.in/c/sheinverse-17042026\n"
        f"├─ Add items worth ₹1000+\n"
        f"├─ Apply code at checkout\n"
        f"└─ Record <b>UNCUT VIDEO</b> throughout\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📜 <b>Terms:</b> No refund after delivery. Video proof mandatory for replacements. ❌"
    )
    await bot.send_message(user_id, delivery_msg, disable_web_page_preview=True)
    try: await callback.message.delete()
    except Exception: pass
    await callback.answer("✅ Approved!")

@router.callback_query(F.data.startswith("admin_reject_"))
async def reject(callback: CallbackQuery, bot: Bot, session: AsyncIOMotorDatabase):
    tx_id = int(callback.data.split("_")[-1])
    tx = await get_transaction(session, tx_id)
    if not tx or tx.status != 'pending':
        await callback.answer("❌ Already processed.", show_alert=True)
        return
    user_id = tx.user_id
    await update_transaction(session, tx_id, status='failed')
    await session.coupons.update_many({"transaction_id": tx_id}, {"$set": {"transaction_id": None}})
    await bot.send_message(
        user_id,
        f"❌ <b>Payment Rejected</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Your payment could not be verified.\n\n"
        f"📌 <b>Possible reasons:</b>\n"
        f"├─ Incorrect UTR number\n"
        f"├─ Wrong payment amount\n"
        f"├─ Payment not received yet\n"
        f"└─ Unclear or invalid screenshot\n\n"
        f"🛒 You can place a new order from <b>Buy Coupons</b>.\n"
        f"📞 For disputes: @helpdesk_coupon_bot with payment proof."
    )
    try: await callback.message.delete()
    except Exception: pass
    await callback.answer("❌ Rejected.")

@router.message(F.text == "🚪 Exit Admin")
async def exit_admin(message: Message):
    from handlers.user import main_reply_keyboard; await message.answer("🚪 Admin Panel Closed.", reply_markup=main_reply_keyboard(message.from_user.id))

# --- Navigation Callbacks ---
@router.callback_query(F.data == "admin_back_to_settings")
async def back_to_settings_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear(); await admin_settings(callback.message, session, state); await callback.message.delete()

@router.callback_query(F.data == "admin_vouchers_back")
async def vouchers_back_cb(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    await state.clear(); await admin_vouchers(callback.message, state, session); await callback.message.delete()

@router.callback_query(F.data.startswith("admin_view_codes_"))
async def view_codes_cb(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        # Get unsold coupons first, then sold ones, limited to 30 for better visibility
        coupons = await get_all_coupons_by_category(session, cat_id, limit=30)

    if not coupons:
        await callback.answer("❌ No coupons in this category.", show_alert=True)
        return

    text = f"👁️ <b>Inventory (Last 30)</b>\n\n"
    keyboard = []
    
    for c in coupons:
        if c.is_sold:
            status = "🔴 SOLD"
        elif c.transaction_id:
            status = "⏳ PEND"
        else:
            status = "🟢 AVAIL"
        
        text += f"{status} | <code>{c.code}</code>\n"
        
        # Only show delete button for available (not sold and not currently in a pending transaction)
        if not c.is_sold and not c.transaction_id:
            keyboard.append([InlineKeyboardButton(text=f"🗑 Delete {c.code}", callback_data=f"admin_del_coupon_ask_{c.id}")])
    
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"admin_manage_cat_{cat_id}")])
    
    # Split message if it gets too long for Telegram
    if len(text) > 4000:
        text = text[:4000] + "\n..."
        
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_coupon_ask_"))
async def delete_coupon_confirm(callback: CallbackQuery):
    coupon_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        coupon = await get_coupon(session, coupon_id)
        if not coupon:
            return await callback.answer("❌ Coupon not found.")
        
    text = f"⚠️ <b>Delete Coupon?</b>\n\nCode: <code>{coupon.code}</code>\n\nAre you sure you want to delete this coupon?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 YES, DELETE", callback_data=f"admin_del_coupon_exec_{coupon_id}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_view_codes_{coupon.category_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_coupon_exec_"))
async def delete_coupon_exec(callback: CallbackQuery):
    coupon_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        coupon = await get_coupon(session, coupon_id)
        if not coupon:
            await callback.answer("❌ Coupon not found.", show_alert=True)
            return
        
        if coupon.is_sold or coupon.transaction_id:
            await callback.answer("❌ Cannot delete sold or reserved coupon.", show_alert=True)
            return
            
        cat_id = coupon.category_id
        await delete_coupon_db(session, coupon_id)
    
    await callback.answer("✅ Deleted.")
    
    # Refresh the view
    async with AsyncSessionLocal() as session:
        coupons = await get_all_coupons_by_category(session, cat_id, limit=30)
    
    text = f"👁️ <b>Inventory (Last 30)</b>\n\n"
    keyboard = []
    
    if not coupons:
        text += "<i>No coupons left in this category.</i>"
    else:
        for c in coupons:
            if c.is_sold: status = "🔴 SOLD"
            elif c.transaction_id: status = "⏳ PEND"
            else: status = "🟢 AVAIL"
            
            text += f"{status} | <code>{c.code}</code>\n"
            if not c.is_sold and not c.transaction_id:
                keyboard.append([InlineKeyboardButton(text=f"🗑 Delete {c.code}", callback_data=f"admin_del_coupon_ask_{c.id}")])
    
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"admin_manage_cat_{cat_id}")])
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith("admin_clear_codes_confirm_"))
async def clear_codes_confirm(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        avail, pend, cat = await get_category_with_stock(session, cat_id)
        count = avail
    
    if not count or count == 0:
        return await callback.answer("❌ No available codes to delete.", show_alert=True)
        
    text = (
        f"⚠️ <b>MASS DELETION WARNING</b>\n\n"
        f"You are about to delete <b>ALL {count} available codes</b> in category: <b>{cat.name}</b>.\n\n"
        f"<i>This action is irreversible and will not affect sold or currently pending coupons.</i>\n\n"
        f"<b>Are you absolutely sure?</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 YES, DELETE ALL", callback_data=f"admin_clear_codes_exec_{cat_id}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_manage_cat_{cat_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("admin_clear_codes_exec_"))
async def clear_codes_exec(callback: CallbackQuery, session: AsyncIOMotorDatabase):
    cat_id = int(callback.data.split("_")[-1])
    await delete_all_coupons_by_category(session, cat_id)

    await callback.answer("✅ All available codes deleted.", show_alert=True)
    await manage_cat(callback, session)

@router.callback_query(F.data.startswith("admin_del_cat_ask_"))
async def del_cat_confirm(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        cat = await get_category(session, cat_id)
        if not cat: return await callback.answer("❌ Not found.")
        
    text = (
        f"🚨 <b>DELETE CATEGORY?</b>\n\n"
        f"Category: <b>{cat.name}</b>\n\n"
        f"⚠️ <b>WARNING:</b> This will delete the category AND all coupons (sold and unsold) inside it! This action is irreversible.\n\n"
        f"Are you sure?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 YES, DELETE EVERYTHING", callback_data=f"admin_del_cat_exec_{cat_id}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data=f"admin_manage_cat_{cat_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_cat_exec_"))
async def del_cat_exec(callback: CallbackQuery, state: FSMContext, session: AsyncIOMotorDatabase):
    cat_id = int(callback.data.split("_")[-1])
    await delete_category_db(session, cat_id)
    await callback.answer("✅ Category and all its data deleted.", show_alert=True)
    await admin_vouchers(callback.message, state, session)
    try:
        await callback.message.delete()
    except Exception:
        pass

@router.callback_query(F.data == "admin_channels")
async def chan_mgr_cb(callback: CallbackQuery):
    async with AsyncSessionLocal() as session: channels = await get_channels(session)
    text = "📢 <b>Channels</b>\n"; keyboard = []
    for ch in channels: text += f"🔹 {ch.name}\n"; keyboard.append([InlineKeyboardButton(text=f"🗑 {ch.name}", callback_data=f"del_ch_ask_{ch.id}")])
    keyboard.append([InlineKeyboardButton(text="➕ Add", callback_data="add_ch")]); keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back_to_settings")])
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data == "add_ch")
async def add_chan_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_ch_name); await callback.message.answer("🏷️ Name:"); await callback.answer()

@router.message(AdminStates.add_ch_name)
async def proc_chan_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text); await state.set_state(AdminStates.add_ch_id); await message.answer("🆔 ID:")

@router.message(AdminStates.add_ch_id)
async def proc_chan_id(message: Message, state: FSMContext):
    await state.update_data(chat_id=message.text); await state.set_state(AdminStates.add_ch_link); await message.answer("🔗 Link:")

@router.message(AdminStates.add_ch_link)
async def proc_chan_link(message: Message, state: FSMContext):
    data = await state.get_data(); 
    async with AsyncSessionLocal() as session:
        await add_channel_db(session, data['name'], data['chat_id'], message.text)
        await state.clear(); await message.answer("✅ Added!"); await admin_settings(message, session, state)

@router.callback_query(F.data.startswith("del_ch_ask_"))
async def del_chan_confirm(callback: CallbackQuery):
    ch_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        ch = await get_channel(session, ch_id)
        if not ch: return await callback.answer("❌ Not found.")
        
    text = f"⚠️ <b>Remove Channel?</b>\n\nChannel: <b>{ch.name}</b>\n\nAre you sure?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 YES, REMOVE", callback_data=f"del_ch_exec_{ch_id}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="admin_channels")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("del_ch_exec_"))
async def del_chan_exec(callback: CallbackQuery):
    ch_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        await delete_channel_db(session, ch_id)
    await callback.answer("✅ Channel removed.")
    await chan_mgr_cb(callback)

async def show_support_settings(message: Message):
    async with AsyncSessionLocal() as session:
        from database.requests import get_all_support_contacts
        contacts = await get_all_support_contacts(session)
    
    text = "📞 <b>Support Contact Management</b>\n\n"
    keyboard = []
    
    if not contacts:
        text += "<i>No support contacts added yet. Users will see the default support bot.</i>"
    else:
        for c in contacts:
            status = "🟢" if c.is_active else "🔴"
            text += f"{status} <b>{c.label}</b>: @{c.username}\n"
            keyboard.append([
                InlineKeyboardButton(text=f"🗑 Delete {c.label}", callback_data=f"admin_del_support_ask_{c.id}"),
                InlineKeyboardButton(text="Toggle" if c.is_active else "Enable", callback_data=f"admin_toggle_support_{c.id}")
            ])
    
    keyboard.append([InlineKeyboardButton(text="➕ Add New Support", callback_data="admin_add_support")])
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back_to_settings")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data == "admin_support_settings")
async def support_settings_cb(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        from database.requests import get_all_support_contacts
        contacts = await get_all_support_contacts(session)
    
    text = "📞 <b>Support Contact Management</b>\n\n"
    keyboard = []
    
    if not contacts:
        text += "<i>No support contacts added yet. Users will see the default support bot.</i>"
    else:
        for c in contacts:
            status = "🟢" if c.is_active else "🔴"
            text += f"{status} <b>{c.label}</b>: @{c.username}\n"
            keyboard.append([
                InlineKeyboardButton(text=f"🗑 Delete {c.label}", callback_data=f"admin_del_support_ask_{c.id}"),
                InlineKeyboardButton(text="Toggle" if c.is_active else "Enable", callback_data=f"admin_toggle_support_{c.id}")
            ])
    
    keyboard.append([InlineKeyboardButton(text="➕ Add New Support", callback_data="admin_add_support")])
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back_to_settings")])
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data == "admin_add_support")
async def add_support_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_support_label)
    await callback.message.answer("🏷️ <b>Enter a label for this support:</b>\n(e.g. 'Support Team 1' or 'Urgent Help')")
    await callback.answer()

@router.message(AdminStates.add_support_label)
async def add_support_label_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    await state.update_data(support_label=message.text)
    await state.set_state(AdminStates.add_support_user)
    await message.answer("🆔 <b>Enter the Telegram username:</b>\n(Without @ symbol)")

@router.message(AdminStates.add_support_user)
async def add_support_user_proc(message: Message, state: FSMContext):
    if is_menu_button(message.text): await state.clear(); return
    username = message.text.replace("@", "").strip()
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        from database.requests import add_support_contact
        await add_support_contact(session, data['support_label'], username)
    await state.clear()
    await message.answer(f"✅ Support contact <b>{data['support_label']}</b> (@{username}) added!")
    await show_support_settings(message)

@router.callback_query(F.data.startswith("admin_del_support_ask_"))
async def del_support_confirm_cb(callback: CallbackQuery):
    c_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from database.requests import get_all_support_contacts # Or use a single fetch
        contacts = await get_all_support_contacts(session)
        contact = next((c for c in contacts if c.id == c_id), None)
        if not contact: return await callback.answer("❌ Not found.")
        
    text = f"⚠️ <b>Delete Support Contact?</b>\n\nLabel: <b>{contact.label}</b>\nUser: @{contact.username}\n\nAre you sure?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 YES, DELETE", callback_data=f"admin_del_support_exec_{c_id}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="admin_support_settings")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_support_exec_"))
async def del_support_exec_cb(callback: CallbackQuery):
    c_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from database.requests import delete_support_contact
        await delete_support_contact(session, c_id)
    await callback.answer("✅ Support contact deleted.")
    await support_settings_cb(callback)

@router.callback_query(F.data.startswith("admin_toggle_support_"))
async def toggle_support_cb(callback: CallbackQuery):
    c_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from database.requests import toggle_support_status
        await toggle_support_status(session, c_id)
    await callback.answer("✅ Status updated.")
    await support_settings_cb(callback)

@router.callback_query(F.data == "admin_blocked_list")
async def blocked_users_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        users = await get_blocked_users(session)
    
    text = "🚫 <b>Blocked Users (Banned)</b>\n\n"
    keyboard = []
    
    if not users:
        text += "<i>No users are currently blocked.</i>"
    else:
        for u in users[:20]:
            text += f"▪️ <b>{u.full_name}</b> (<code>{u.id}</code>)\n"
            keyboard.append([InlineKeyboardButton(text=f"✅ Unblock {u.id}", callback_data=f"admin_unblock_user_{u.id}")])
            
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back_to_settings")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("admin_unblock_user_"))
async def unblock_user_cb(callback: CallbackQuery, state: FSMContext):
    u_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from database.requests import reset_user_warnings
        await reset_user_warnings(session, u_id)
        await session.users.update_one({"_id": u_id}, {"$set": {"is_blocked": False}})
        # await session.commit() # No longer needed for Motor
    await callback.answer("✅ User unblocked and warnings reset.")
    await blocked_users_cb(callback, state)
