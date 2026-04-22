"""
database.py - Async MongoDB interface using Motor
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import motor.motor_asyncio
from pymongo import ASCENDING


class Database:
    def __init__(self):
        self.client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
        self.db = None
        self.users = None

    async def connect(self):
        mongo_uri = os.environ["MONGO_URI"]
        self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.client[os.environ.get("DB_NAME", "compressor_bot")]
        self.users = self.db["users"]

        await self.users.create_index([("user_id", ASCENDING)], unique=True)
        await self.users.create_index([("expiry_date", ASCENDING)])
        print("✅ Connected to MongoDB Atlas")

    async def close(self):
        if self.client:
            self.client.close()

    # ── User CRUD ──────────────────────────────────────────────────────────────

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.users.find_one({"user_id": user_id})

    async def upsert_user(self, user_id: int, username: str = None, full_name: str = None) -> dict:
        now = datetime.now(timezone.utc)
        result = await self.users.find_one_and_update(
            {"user_id": user_id},
            {
                # Written ONCE on document creation only — never overlaps with $set
                "$setOnInsert": {
                    "user_id":        user_id,
                    "status":         "free",
                    "daily_usage":    0,
                    "usage_reset_at": now,
                    "expiry_date":    None,
                    "is_banned":      False,
                    "total_files":    0,
                    "created_at":     now,
                },
                # Refreshed on every interaction — keys are strictly disjoint from $setOnInsert
                "$set": {
                    "username":  username,
                    "full_name": full_name,
                    "last_seen": now,
                },
            },
            upsert=True,
            return_document=True,
        )
        return result

    async def is_banned(self, user_id: int) -> bool:
        user = await self.users.find_one({"user_id": user_id}, {"is_banned": 1})
        return bool(user and user.get("is_banned"))

    # ── Usage tracking ─────────────────────────────────────────────────────────

    async def increment_usage(self, user_id: int) -> int:
        """Increment daily usage counter, auto-reset after 24h. Returns new count."""
        now = datetime.now(timezone.utc)
        user = await self.get_user(user_id)
        if not user:
            return 0

        reset_at = user.get("usage_reset_at", now)
        if isinstance(reset_at, str):
            reset_at = datetime.fromisoformat(reset_at)
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)

        if (now - reset_at).total_seconds() >= 86400:
            await self.users.update_one(
                {"user_id": user_id},
                {"$set": {"daily_usage": 1, "usage_reset_at": now}},
            )
            return 1

        result = await self.users.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"daily_usage": 1, "total_files": 1}},
            return_document=True,
        )
        return result["daily_usage"]

    async def get_daily_usage(self, user_id: int) -> int:
        user = await self.get_user(user_id)
        if not user:
            return 0
        now = datetime.now(timezone.utc)
        reset_at = user.get("usage_reset_at", now)
        if isinstance(reset_at, str):
            reset_at = datetime.fromisoformat(reset_at)
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        if (now - reset_at).total_seconds() >= 86400:
            return 0
        return user.get("daily_usage", 0)

    # ── Premium management ─────────────────────────────────────────────────────

    async def approve_premium(self, user_id: int, days: int = 30) -> bool:
        expiry = datetime.now(timezone.utc) + timedelta(days=days)
        result = await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"status": "premium", "expiry_date": expiry}},
        )
        return result.modified_count > 0

    async def revoke_premium(self, user_id: int) -> bool:
        result = await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"status": "free", "expiry_date": None}},
        )
        return result.modified_count > 0

    async def ban_user(self, user_id: int) -> bool:
        result = await self.users.update_one(
            {"user_id": user_id}, {"$set": {"is_banned": True}}
        )
        return result.modified_count > 0

    async def unban_user(self, user_id: int) -> bool:
        result = await self.users.update_one(
            {"user_id": user_id}, {"$set": {"is_banned": False}}
        )
        return result.modified_count > 0

    # ── Subscription scheduler helpers ────────────────────────────────────────

    async def get_expiring_soon(self, hours: int = 48) -> list[dict]:
        """Return premium users whose subscription expires within `hours` hours."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        cursor = self.users.find(
            {
                "status": "premium",
                "expiry_date": {"$gt": now, "$lte": cutoff},
                "is_banned": False,
            },
            {"user_id": 1, "expiry_date": 1},
        )
        return await cursor.to_list(length=None)

    async def get_expired(self) -> list[dict]:
        """Return premium users whose subscription has already expired."""
        now = datetime.now(timezone.utc)
        cursor = self.users.find(
            {"status": "premium", "expiry_date": {"$lte": now}},
            {"user_id": 1},
        )
        return await cursor.to_list(length=None)

    async def expire_subscriptions(self) -> int:
        """Downgrade all expired premium users. Returns count."""
        now = datetime.now(timezone.utc)
        result = await self.users.update_many(
            {"status": "premium", "expiry_date": {"$lte": now}},
            {"$set": {"status": "free", "expiry_date": None}},
        )
        return result.modified_count

    # ── Stats ──────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        total = await self.users.count_documents({})
        premium = await self.users.count_documents({"status": "premium"})
        banned = await self.users.count_documents({"is_banned": True})

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        pipeline = [
            {"$match": {"usage_reset_at": {"$gte": day_start}}},
            {"$group": {"_id": None, "total": {"$sum": "$daily_usage"}}},
        ]
        agg = await self.users.aggregate(pipeline).to_list(length=1)
        files_today = agg[0]["total"] if agg else 0

        return {
            "total_users":   total,
            "premium_users": premium,
            "banned_users":  banned,
            "files_today":   files_today,
        }

    async def get_all_user_ids(self) -> list[int]:
        cursor = self.users.find({"is_banned": False}, {"user_id": 1})
        docs = await cursor.to_list(length=None)
        return [d["user_id"] for d in docs]


# Singleton
db = Database()