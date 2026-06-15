import os
import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ReturnDocument, ASCENDING, DESCENDING
from pymongo.server_api import ServerApi
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv(
    "MONGO_URL",
    "mongodb+srv://huri_db:TTIChIpZt6F14rZf@coupon.8mp1wfm.mongodb.net/shein_bot?appName=Coupon"
)

_client: AsyncIOMotorClient = None
_db: AsyncIOMotorDatabase = None


def get_db() -> AsyncIOMotorDatabase:
    """Return the active Motor database — always available after init_db()."""
    return _db


class AsyncSessionLocal:
    """
    Compatibility wrapper: allows 'async with AsyncSessionLocal() as session'
    to work while we migrate handlers from SQLAlchemy to MongoDB (Motor).
    Returns the AsyncIOMotorDatabase instance.
    """
    async def __aenter__(self):
        return _db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


async def get_next_id(collection: str) -> int:
    """
    Atomic auto-increment integer _id using a 'counters' collection.
    Replaces SQLAlchemy's SERIAL / SEQUENCE so all callback_data IDs stay ints.
    """
    result = await _db.counters.find_one_and_update(
        {"_id": collection},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result["seq"]


async def init_db():
    """Connect to MongoDB Atlas, create indexes, seed mandatory channel."""
    global _client, _db

    # Enhanced settings for 24/7 stability
    _client = AsyncIOMotorClient(
        MONGO_URL,
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=20000,
        socketTimeoutMS=30000,
        heartbeatFrequencyMS=10000,
        retryWrites=True,
        retryReads=True,
        maxPoolSize=50,
        minPoolSize=10,
        tlsAllowInvalidCertificates=True,
        tlsAllowInvalidHostnames=True,
        server_api=ServerApi('1')
    )
    _db = _client.get_database("shein_bot")

    # Verify connectivity
    await _db.command("ping")

    # ── Indexes ───────────────────────────────────────────────────────────────
    await _db.users.create_index([("is_admin",        ASCENDING)],  background=True)
    await _db.users.create_index([("is_blocked",      ASCENDING)],  background=True)
    await _db.users.create_index([("referred_by",     ASCENDING)],  background=True)
    await _db.users.create_index([("referral_count",  DESCENDING)], background=True)

    await _db.transactions.create_index([("user_id",                    ASCENDING)],  background=True)
    await _db.transactions.create_index([("status",                     ASCENDING)],  background=True)
    await _db.transactions.create_index([("expires_at",                 ASCENDING)],  background=True)
    await _db.transactions.create_index([("created_at",                 ASCENDING)],  background=True)
    await _db.transactions.create_index([("provider_payment_charge_id", ASCENDING)],  background=True)
    await _db.transactions.create_index([("utr", ASCENDING)], unique=True, sparse=True, background=True)

    await _db.coupons.create_index([("category_id",   ASCENDING)], background=True)
    await _db.coupons.create_index([("is_sold",       ASCENDING)], background=True)
    await _db.coupons.create_index([("transaction_id",ASCENDING)], background=True)
    await _db.coupons.create_index([("sold_to",       ASCENDING)], background=True)

    await _db.categories.create_index([("is_active", ASCENDING)], background=True)

    # ── Seed mandatory join channel ────────────────────────────────────────────
    if not await _db.channels.find_one({"chat_id": "-1003980919319"}):
        cid = await get_next_id("channels")
        await _db.channels.insert_one({
            "_id": cid,
            "name": "Nexus_IO",
            "chat_id": "-1003980919319",
            "invite_link": "https://t.me/Nexus_IO",
        })
