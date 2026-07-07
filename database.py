import asyncio
import math
import random
import time
from datetime import datetime
from typing import Optional, Dict, List

import aiosqlite

from config import (
    DEFAULT_MAX_AGE,
    DEFAULT_MIN_AGE,
    DEFAULT_SEARCH_RADIUS,
    FREE_DAILY_SUPERLIKES,
    PREMIUM_DAILY_SUPERLIKES,
    haversine_km,
    logger,
    start_of_today_ts,
)


class Database:
    def __init__(self, db_name="dating.db"):
        self.db_name = db_name
        self.db: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self):
        if self.db is not None:
            return
        self.db = await aiosqlite.connect(self.db_name, timeout=30)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.db.execute("PRAGMA synchronous=NORMAL;")
        await self.db.execute("PRAGMA foreign_keys=ON;")
        await self.db.execute("PRAGMA busy_timeout=5000;")
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    def _db(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Database is not connected")
        return self.db

    async def _execute_write(self, query: str, params: tuple = (), retries: int = 3):
        db = self._db()
        last_error = None

        for attempt in range(retries):
            try:
                async with self._write_lock:
                    cursor = await db.execute(query, params)
                    await db.commit()
                return cursor
            except aiosqlite.OperationalError as e:
                last_error = e
                if "locked" in str(e).lower() and attempt < retries - 1:
                    await asyncio.sleep(0.15 * (attempt + 1))
                    continue
                raise

        raise last_error  # pragma: no cover

    async def init_db(self):
        await self.connect()
        await self.init_tables()

    async def init_tables(self):
        db = self._db()

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                age INTEGER,
                bio TEXT,
                city TEXT,
                lat REAL,
                lon REAL,
                photo TEXT,
                likes INTEGER DEFAULT 0,
                created INTEGER DEFAULT 0,
                last_active INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                gender TEXT DEFAULT 'Не указано',
                search_gender TEXT DEFAULT 'Любой',
                username TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_until INTEGER DEFAULT 0,
                daily_superlikes INTEGER DEFAULT 1,
                extra_superlikes INTEGER DEFAULT 0,
                last_daily_reset INTEGER DEFAULT 0,
                search_radius INTEGER DEFAULT 100,
                min_age_search INTEGER DEFAULT 18,
                max_age_search INTEGER DEFAULT 100,
                photo_updated INTEGER DEFAULT 0,
                registration_ip TEXT DEFAULT '',
                last_ip TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER,
                to_id INTEGER,
                is_super INTEGER DEFAULT 0,
                created INTEGER DEFAULT 0,
                seen INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                UNIQUE(from_id, to_id)
            );

            CREATE TABLE IF NOT EXISTS views (
                viewer_id INTEGER,
                viewed_id INTEGER,
                created INTEGER,
                UNIQUE(viewer_id, viewed_id)
            );

            CREATE TABLE IF NOT EXISTS blocks (
                user_id INTEGER,
                blocked_id INTEGER,
                created INTEGER,
                UNIQUE(user_id, blocked_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                reported_id INTEGER,
                reason TEXT,
                created INTEGER,
                status TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS referrals (
                inviter_id INTEGER,
                invited_id INTEGER,
                created INTEGER,
                rewarded INTEGER DEFAULT 0,
                UNIQUE(invited_id)
            );

            CREATE TABLE IF NOT EXISTS stats (
                date TEXT PRIMARY KEY,
                new_users INTEGER DEFAULT 0,
                likes_count INTEGER DEFAULT 0,
                matches_count INTEGER DEFAULT 0,
                premium_purchases INTEGER DEFAULT 0,
                superlike_purchases INTEGER DEFAULT 0,
                revenue_stars INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount_stars INTEGER,
                payment_type TEXT,
                created INTEGER,
                telegram_payment_charge_id TEXT,
                provider_payment_charge_id TEXT,
                status TEXT DEFAULT 'completed'
            );

            CREATE INDEX IF NOT EXISTS idx_users_city ON users(city);
            CREATE INDEX IF NOT EXISTS idx_users_gender ON users(gender);
            CREATE INDEX IF NOT EXISTS idx_users_search_gender ON users(search_gender);
            CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);
            CREATE INDEX IF NOT EXISTS idx_users_created ON users(created);
            CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned);

            CREATE INDEX IF NOT EXISTS idx_likes_to ON likes(to_id, status);
            CREATE INDEX IF NOT EXISTS idx_likes_from ON likes(from_id);

            CREATE INDEX IF NOT EXISTS idx_blocks ON blocks(user_id, blocked_id);
            CREATE INDEX IF NOT EXISTS idx_blocks_blocked ON blocks(blocked_id);

            CREATE INDEX IF NOT EXISTS idx_views_viewer ON views(viewer_id, created);
            CREATE INDEX IF NOT EXISTS idx_views_viewed ON views(viewed_id);

            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_reports_reported ON reports(reported_id, status);

            CREATE INDEX IF NOT EXISTS idx_referrals_inviter ON referrals(inviter_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_inviter_rewarded ON referrals(inviter_id, rewarded);
        """)
        await db.commit()

        # Миграции для существующих баз
        for table, column, definition in [
            ("users", "name", "TEXT NOT NULL DEFAULT ''"),
            ("users", "age", "INTEGER"),
            ("users", "bio", "TEXT"),
            ("users", "city", "TEXT"),
            ("users", "lat", "REAL"),
            ("users", "lon", "REAL"),
            ("users", "photo", "TEXT"),
            ("users", "likes", "INTEGER DEFAULT 0"),
            ("users", "created", "INTEGER DEFAULT 0"),
            ("users", "last_active", "INTEGER DEFAULT 0"),
            ("users", "is_banned", "INTEGER DEFAULT 0"),
            ("users", "gender", "TEXT DEFAULT 'Не указано'"),
            ("users", "search_gender", "TEXT DEFAULT 'Любой'"),
            ("users", "username", "TEXT"),
            ("users", "is_premium", "INTEGER DEFAULT 0"),
            ("users", "premium_until", "INTEGER DEFAULT 0"),
            ("users", "daily_superlikes", "INTEGER DEFAULT 1"),
            ("users", "extra_superlikes", "INTEGER DEFAULT 0"),
            ("users", "last_daily_reset", "INTEGER DEFAULT 0"),
            ("users", "search_radius", "INTEGER DEFAULT 100"),
            ("users", "min_age_search", "INTEGER DEFAULT 18"),
            ("users", "max_age_search", "INTEGER DEFAULT 100"),
            ("users", "photo_updated", "INTEGER DEFAULT 0"),
            ("users", "registration_ip", "TEXT DEFAULT ''"),
            ("users", "last_ip", "TEXT DEFAULT ''"),
            ("likes", "seen", "INTEGER DEFAULT 0"),
            ("likes", "status", "TEXT DEFAULT 'pending'"),
            ("reports", "status", "TEXT DEFAULT 'pending'"),
            ("referrals", "rewarded", "INTEGER DEFAULT 0"),
        ]:
            await self._ensure_column(table, column, definition)

        logger.info("База данных инициализирована")

    async def _table_columns(self, table: str) -> List[str]:
        db = self._db()
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
            return [row["name"] for row in rows]

    async def _ensure_column(self, table: str, column: str, definition: str):
        columns = await self._table_columns(table)
        if column not in columns:
            logger.info(f"Применяем миграцию: добавляем колонку {column} в {table}")
            await self._execute_write(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logger.info(f"Колонка {column} добавлена")

    # ===== ПОЛЬЗОВАТЕЛИ =====
    async def get_user(self, uid) -> Optional[Dict]:
        async with self._db().execute("SELECT * FROM users WHERE id=?", (uid,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_user(self, data: Dict, inviter_id: Optional[int] = None):
        db = self._db()
        now = int(time.time())
        today = datetime.now().strftime("%Y-%m-%d")

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO users (
                        id, name, age, bio, city, lat, lon, photo, likes, created, last_active,
                        is_banned, gender, search_gender, username, is_premium, premium_until,
                        daily_superlikes, extra_superlikes, last_daily_reset, search_radius,
                        min_age_search, max_age_search, photo_updated, registration_ip, last_ip
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["id"],
                        data["name"],
                        data["age"],
                        data.get("bio", ""),
                        data.get("city", "Не указан"),
                        data.get("lat"),
                        data.get("lon"),
                        data.get("photo", ""),
                        0,
                        now,
                        now,
                        0,
                        data.get("gender", "Не указано"),
                        data.get("search_gender", "Любой"),
                        data.get("username", ""),
                        0,
                        0,
                        FREE_DAILY_SUPERLIKES,
                        0,
                        now,
                        data.get("search_radius", DEFAULT_SEARCH_RADIUS),
                        data.get("min_age_search", DEFAULT_MIN_AGE),
                        data.get("max_age_search", DEFAULT_MAX_AGE),
                        1 if data.get("photo") else 0,
                        data.get("registration_ip", ""),
                        data.get("last_ip", ""),
                    )
                )

                created = cursor.rowcount > 0
                if created:
                    await db.execute(
                        """INSERT INTO stats(date, new_users)
                           VALUES(?, 1)
                           ON CONFLICT(date) DO UPDATE SET new_users = new_users + 1""",
                        (today,)
                    )

                    # Реферальный бонус только если запись реально вставилась
                    if inviter_id is not None and inviter_id != data["id"]:
                        async with db.execute(
                            "SELECT 1 FROM users WHERE id=?",
                            (inviter_id,)
                        ) as inv_cursor:
                            inviter_exists = await inv_cursor.fetchone()

                        if inviter_exists:
                            ref_cursor = await db.execute(
                                """INSERT OR IGNORE INTO referrals(inviter_id, invited_id, created, rewarded)
                                   VALUES(?,?,?,1)""",
                                (inviter_id, data["id"], now)
                            )
                            if ref_cursor.rowcount > 0:
                                await db.execute(
                                    "UPDATE users SET extra_superlikes = extra_superlikes + 1 WHERE id = ?",
                                    (inviter_id,)
                                )

                await db.commit()
                return created
            except Exception as e:
                await db.rollback()
                logger.error(f"Create user error: {e}")
                return False

    async def update_user(self, uid, **kw):
        allowed = {
            "name", "age", "bio", "city", "lat", "lon", "photo", "likes",
            "last_active", "is_banned", "gender", "search_gender", "username",
            "is_premium", "premium_until", "daily_superlikes", "extra_superlikes",
            "last_daily_reset", "search_radius", "min_age_search", "max_age_search",
            "photo_updated", "registration_ip", "last_ip"
        }
        filtered = {k: v for k, v in kw.items() if k in allowed}
        if not filtered:
            return False

        set_clause = ", ".join(f"{k}=?" for k in filtered.keys())
        params = list(filtered.values()) + [uid]
        try:
            cursor = await self._execute_write(f"UPDATE users SET {set_clause} WHERE id=?", tuple(params))
            if cursor.rowcount > 0:
                return True
            return (await self.get_user(uid)) is not None
        except Exception as e:
            logger.error(f"Update user error: {e}")
            return False

    async def check_premium(self, uid):
        u = await self.get_user(uid)
        if not u:
            return False

        now = int(time.time())
        premium_until = int(u.get("premium_until", 0) or 0)
        is_premium = int(u.get("is_premium", 0) or 0)

        if is_premium and premium_until > now:
            return True

        if premium_until > now and not is_premium:
            await self.update_user(
                uid,
                is_premium=1,
                daily_superlikes=PREMIUM_DAILY_SUPERLIKES
            )
            return True

        if is_premium and premium_until <= now:
            await self.update_user(
                uid,
                is_premium=0,
                premium_until=0,
                daily_superlikes=FREE_DAILY_SUPERLIKES
            )
        return False

    async def give_premium(self, uid, days=30):
        u = await self.get_user(uid)
        if not u:
            return False

        now = int(time.time())
        current_premium = int(u.get("premium_until", 0) or 0)
        start_time = max(now, current_premium)
        premium_until = start_time + (days * 86400)

        return await self.update_user(
            uid,
            is_premium=1,
            premium_until=premium_until,
            daily_superlikes=PREMIUM_DAILY_SUPERLIKES,
            last_daily_reset=now
        )

    async def give_superlikes(self, uid, count):
        user = await self.get_user(uid)
        if not user:
            return False
        current = int(user.get("extra_superlikes", 0) or 0)
        return await self.update_user(uid, extra_superlikes=current + count)

    async def get_available_superlikes(self, uid):
        u = await self.get_user(uid)
        if not u:
            return 0
        return int(u.get("daily_superlikes", 0) or 0) + int(u.get("extra_superlikes", 0) or 0)

    async def use_superlike(self, uid):
        u = await self.get_user(uid)
        if not u:
            return False

        daily = int(u.get("daily_superlikes", 0) or 0)
        extra = int(u.get("extra_superlikes", 0) or 0)

        if daily > 0:
            return await self.update_user(uid, daily_superlikes=daily - 1)
        if extra > 0:
            return await self.update_user(uid, extra_superlikes=extra - 1)
        return False

    async def reset_daily_superlikes(self):
        now = int(time.time())
        today_ts = start_of_today_ts()
        db = self._db()

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """
                    UPDATE users
                    SET daily_superlikes = CASE
                        WHEN is_premium = 1 AND premium_until > ? THEN ?
                        ELSE ?
                    END,
                    last_daily_reset = ?
                    WHERE last_daily_reset < ?
                    """,
                    (now, PREMIUM_DAILY_SUPERLIKES, FREE_DAILY_SUPERLIKES, now, today_ts)
                )
                await db.commit()
                return True
            except Exception as e:
                await db.rollback()
                logger.error(f"Reset daily superlikes error: {e}")
                return False

    # ===== ПЛАТЕЖИ =====
    async def add_payment(self, user_id, amount_stars, payment_type, telegram_charge_id, provider_charge_id):
        db = self._db()
        today = datetime.now().strftime("%Y-%m-%d")

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """INSERT INTO payments
                       (user_id, amount_stars, payment_type, created, telegram_payment_charge_id, provider_payment_charge_id)
                       VALUES (?,?,?,?,?,?)""",
                    (user_id, amount_stars, payment_type, int(time.time()), telegram_charge_id, provider_charge_id)
                )
                if payment_type == "premium_pack":
                    await db.execute(
                        """INSERT INTO stats(date, premium_purchases, revenue_stars)
                           VALUES(?, 1, ?)
                           ON CONFLICT(date) DO UPDATE SET
                               premium_purchases = premium_purchases + 1,
                               revenue_stars = revenue_stars + ?""",
                        (today, amount_stars, amount_stars)
                    )
                elif payment_type == "superlikes_pack":
                    await db.execute(
                        """INSERT INTO stats(date, superlike_purchases, revenue_stars)
                           VALUES(?, 1, ?)
                           ON CONFLICT(date) DO UPDATE SET
                               superlike_purchases = superlike_purchases + 1,
                               revenue_stars = revenue_stars + ?""",
                        (today, amount_stars, amount_stars)
                    )
                await db.commit()
                return True
            except Exception as e:
                await db.rollback()
                logger.error(f"Add payment error: {e}")
                return False

    async def get_payment_history(self, uid, limit=10):
        async with self._db().execute(
            "SELECT * FROM payments WHERE user_id=? ORDER BY created DESC LIMIT ?",
            (uid, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(x) for x in rows]

    # ===== ЛАЙКИ =====
    async def add_like(self, from_id, to_id, is_super=False):
        if from_id == to_id:
            return "self_like"

        db = self._db()
        today = datetime.now().strftime("%Y-%m-%d")

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")

                async with db.execute(
                    "SELECT id, is_banned, daily_superlikes, extra_superlikes FROM users WHERE id=?",
                    (from_id,)
                ) as c1:
                    user_from = await c1.fetchone()

                async with db.execute(
                    "SELECT id, is_banned FROM users WHERE id=?",
                    (to_id,)
                ) as c2:
                    user_to = await c2.fetchone()

                if not user_from or not user_to:
                    await db.rollback()
                    return "no_profile"

                if int(user_from["is_banned"]) == 1 or int(user_to["is_banned"]) == 1:
                    await db.rollback()
                    return "blocked"

                async with db.execute(
                    """
                    SELECT 1
                    FROM blocks
                    WHERE (user_id=? AND blocked_id=?)
                       OR (user_id=? AND blocked_id=?)
                    LIMIT 1
                    """,
                    (from_id, to_id, to_id, from_id)
                ) as c3:
                    if await c3.fetchone():
                        await db.rollback()
                        return "blocked"

                daily = int(user_from["daily_superlikes"] or 0)
                extra = int(user_from["extra_superlikes"] or 0)

                if is_super and daily + extra <= 0:
                    await db.rollback()
                    return "no_superlikes"

                await db.execute(
                    "INSERT INTO likes(from_id, to_id, is_super, created) VALUES(?,?,?,?)",
                    (from_id, to_id, 1 if is_super else 0, int(time.time()))
                )
                await db.execute("UPDATE users SET likes = likes + 1 WHERE id = ?", (to_id,))

                if is_super:
                    if daily > 0:
                        await db.execute(
                            "UPDATE users SET daily_superlikes = daily_superlikes - 1 WHERE id = ?",
                            (from_id,)
                        )
                    else:
                        await db.execute(
                            "UPDATE users SET extra_superlikes = extra_superlikes - 1 WHERE id = ?",
                            (from_id,)
                        )

                await db.execute(
                    """INSERT INTO stats(date, likes_count)
                       VALUES(?, 1)
                       ON CONFLICT(date) DO UPDATE SET likes_count = likes_count + 1""",
                    (today,)
                )
                await db.commit()
                return True

            except aiosqlite.IntegrityError:
                await db.rollback()
                return False
            except Exception as e:
                await db.rollback()
                logger.error(f"Add like error: {e}")
                return False

    async def accept_like(self, from_id, to_id):
        db = self._db()
        today = datetime.now().strftime("%Y-%m-%d")

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE likes SET status='accepted', seen=1 WHERE from_id=? AND to_id=? AND status='pending'",
                    (from_id, to_id)
                )
                if cursor.rowcount == 0:
                    await db.rollback()
                    return False

                async with db.execute(
                    """SELECT COUNT(*) AS cnt
                       FROM likes
                       WHERE ((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))
                         AND status='accepted'""",
                    (from_id, to_id, to_id, from_id)
                ) as c2:
                    row = await c2.fetchone()
                    if row and row["cnt"] == 2:
                        await db.execute(
                            """INSERT INTO stats(date, matches_count)
                               VALUES(?, 1)
                               ON CONFLICT(date) DO UPDATE SET matches_count = matches_count + 1""",
                            (today,)
                        )
                await db.commit()
                return True
            except Exception as e:
                await db.rollback()
                logger.error(f"Accept like error: {e}")
                return False

    async def reject_like(self, from_id, to_id):
        db = self._db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE likes SET status='rejected', seen=1 WHERE from_id=? AND to_id=? AND status='pending'",
                    (from_id, to_id)
                )
                await db.commit()
                return cursor.rowcount > 0
            except Exception as e:
                await db.rollback()
                logger.error(f"Reject like error: {e}")
                return False

    async def get_mutual_likes(self, uid):
        async with self._db().execute("""
            SELECT DISTINCT u.id, u.name, u.age, u.city, u.photo, u.username
            FROM likes l1
            JOIN likes l2
              ON l1.from_id = l2.to_id
             AND l1.to_id = l2.from_id
            JOIN users u
              ON u.id = l2.from_id
            WHERE l1.from_id = ?
              AND l1.status = 'accepted'
              AND l2.status = 'accepted'
              AND u.is_banned = 0
        """, (uid,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(x) for x in rows]

    async def get_likes_received(self, uid, limit=10):
        async with self._db().execute("""
            SELECT u.id, u.name, u.age, u.city, u.photo, u.username,
                   l.is_super, l.created, l.id as like_id
            FROM likes l
            JOIN users u ON l.from_id = u.id
            WHERE l.to_id = ?
              AND l.seen = 0
              AND l.status = 'pending'
              AND u.is_banned = 0
              AND NOT EXISTS(
                  SELECT 1 FROM blocks
                  WHERE (user_id=? AND blocked_id=u.id)
                     OR (user_id=u.id AND blocked_id=?)
              )
            ORDER BY l.created DESC
            LIMIT ?
        """, (uid, uid, uid, limit)) as cursor:
            rows = await cursor.fetchall()
            return [dict(x) for x in rows]

    async def mark_like_seen(self, like_id: int):
        await self._execute_write(
            "UPDATE likes SET seen=1 WHERE id=?",
            (like_id,)
        )

    # ===== БЛОКИРОВКИ / ПРОСМОТРЫ / ЖАЛОБЫ =====
    async def block_user(self, uid, bid):
        if uid == bid:
            return False

        user_a = await self.get_user(uid)
        user_b = await self.get_user(bid)
        if not user_a or not user_b:
            return False

        db = self._db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT OR IGNORE INTO blocks(user_id, blocked_id, created) VALUES(?,?,?)",
                    (uid, bid, int(time.time()))
                )
                await db.execute(
                    "DELETE FROM likes WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)",
                    (uid, bid, bid, uid)
                )
                await db.commit()
                return True
            except Exception as e:
                await db.rollback()
                logger.error(f"Block user error: {e}")
                return False

    async def is_blocked(self, uid, oid):
        async with self._db().execute(
            "SELECT 1 FROM blocks WHERE (user_id=? AND blocked_id=?) OR (user_id=? AND blocked_id=?)",
            (uid, oid, oid, uid)
        ) as cursor:
            return bool(await cursor.fetchone())

    async def add_view(self, vid, vid2):
        try:
            await self._execute_write(
                """INSERT INTO views(viewer_id, viewed_id, created)
                   VALUES(?,?,?)
                   ON CONFLICT(viewer_id, viewed_id) DO UPDATE SET created=excluded.created""",
                (vid, vid2, int(time.time()))
            )
        except Exception:
            pass

    async def add_report(self, reporter_id: int, reported_id: int, reason: str):
        if reporter_id == reported_id:
            return False

        reporter = await self.get_user(reporter_id)
        reported = await self.get_user(reported_id)
        if not reporter or not reported:
            return False

        db = self._db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO reports(reporter_id, reported_id, reason, created) VALUES(?,?,?,?)",
                    (reporter_id, reported_id, reason, int(time.time()))
                )
                await db.commit()
                return True
            except Exception as e:
                await db.rollback()
                logger.error(f"Add report error: {e}")
                return False

    async def get_next_profile(self, uid):
        viewer = await self.get_user(uid)
        if not viewer:
            return None

        min_age = int(viewer.get("min_age_search", DEFAULT_MIN_AGE) or DEFAULT_MIN_AGE)
        max_age = int(viewer.get("max_age_search", DEFAULT_MAX_AGE) or DEFAULT_MAX_AGE)
        if min_age > max_age:
            min_age, max_age = max_age, min_age

        search_gender = viewer.get("search_gender", "Любой")

        radius_value = viewer.get("search_radius")
        try:
            search_radius = DEFAULT_SEARCH_RADIUS if radius_value is None else int(radius_value)
        except (TypeError, ValueError):
            search_radius = DEFAULT_SEARCH_RADIUS

        query = """
            SELECT u.*
            FROM users u
            WHERE u.id != ?
              AND u.is_banned = 0
              AND u.photo IS NOT NULL
              AND u.photo != ''
              AND u.age BETWEEN ? AND ?
              AND NOT EXISTS(
                  SELECT 1 FROM views
                  WHERE viewer_id = ?
                    AND viewed_id = u.id
                    AND created > ?
              )
              AND NOT EXISTS(
                  SELECT 1 FROM blocks
                  WHERE (user_id = ? AND blocked_id = u.id)
                     OR (user_id = u.id AND blocked_id = ?)
              )
              AND NOT EXISTS(
                  SELECT 1 FROM likes
                  WHERE from_id = ?
                    AND to_id = u.id
              )
        """
        params = [
            uid,
            min_age,
            max_age,
            uid,
            int(time.time()) - 30 * 86400,
            uid,
            uid,
            uid,
        ]

        if search_gender != "Любой":
            query += " AND u.gender = ?"
            params.append(search_gender)

        viewer_lat = viewer.get("lat")
        viewer_lon = viewer.get("lon")
        if viewer_lat is not None and viewer_lon is not None and search_radius > 0:
            viewer_lat = float(viewer_lat)
            viewer_lon = float(viewer_lon)
            delta_lat = search_radius / 111.0
            cos_lat = max(math.cos(math.radians(viewer_lat)), 0.01)
            delta_lon = search_radius / (111.320 * cos_lat)

            lat_min = max(-90.0, viewer_lat - delta_lat)
            lat_max = min(90.0, viewer_lat + delta_lat)
            lon_min = max(-180.0, viewer_lon - delta_lon)
            lon_max = min(180.0, viewer_lon + delta_lon)

            query += """
              AND u.lat IS NOT NULL
              AND u.lon IS NOT NULL
              AND u.lat BETWEEN ? AND ?
              AND u.lon BETWEEN ? AND ?
            """
            params.extend([lat_min, lat_max, lon_min, lon_max])

        query += " ORDER BY u.last_active DESC, u.created DESC LIMIT 500"

        async with self._db().execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()

        candidates = [dict(x) for x in rows]

        if viewer_lat is not None and viewer_lon is not None and search_radius > 0:
            filtered = []
            for c in candidates:
                if c.get("lat") is None or c.get("lon") is None:
                    continue
                dist = haversine_km(
                    float(viewer_lat),
                    float(viewer_lon),
                    float(c["lat"]),
                    float(c["lon"])
                )
                if dist <= search_radius:
                    filtered.append(c)
            candidates = filtered

        if not candidates:
            return None

        return random.choice(candidates)

    # ===== РЕФЕРАЛЫ / АДМИН =====
    async def get_total_revenue(self):
        async with self._db().execute("SELECT SUM(amount_stars) as total FROM payments") as cursor:
            row = await cursor.fetchone()
            return int(row["total"] or 0) if row else 0

    async def get_users_count(self):
        async with self._db().execute("SELECT COUNT(*) as cnt FROM users") as cursor:
            row = await cursor.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_today_users(self):
        today = start_of_today_ts()
        async with self._db().execute("SELECT COUNT(*) as cnt FROM users WHERE created >= ?", (today,)) as cursor:
            row = await cursor.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_active_today(self):
        today = start_of_today_ts()
        async with self._db().execute("SELECT COUNT(*) as cnt FROM users WHERE last_active >= ?", (today,)) as cursor:
            row = await cursor.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_total_likes(self):
        async with self._db().execute("SELECT COUNT(*) as cnt FROM likes") as cursor:
            row = await cursor.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_matches(self):
        async with self._db().execute("""
            SELECT COUNT(DISTINCT CASE
                WHEN l1.from_id < l1.to_id
                THEN l1.from_id || '-' || l1.to_id
                ELSE l1.to_id || '-' || l1.from_id
            END) as cnt
            FROM likes l1
            JOIN likes l2
              ON l1.from_id = l2.to_id
             AND l1.to_id = l2.from_id
            WHERE l1.status='accepted'
              AND l2.status='accepted'
        """) as cursor:
            row = await cursor.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_reports_count(self):
        async with self._db().execute("SELECT COUNT(*) as cnt FROM reports") as cursor:
            row = await cursor.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_all_users(self, limit=100, offset=0):
        async with self._db().execute(
            "SELECT id, name, age, city, is_banned, username, is_premium FROM users ORDER BY created DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(x) for x in rows]

    async def ban_user(self, uid):
        return await self.update_user(uid, is_banned=1)

    async def unban_user(self, uid):
        return await self.update_user(uid, is_banned=0)
