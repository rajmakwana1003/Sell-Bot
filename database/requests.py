"""
database/requests.py  —  All database operations (MongoDB / Motor version).

Every function accepts `db: AsyncIOMotorDatabase` as its first argument.
Return values are either `Doc` objects (attribute-accessible wrappers around
MongoDB dicts), lists of `Doc` objects, or primitive scalars — keeping the
same interface the handlers expect.
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument, ASCENDING, DESCENDING


# ─── Doc wrapper ──────────────────────────────────────────────────────────────

class Doc:
    """
    Lightweight wrapper: converts a MongoDB document dict to an object
    where fields are accessible as attributes (e.g. doc.is_admin).
    MongoDB's `_id` is also available as `.id` for backward compatibility.
    """
    __slots__ = ("__dict__",)

    def __init__(self, d: dict):
        if d is not None:
            data = dict(d)
            if "_id" in data and "id" not in data:
                data["id"] = data["_id"]
            self.__dict__.update(data)

    def __bool__(self):
        return bool(self.__dict__)

    def __repr__(self):
        return f"Doc({self.__dict__})"

    def __getattr__(self, name):
        return None  # Return None for missing attrs instead of raising AttributeError


def _d(doc) -> Optional[Doc]:
    return Doc(doc) if doc is not None else None


def _dl(docs) -> list:
    return [Doc(d) for d in (docs if docs is not None else []) if d is not None]


# ─── User operations ──────────────────────────────────────────────────────────

async def get_or_create_user(db: AsyncIOMotorDatabase, user_id: int,
                              username=None, full_name=None, referred_by=None) -> Tuple[Doc, bool]:
    doc = await db.users.find_one({"_id": user_id})
    if doc is not None:
        return _d(doc), False

    # Credit referrer (atomic)
    final_ref = None
    if referred_by and referred_by != user_id:
        result = await db.users.find_one_and_update(
            {"_id": referred_by},
            {"$inc": {"referral_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if result is not None:
            final_ref = referred_by

    new_doc = {
        "_id":          user_id,
        "username":     username,
        "full_name":    full_name,
        "is_admin":     False,
        "is_blocked":   False,
        "is_suspicious":False,
        "warning_count":0,
        "referred_by":  final_ref,
        "referral_count":0,
        "created_at":   datetime.utcnow(),
    }
    await db.users.insert_one(new_doc)
    return _d(new_doc), True


async def get_user_by_id(db: AsyncIOMotorDatabase, user_id: int) -> Optional[Doc]:
    return _d(await db.users.find_one({"_id": user_id}))


async def get_all_users(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.users.find({}).to_list(None))


async def get_admins(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.users.find({"is_admin": True}).to_list(None))


async def set_admin_status(db: AsyncIOMotorDatabase, user_id: int, status: bool):
    await db.users.update_one({"_id": user_id}, {"$set": {"is_admin": status}})


async def get_blocked_users(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.users.find({"is_blocked": True}).sort("_id", ASCENDING).to_list(None))


async def get_suspicious_users(db: AsyncIOMotorDatabase) -> list:
    docs = await db.users.find(
        {"$or": [{"is_suspicious": True}, {"warning_count": {"$gt": 0}}]}
    ).sort("warning_count", DESCENDING).to_list(None)
    return _dl(docs)


async def increment_user_warning(db: AsyncIOMotorDatabase, user_id: int) -> int:
    result = await db.users.find_one_and_update(
        {"_id": user_id},
        {"$inc": {"warning_count": 1}, "$set": {"is_suspicious": True}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return 0
    count = result["warning_count"]
    if count >= 5:
        await db.users.update_one({"_id": user_id}, {"$set": {"is_blocked": True}})
    return count


async def reset_user_warnings(db: AsyncIOMotorDatabase, user_id: int):
    await db.users.update_one(
        {"_id": user_id},
        {"$set": {"warning_count": 0, "is_suspicious": False}},
    )


async def get_top_referrers(db: AsyncIOMotorDatabase, limit: int = 10) -> list:
    docs = await db.users.find(
        {"referral_count": {"$gt": 0}}
    ).sort("referral_count", DESCENDING).limit(limit).to_list(None)
    return _dl(docs)


async def get_users_with_purchases(db: AsyncIOMotorDatabase) -> list:
    """User list with purchase count for admin panel."""
    pipeline = [
        {"$lookup": {
            "from": "transactions",
            "let": {"uid": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$user_id", "$$uid"]}, "status": "completed"}}
            ],
            "as": "txs",
        }},
        {"$addFields": {"bought": {"$size": "$txs"}}},
        {"$project": {"txs": 0}},
        {"$sort": {"_id": 1}},
    ]
    return _dl(await db.users.aggregate(pipeline).to_list(None))


# ─── Category operations ──────────────────────────────────────────────────────

async def add_category(db: AsyncIOMotorDatabase, name: str,
                       description: str = None, terms: str = None) -> Doc:
    from database.db import get_next_id
    cat_id = await get_next_id("categories")
    doc = {"_id": cat_id, "name": name, "description": description,
           "terms": terms, "is_active": True}
    await db.categories.insert_one(doc)
    return _d(doc)


async def get_categories(db: AsyncIOMotorDatabase, only_active: bool = False) -> list:
    query = {"is_active": True} if only_active else {}
    return _dl(await db.categories.find(query).to_list(None))


async def get_category(db: AsyncIOMotorDatabase, cat_id: int) -> Optional[Doc]:
    return _d(await db.categories.find_one({"_id": cat_id}))


async def toggle_category_status(db: AsyncIOMotorDatabase, category_id: int) -> Optional[Doc]:
    cat = await db.categories.find_one({"_id": category_id})
    if cat is None:
        return None
    new_status = not cat.get("is_active", True)
    await db.categories.update_one({"_id": category_id}, {"$set": {"is_active": new_status}})
    cat["is_active"] = new_status
    return _d(cat)


async def update_category_name(db: AsyncIOMotorDatabase, category_id: int, name: str):
    await db.categories.update_one({"_id": category_id}, {"$set": {"name": name}})


async def update_category_description(db: AsyncIOMotorDatabase, category_id: int, desc: str):
    await db.categories.update_one({"_id": category_id}, {"$set": {"description": desc}})


async def update_category_terms(db: AsyncIOMotorDatabase, category_id: int, terms: str):
    await db.categories.update_one({"_id": category_id}, {"$set": {"terms": terms}})


async def delete_category_db(db: AsyncIOMotorDatabase, category_id: int):
    await asyncio.gather(
        db.coupons.delete_many({"category_id": category_id}),
        db.categories.delete_one({"_id": category_id}),
    )


async def delete_all_coupons_by_category(db: AsyncIOMotorDatabase, category_id: int):
    """Delete only available (not sold, not pending) coupons."""
    await db.coupons.delete_many(
        {"category_id": category_id, "is_sold": False, "transaction_id": None}
    )


async def get_category_with_stock(db: AsyncIOMotorDatabase, cat_id: int):
    """Returns (avail_count, pend_count, category_Doc) for admin manage_cat."""
    avail, pend, cat = await asyncio.gather(
        db.coupons.count_documents({"category_id": cat_id, "is_sold": False, "transaction_id": None}),
        db.coupons.count_documents({"category_id": cat_id, "is_sold": False, "transaction_id": {"$ne": None}}),
        db.categories.find_one({"_id": cat_id}),
    )
    return avail, pend, _d(cat)


async def get_category_stock_summary(db: AsyncIOMotorDatabase) -> list:
    """
    For Browse Coupons screen.
    Returns list of (category_Doc, stock_count, min_price).
    """
    pipeline = [
        {"$match": {"is_active": True}},
        {"$lookup": {
            "from": "coupons",
            "let": {"cat_id": "$_id"},
            "pipeline": [{"$match": {
                "$expr": {"$eq": ["$category_id", "$$cat_id"]},
                "is_sold": False, "transaction_id": None,
            }}],
            "as": "avail",
        }},
        {"$addFields": {
            "stock": {"$size": "$avail"},
            "price": {"$min": "$avail.price_inr"},
        }},
        {"$project": {"avail": 0}},
        {"$sort": {"_id": 1}},
    ]
    docs = await db.categories.aggregate(pipeline).to_list(None)
    return [(_d({k: v for k, v in d.items() if k not in ("stock", "price")}),
             d.get("stock", 0),
             d.get("price")) for d in docs]


async def get_inventory_summary(db: AsyncIOMotorDatabase) -> list:
    """
    For Live Inventory screen.
    Returns list of (cat_id, name, is_active, avail, pend, min_price).
    """
    pipeline = [
        {"$lookup": {
            "from": "coupons",
            "let": {"cat_id": "$_id"},
            "pipeline": [{"$match": {"$expr": {"$eq": ["$category_id", "$$cat_id"]}}}],
            "as": "all",
        }},
        {"$addFields": {
            "avail": {"$size": {"$filter": {
                "input": "$all",
                "cond": {"$and": [{"$eq": ["$$this.is_sold", False]},
                                  {"$eq": ["$$this.transaction_id", None]}]},
            }}},
            "pend": {"$size": {"$filter": {
                "input": "$all",
                "cond": {"$and": [{"$eq": ["$$this.is_sold", False]},
                                  {"$ne": ["$$this.transaction_id", None]}]},
            }}},
            "price": {"$min": {"$map": {
                "input": {"$filter": {
                    "input": "$all",
                    "cond": {"$and": [{"$eq": ["$$this.is_sold", False]},
                                      {"$eq": ["$$this.transaction_id", None]}]},
                }},
                "in": "$$this.price_inr",
            }}},
        }},
        {"$project": {"all": 0}},
        {"$sort": {"_id": 1}},
    ]
    docs = await db.categories.aggregate(pipeline).to_list(None)
    return [(d["_id"], d["name"], d.get("is_active", True),
             d.get("avail", 0), d.get("pend", 0), d.get("price")) for d in docs]


# ─── Coupon operations ────────────────────────────────────────────────────────

async def add_coupon(db: AsyncIOMotorDatabase, category_id: int,
                     code: str, price: float) -> Doc:
    from database.db import get_next_id
    cid = await get_next_id("coupons")
    doc = {"_id": cid, "category_id": category_id, "code": code,
           "price_inr": price, "is_sold": False, "sold_to": None,
           "transaction_id": None, "created_at": datetime.utcnow()}
    await db.coupons.insert_one(doc)
    return _d(doc)


async def get_coupon(db: AsyncIOMotorDatabase, coupon_id: int) -> Optional[Doc]:
    return _d(await db.coupons.find_one({"_id": coupon_id}))


async def get_available_coupons(db: AsyncIOMotorDatabase, category_id: int) -> list:
    return _dl(await db.coupons.find(
        {"category_id": category_id, "is_sold": False, "transaction_id": None}
    ).to_list(None))


async def get_all_coupons_by_category(db: AsyncIOMotorDatabase, category_id: int,
                                       limit: int = 30) -> list:
    """For admin inventory view — sorted: available first, then pending, then sold."""
    docs = await db.coupons.find({"category_id": category_id}).sort([
        ("is_sold", ASCENDING),
        ("transaction_id", DESCENDING),
        ("_id", DESCENDING),
    ]).limit(limit).to_list(None)
    return _dl(docs)


async def delete_coupon_db(db: AsyncIOMotorDatabase, coupon_id: int):
    await db.coupons.delete_one({"_id": coupon_id})


async def update_category_price(db: AsyncIOMotorDatabase, category_id: int, price: float):
    """Update price for all unsold coupons in a category."""
    await db.coupons.update_many(
        {"category_id": category_id, "is_sold": False},
        {"$set": {"price_inr": price}},
    )


async def get_category_price(db: AsyncIOMotorDatabase, cat_id: int) -> Optional[float]:
    """Get current price (min price of available coupons)."""
    doc = await db.coupons.find_one(
        {"category_id": cat_id, "is_sold": False, "transaction_id": None},
        sort=[("price_inr", ASCENDING)]
    )
    return doc["price_inr"] if doc else None


async def reserve_coupons_atomic(db: AsyncIOMotorDatabase, category_id: int,
                                   quantity: int, tx_id: int) -> Optional[list]:
    """
    Atomically reserve `quantity` coupons via find_one_and_update.
    Replaces PostgreSQL SELECT FOR UPDATE NOWAIT.
    Returns list of Doc objects, or None if stock insufficient.
    """
    reserved = []
    for _ in range(quantity):
        coupon = await db.coupons.find_one_and_update(
            {"category_id": category_id, "is_sold": False, "transaction_id": None},
            {"$set": {"transaction_id": tx_id}},
            return_document=ReturnDocument.AFTER,
        )
        if coupon is None:
            # Insufficient stock — release already-reserved coupons
            if reserved:
                ids = [c["_id"] for c in reserved]
                await db.coupons.update_many(
                    {"_id": {"$in": ids}},
                    {"$set": {"transaction_id": None}},
                )
            return None
        reserved.append(coupon)
    return _dl(reserved)


async def get_coupons_by_transaction(db: AsyncIOMotorDatabase, tx_id: int) -> list:
    return _dl(await db.coupons.find({"transaction_id": tx_id}).to_list(None))


# ─── Transaction operations ───────────────────────────────────────────────────

async def create_transaction(db: AsyncIOMotorDatabase, user_id: int,
                              amount: float, quantity: int,
                              provider_payment_charge_id: str = None,
                              expires_at: datetime = None) -> Doc:
    from database.db import get_next_id
    tx_id = await get_next_id("transactions")
    doc = {
        "_id":                        tx_id,
        "user_id":                    user_id,
        "quantity":                   quantity,
        "amount":                     amount,
        "payment_proof_id":           None,
        "utr":                        None,
        "provider_payment_charge_id": provider_payment_charge_id,
        "status":                     "pending",
        "created_at":                 datetime.utcnow(),
        "expires_at":                 expires_at,
    }
    await db.transactions.insert_one(doc)
    return _d(doc)


async def get_transaction(db: AsyncIOMotorDatabase, tx_id: int) -> Optional[Doc]:
    return _d(await db.transactions.find_one({"_id": tx_id}))


async def update_transaction(db: AsyncIOMotorDatabase, tx_id: int, **kwargs):
    await db.transactions.update_one({"_id": tx_id}, {"$set": kwargs})


async def finalize_sale(db: AsyncIOMotorDatabase, tx_id: int, user_id: int):
    """Mark coupons as sold and transaction as completed."""
    await asyncio.gather(
        db.coupons.update_many(
            {"transaction_id": tx_id},
            {"$set": {"is_sold": True, "sold_to": user_id}},
        ),
        db.transactions.update_one({"_id": tx_id}, {"$set": {"status": "completed"}}),
    )


async def find_transaction_robust(db: AsyncIOMotorDatabase, search_term: str) -> Optional[Doc]:
    """Search by integer ID, order ID string, UTR, or payment proof."""
    try:
        doc = await db.transactions.find_one({"_id": int(search_term)})
        if doc:
            return _d(doc)
    except (ValueError, TypeError):
        pass

    doc = await db.transactions.find_one({"$or": [
        {"provider_payment_charge_id": search_term},
        {"utr": search_term},
        {"payment_proof_id": search_term},
    ]})
    if doc is not None:
        return _d(doc)

    try:
        pattern = re.compile(re.escape(search_term), re.IGNORECASE)
        doc = await db.transactions.find_one(
            {"provider_payment_charge_id": {"$regex": pattern}},
            sort=[("created_at", DESCENDING)],
        )
        if doc:
            return _d(doc)
    except Exception:
        pass
    return None


async def get_user_transactions_completed(db: AsyncIOMotorDatabase, user_id: int) -> list:
    docs = await db.transactions.find(
        {"user_id": user_id, "status": "completed"}
    ).sort("created_at", DESCENDING).to_list(None)
    return _dl(docs)


async def get_user_order_counts(db: AsyncIOMotorDatabase, user_id: int):
    """Returns (total_completed_orders, total_spent) for a user."""
    pipeline = [
        {"$match": {"user_id": user_id, "status": "completed"}},
        {"$group": {
            "_id": None,
            "count": {"$sum": 1},
            "total": {"$sum": "$amount"}
        }}
    ]
    res = await db.transactions.aggregate(pipeline).to_list(1)
    if not res:
        return 0, 0.0
    return res[0]["count"], float(res[0]["total"])


async def update_user(db: AsyncIOMotorDatabase, user_id: int, **kwargs):
    await db.users.update_one({"_id": user_id}, {"$set": kwargs})


async def release_coupons_by_transaction(db: AsyncIOMotorDatabase, tx_id: int):
    """Release coupons reserved by a transaction."""
    await db.coupons.update_many(
        {"transaction_id": tx_id, "is_sold": False},
        {"$set": {"transaction_id": None}}
    )


async def get_transaction_with_items(db: AsyncIOMotorDatabase, tx_id: int):
    """Returns (tx_Doc, [(coupon_Doc, cat_Doc), ...])."""
    tx = await db.transactions.find_one({"_id": tx_id})
    if tx is None:
        return None, []

    pipeline = [
        {"$match": {"transaction_id": tx_id}},
        {"$lookup": {
            "from": "categories",
            "localField": "category_id",
            "foreignField": "_id",
            "as": "cat",
        }},
        {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},
    ]
    coupon_docs = await db.coupons.aggregate(pipeline).to_list(None)
    items = [(_d({k: v for k, v in c.items() if k != "cat"}), _d(c.get("cat")))
             for c in coupon_docs]
    return _d(tx), items


async def get_pending_transactions(db: AsyncIOMotorDatabase) -> list:
    docs = await db.transactions.find(
        {"status": "pending"}
    ).sort("created_at", DESCENDING).to_list(None)
    return _dl(docs)


async def get_transactions_by_status(db: AsyncIOMotorDatabase, status: str, limit: int = 20) -> list:
    docs = await db.transactions.find(
        {"status": status}
    ).sort("created_at", DESCENDING).limit(limit).to_list(None)
    return _dl(docs)


async def get_order_dashboard_stats(db: AsyncIOMotorDatabase) -> dict:
    """All counts needed for the Orders Dashboard in one parallel batch."""
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    yesterday_start = today_start - timedelta(days=1)

    today_count, yesterday_count, pending, completed, failed, cancelled = await asyncio.gather(
        db.transactions.count_documents({"created_at": {"$gte": today_start}}),
        db.transactions.count_documents({"created_at": {"$gte": yesterday_start, "$lt": today_start}}),
        db.transactions.count_documents({"status": "pending"}),
        db.transactions.count_documents({"status": "completed"}),
        db.transactions.count_documents({"status": "failed"}),
        db.transactions.count_documents({"status": "cancelled"}),
    )
    return {
        "today": today_count, "yesterday": yesterday_count,
        "pending": pending, "completed": completed,
        "failed": failed, "cancelled": cancelled,
    }


async def purge_cancelled_transactions(db: AsyncIOMotorDatabase) -> int:
    result = await db.transactions.delete_many({"status": "cancelled"})
    return result.deleted_count


async def get_expired_pending_transactions(db: AsyncIOMotorDatabase) -> list:
    now = datetime.utcnow()
    docs = await db.transactions.find(
        {"status": "pending", "expires_at": {"$lte": now}, "payment_proof_id": None}
    ).to_list(None)
    return _dl(docs)


# ─── Channel operations ───────────────────────────────────────────────────────

async def get_channels(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.channels.find({}).to_list(None))


async def get_channel(db: AsyncIOMotorDatabase, channel_id: int) -> Optional[Doc]:
    return _d(await db.channels.find_one({"_id": channel_id}))


async def add_channel_db(db: AsyncIOMotorDatabase, name: str, chat_id: str, invite_link: str):
    from database.db import get_next_id
    cid = await get_next_id("channels")
    await db.channels.insert_one({"_id": cid, "name": name,
                                   "chat_id": chat_id, "invite_link": invite_link})


async def delete_channel_db(db: AsyncIOMotorDatabase, channel_id: int):
    await db.channels.delete_one({"_id": channel_id})


# ─── Settings ─────────────────────────────────────────────────────────────────

async def get_setting(db: AsyncIOMotorDatabase, key: str, default: str = None) -> Optional[str]:
    doc = await db.settings.find_one({"_id": key})
    return doc["value"] if doc is not None else default


async def set_setting(db: AsyncIOMotorDatabase, key: str, value: str):
    await db.settings.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


# ─── Support contacts ─────────────────────────────────────────────────────────

async def get_support_contacts(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.support_contacts.find({"is_active": True}).to_list(None))


async def get_all_support_contacts(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.support_contacts.find({}).to_list(None))


async def add_support_contact(db: AsyncIOMotorDatabase, label: str, username: str) -> Doc:
    from database.db import get_next_id
    cid = await get_next_id("support_contacts")
    doc = {"_id": cid, "label": label, "username": username, "is_active": True}
    await db.support_contacts.insert_one(doc)
    return _d(doc)


async def delete_support_contact(db: AsyncIOMotorDatabase, contact_id: int):
    await db.support_contacts.delete_one({"_id": contact_id})


async def toggle_support_status(db: AsyncIOMotorDatabase, contact_id: int):
    doc = await db.support_contacts.find_one({"_id": contact_id})
    if doc is not None:
        ns = not doc.get("is_active", True)
        await db.support_contacts.update_one({"_id": contact_id}, {"$set": {"is_active": ns}})
        doc["is_active"] = ns
    return _d(doc)


# ─── Referral rewards ─────────────────────────────────────────────────────────

async def get_referral_rewards(db: AsyncIOMotorDatabase) -> list:
    return _dl(await db.referral_rewards.find({}).to_list(None))


async def add_referral_reward(db: AsyncIOMotorDatabase, code: str):
    from database.db import get_next_id
    rid = await get_next_id("referral_rewards")
    await db.referral_rewards.insert_one({
        "_id": rid, "code": code, "is_used": False, "used_by": None,
        "created_at": datetime.utcnow(),
    })


async def delete_referral_reward(db: AsyncIOMotorDatabase, reward_id: int):
    await db.referral_rewards.delete_one({"_id": reward_id})


async def get_available_reward(db: AsyncIOMotorDatabase) -> Optional[Doc]:
    return _d(await db.referral_rewards.find_one({"is_used": False}))


async def count_available_rewards(db: AsyncIOMotorDatabase) -> int:
    return await db.referral_rewards.count_documents({"is_used": False})


async def use_reward(db: AsyncIOMotorDatabase, reward_id: int, user_id: int):
    await db.referral_rewards.update_one(
        {"_id": reward_id},
        {"$set": {"is_used": True, "used_by": user_id}},
    )


async def get_user_redeemed_rewards(db: AsyncIOMotorDatabase, user_id: int) -> list:
    return _dl(await db.referral_rewards.find({"used_by": user_id}).to_list(None))


async def get_referral_reward_by_id(db: AsyncIOMotorDatabase, reward_id: int) -> Optional[Doc]:
    return _d(await db.referral_rewards.find_one({"_id": reward_id}))


async def get_user_full_history(db: AsyncIOMotorDatabase, user_id: int) -> list:
    """Returns list of (tx_Doc, coupon_Doc, cat_Doc) for user profile."""
    pipeline = [
        {"$match": {"user_id": user_id, "status": "completed"}},
        {"$lookup": {
            "from": "coupons",
            "localField": "_id",
            "foreignField": "transaction_id",
            "as": "coupons",
        }},
        {"$unwind": "$coupons"},
        {"$lookup": {
            "from": "categories",
            "localField": "coupons.category_id",
            "foreignField": "_id",
            "as": "cat",
        }},
        {"$unwind": "$cat"},
        {"$sort": {"created_at": DESCENDING}},
    ]
    docs = await db.transactions.aggregate(pipeline).to_list(None)
    return [(_d({k: v for k, v in d.items() if k not in ("coupons", "cat")}),
             _d(d["coupons"]),
             _d(d["cat"])) for d in docs]


# ─── Statistics ───────────────────────────────────────────────────────────────

async def get_stats(db: AsyncIOMotorDatabase) -> dict:

    async def _users():
        r = await db.users.aggregate([{"$group": {
            "_id": None,
            "total":   {"$sum": 1},
            "blocked": {"$sum": {"$cond": ["$is_blocked", 1, 0]}},
            "admins":  {"$sum": {"$cond": ["$is_admin",  1, 0]}},
        }}]).to_list(1)
        return r[0] if r else {"total": 0, "blocked": 0, "admins": 0}

    async def _coupons():
        r = await db.coupons.aggregate([{"$group": {
            "_id": None,
            "total":     {"$sum": 1},
            "available": {"$sum": {"$cond": [
                {"$and": [{"$eq": ["$is_sold", False]}, {"$eq": ["$transaction_id", None]}]}, 1, 0
            ]}},
            "sold": {"$sum": {"$cond": ["$is_sold", 1, 0]}},
        }}]).to_list(1)
        return r[0] if r else {"total": 0, "available": 0, "sold": 0}

    async def _cat_stats():
        pipeline = [
            {"$lookup": {
                "from": "coupons",
                "let": {"cat_id": "$_id"},
                "pipeline": [{"$match": {"$expr": {"$eq": ["$category_id", "$$cat_id"]}}}],
                "as": "coupons",
            }},
            {"$addFields": {
                "available": {"$size": {"$filter": {
                    "input": "$coupons",
                    "cond": {"$and": [{"$eq": ["$$this.is_sold", False]},
                                      {"$eq": ["$$this.transaction_id", None]}]},
                }}},
                "sold":    {"$size": {"$filter": {"input": "$coupons",
                                                   "cond": {"$eq": ["$$this.is_sold", True]}}}},
                "revenue": {"$sum": {"$map": {
                    "input": {"$filter": {"input": "$coupons",
                                         "cond": {"$eq": ["$$this.is_sold", True]}}},
                    "in": "$$this.price_inr",
                }}},
            }},
            {"$project": {"coupons": 0}},
        ]
        docs = await db.categories.aggregate(pipeline).to_list(None)
        return [{"name": d["name"], "available": d.get("available", 0),
                 "sold": d.get("sold", 0), "revenue": float(d.get("revenue", 0))}
                for d in docs]

    users, coupons, categories = await asyncio.gather(_users(), _coupons(), _cat_stats())
    return {
        "users":      {"total": users["total"], "blocked": users["blocked"], "admins": users["admins"]},
        "coupons":    {"total": coupons["total"], "available": coupons["available"], "sold": coupons["sold"]},
        "categories": categories,
    }


async def get_advanced_stats(db: AsyncIOMotorDatabase) -> dict:
    now = datetime.utcnow()
    today_start     = datetime(now.year, now.month, now.day)
    yesterday_start = today_start - timedelta(days=1)
    month_start     = datetime(now.year, now.month, 1)

    r = await db.transactions.aggregate([
        {"$match": {"status": "completed"}},
        {"$group": {
            "_id": None,
            "today":     {"$sum": {"$cond": [{"$gte": ["$created_at", today_start]}, "$amount", 0]}},
            "yesterday": {"$sum": {"$cond": [
                {"$and": [{"$gte": ["$created_at", yesterday_start]},
                           {"$lt":  ["$created_at", today_start]}]}, "$amount", 0,
            ]}},
            "month": {"$sum": {"$cond": [{"$gte": ["$created_at", month_start]}, "$amount", 0]}},
            "total": {"$sum": "$amount"},
        }},
    ]).to_list(1)
    if r:
        return {k: float(v) for k, v in r[0].items() if k != "_id"}
    return {"today": 0.0, "yesterday": 0.0, "month": 0.0, "total": 0.0}
