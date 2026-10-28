import asyncio
import html
import logging
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.crypto.authkey import AuthKey
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reactions")
DC_SERVERS = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    admin_id: int
    invite_hash: str
    dialoghub_db: Path
    sessions_dir: Path
    state_db: Path
    poll_seconds: int
    delay_min: float
    delay_max: float
    default_reactions: tuple[str, ...]

    @classmethod
    def from_env(cls):
        required = ("BOT_TOKEN", "API_ID", "API_HASH", "ADMIN_ID", "INVITE_HASH")
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise RuntimeError("Missing: " + ", ".join(missing))
        return cls(
            os.environ["BOT_TOKEN"], int(os.environ["API_ID"]), os.environ["API_HASH"],
            int(os.environ["ADMIN_ID"]), os.environ["INVITE_HASH"],
            Path(os.getenv("DIALOGHUB_DB", "/opt/dialoghub/data/dialoghub.sqlite3")),
            Path(os.getenv("DIALOGHUB_SESSIONS", "/root/FULL_CRM/accounts")),
            Path(os.getenv("STATE_DB", "/opt/tg-channel-reaction-bot/data/state.sqlite3")),
            int(os.getenv("POLL_SECONDS", "30")),
            float(os.getenv("DELAY_MIN_SECONDS", "5")),
            float(os.getenv("DELAY_MAX_SECONDS", "15")),
            tuple(x.strip() for x in os.getenv("DEFAULT_REACTIONS", "👍,❤️,🔥,👏,🎉,😍").split(",") if x.strip()),
        )


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
                name TEXT PRIMARY KEY, title TEXT NOT NULL, user_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, membership TEXT NOT NULL DEFAULT 'pending',
                channel_id INTEGER, access_hash INTEGER, premium INTEGER,
                error TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, manual INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deliveries(
                post_id INTEGER NOT NULL, account_name TEXT NOT NULL, status TEXT NOT NULL,
                reactions TEXT, error TEXT, updated_at INTEGER NOT NULL,
                PRIMARY KEY(post_id,account_name));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(accounts)")}
        if "premium" not in columns:
            self.db.execute("ALTER TABLE accounts ADD COLUMN premium INTEGER")
        post_columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
        if "manual" not in post_columns:
            self.db.execute("ALTER TABLE posts ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.db.commit()

    def sync_accounts(self, cfg):
        source = sqlite3.connect(f"file:{cfg.dialoghub_db}?mode=ro", uri=True)
        try:
            rows = source.execute("SELECT session_name,title FROM accounts WHERE enabled=1 ORDER BY id").fetchall()
        finally:
            source.close()
        active = []
        for name, title in rows:
            path = cfg.sessions_dir / f"{name}.session"
            if not path.exists():
                log.warning("Session missing for %s", name)
                continue
            try:
                session_db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    user_id = session_db.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()[0]
                finally:
                    session_db.close()
            except (sqlite3.Error, TypeError) as exc:
                log.warning("Session unreadable for %s: %s", name, type(exc).__name__)
                continue
            active.append(name)
            self.db.execute("""INSERT INTO accounts(name,title,user_id,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET title=excluded.title,user_id=excluded.user_id,
                enabled=1,updated_at=excluded.updated_at""", (name, title or name, user_id, int(time.time())))
        self.db.execute("UPDATE accounts SET enabled=0")
        if active:
            self.db.executemany("UPDATE accounts SET enabled=1 WHERE name=?", [(x,) for x in active])
        self.db.commit()

    def accounts(self, membership=None):
        if membership:
            return self.db.execute("SELECT * FROM accounts WHERE enabled=1 AND membership=? ORDER BY name", (membership,)).fetchall()
        return self.db.execute("SELECT * FROM accounts WHERE enabled=1 ORDER BY name").fetchall()

    def mark_member(self, name, chat, premium):
        self.db.execute("""UPDATE accounts SET membership='joined',channel_id=?,access_hash=?,premium=?,error=NULL,updated_at=? WHERE name=?""",
                        (chat.id, chat.access_hash, int(premium), int(time.time()), name))
        self.db.commit()

    def mark_excluded(self, name, error):
        self.db.execute("UPDATE accounts SET membership='excluded',error=?,updated_at=? WHERE name=?",
                        (error[:400], int(time.time()), name))
        self.db.commit()

    def queue(self, post_id, manual=False):
        self.db.execute("""INSERT INTO posts(id,created_at,manual) VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET manual=max(posts.manual,excluded.manual)""",
            (post_id, int(time.time()), int(manual)))
        self.db.commit()

    def pending_posts(self):
        return self.db.execute("""SELECT p.id FROM posts p WHERE EXISTS(
            SELECT 1 FROM accounts a LEFT JOIN deliveries d
              ON d.post_id=p.id AND d.account_name=a.name
            WHERE a.enabled=1 AND a.membership='joined'
              AND (d.status IS NULL OR (d.status='error' AND d.updated_at<?)))
            AND (p.manual=1 OR p.id>?) ORDER BY p.id""",
            (int(time.time()) - 900, int(self.meta("start_from", "0")))).fetchall()

    def delivered(self, post_id, name):
        row = self.db.execute("SELECT status FROM deliveries WHERE post_id=? AND account_name=?", (post_id, name)).fetchone()
        return row is not None and row[0] == "done"

    def delivery(self, post_id, name, status, reactions="", error=""):
        self.db.execute("""INSERT INTO deliveries VALUES(?,?,?,?,?,?)
            ON CONFLICT(post_id,account_name) DO UPDATE SET status=excluded.status,
            reactions=excluded.reactions,error=excluded.error,updated_at=excluded.updated_at""",
            (post_id, name, status, reactions, error[:400], int(time.time())))
        self.db.commit()

    def summary(self):
        result = {r[0]: r[1] for r in self.db.execute("SELECT membership,count(*) FROM accounts WHERE enabled=1 GROUP BY membership")}
        result["posts"] = self.db.execute("SELECT count(*) FROM posts").fetchone()[0]
        result["done"] = self.db.execute("SELECT count(*) FROM deliveries WHERE status='done'").fetchone()[0]
        result["errors"] = self.db.execute("SELECT count(*) FROM deliveries WHERE status='error'").fetchone()[0]
        return result


def client_from_pyrogram(cfg: Config, name: str):
    path = cfg.sessions_dir / f"{name}.session"
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dc_id, auth_key = source.execute("SELECT dc_id,auth_key FROM sessions LIMIT 1").fetchone()
    finally:
        source.close()
    session = MemorySession()
    session.set_dc(dc_id, DC_SERVERS[dc_id], 443)
    session.auth_key = AuthKey(auth_key)
    return TelegramClient(session, cfg.api_id, cfg.api_hash, receive_updates=False, auto_reconnect=False)


def reaction_key(reaction):
    if isinstance(reaction, types.ReactionEmoji):
        return ("emoji", reaction.emoticon)
    if isinstance(reaction, types.ReactionCustomEmoji):
        return ("custom", reaction.document_id)
    return ("other", str(reaction))


def allowed_reactions(full, defaults):
    setting = full.full_chat.available_reactions
    if isinstance(setting, types.ChatReactionsSome):
        return list(setting.reactions)
    if isinstance(setting, types.ChatReactionsAll):
        return [types.ReactionEmoji(emoticon=x) for x in defaults]
    return []


def choose_reactions(options, message, max_unique, target_count):
    """Keep the user's selections and stay within the channel's unique-reaction cap."""
    counts = message.reactions.results if message.reactions else []
    existing = [x.reaction for x in counts if x.chosen_order is not None]
    if len(existing) >= target_count:
        return existing[:target_count]
    global_keys = {reaction_key(x.reaction) for x in counts}
    already_used = {reaction_key(x) for x in existing}
    available = [x for x in options if reaction_key(x) not in already_used]
    existing_global = [x for x in available if reaction_key(x) in global_keys]
    new_global = [x for x in available if reaction_key(x) not in global_keys]
    slots = max(0, max_unique - len(global_keys)) if max_unique else len(new_global)
    pool = existing_global + random.sample(new_global, min(slots, len(new_global)))
    needed = target_count - len(existing)
    if len(pool) < needed:
        raise ValueError("not enough allowed reaction types")
    return existing + random.sample(pool, needed)


async def check_or_join(client, invite_hash, do_join):
    invitation = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
    if isinstance(invitation, types.ChatInviteAlready):
        return invitation.chat
    if not do_join:
        return None
    result = await client(functions.messages.ImportChatInviteRequest(hash=invite_hash))
    updates = getattr(result, "updates", result)
    channels = [x for x in updates.chats if isinstance(x, types.Channel)]
    if not channels:
        raise RuntimeError("join did not return a channel")
    return channels[0]


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.state_db)
        self.store.sync_accounts(cfg)
        self.prepare_task = None

    async def join_pending(self, first_only=False):
        pending = self.store.accounts("pending")
        for row in pending:
            client = client_from_pyrogram(self.cfg, row["name"])
            try:
                await client.connect()
                me = await client.get_me()
                chat = await check_or_join(client, self.cfg.invite_hash, True)
                if not isinstance(chat, types.Channel) or chat.access_hash is None:
                    raise RuntimeError("joined entity has no channel access hash")
                self.store.mark_member(row["name"], chat, bool(me and me.premium))
                log.info("Joined %s (Premium: %s)", row["title"], bool(me and me.premium))
                if first_only:
                    return
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.store.mark_excluded(row["name"], error)
                log.warning("Excluded %s: %s", row["title"], error)
            finally:
                await client.disconnect()
            if not first_only:
                await asyncio.sleep(random.uniform(1.5, 3.5))

    async def prepare_and_report(self):
        try:
            await self.join_pending()
            message = "Подключение завершено.\n" + self.status_text()
            excluded = self.store.accounts("excluded")
            if excluded:
                message += "\n\nНе удалось подключить:\n" + "\n".join(
                    f"• {x['title']}: {x['error']}" for x in excluded)
            async with aiohttp.ClientSession() as http:
                await http.post(
                    f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage",
                    json={"chat_id": self.cfg.admin_id, "text": message[:3900]},
                )
        except Exception:
            log.exception("Account preparation failed")

    async def with_monitor(self, callback):
        joined = self.store.accounts("joined")
        if not joined:
            return None
        for row in joined:
            client = client_from_pyrogram(self.cfg, row["name"])
            try:
                await client.connect()
                peer = types.InputPeerChannel(row["channel_id"], row["access_hash"])
                return await callback(client, peer)
            except Exception as exc:
                log.warning("Monitor unavailable via %s: %s", row["title"], exc)
            finally:
                await client.disconnect()
        return None

    async def baseline(self):
        async def latest(client, peer):
            messages = await client.get_messages(peer, limit=1)
            return messages[0].id if messages else 0
        post_id = await self.with_monitor(latest)
        if post_id is not None:
            self.store.set_meta("last_seen", post_id)
            self.store.set_meta("start_from", post_id)
        return post_id

    async def scan_posts(self, last_five=False):
        async def scan(client, peer):
            if last_five or self.store.meta("last_seen") is None:
                messages = await client.get_messages(peer, limit=5)
                posts = [m for m in messages if isinstance(m, types.Message)]
                for m in posts:
                    self.store.queue(m.id, manual=last_five)
                if posts:
                    latest = max(m.id for m in posts)
                    current = int(self.store.meta("last_seen", "0"))
                    self.store.set_meta("last_seen", max(latest, current))
                return len(posts)
            cursor = int(self.store.meta("last_seen", "0"))
            posts = []
            async for m in client.iter_messages(peer, min_id=cursor, reverse=True):
                if isinstance(m, types.Message):
                    posts.append(m)
            for m in posts:
                self.store.queue(m.id)
            if posts:
                self.store.set_meta("last_seen", max(m.id for m in posts))
            return len(posts)
        return await self.with_monitor(scan)

    async def process_post(self, post_id):
        async def get_allowed(client, peer):
            full = await client(functions.channels.GetFullChannelRequest(channel=peer))
            return allowed_reactions(full, self.cfg.default_reactions), getattr(full.full_chat, "reactions_limit", None)
        data = await self.with_monitor(get_allowed)
        options, max_unique = data if data else (None, None)
        if not options:
            log.warning("Post %s: no available reactions", post_id)
            return
        for row in self.store.accounts("joined"):
            if self.store.meta("paused", "1") == "1":
                return
            if self.store.delivered(post_id, row["name"]):
                continue
            client = client_from_pyrogram(self.cfg, row["name"])
            try:
                await client.connect()
                peer = types.InputPeerChannel(row["channel_id"], row["access_hash"])
                msg = await client.get_messages(peer, ids=post_id)
                if not isinstance(msg, types.Message):
                    self.store.delivery(post_id, row["name"], "done", error="post unavailable")
                    continue
                me = await client.get_me()
                target_count = min(3 if me and me.premium else 1, len(options))
                selected = choose_reactions(options, msg, max_unique, target_count)
                existing = [x.reaction for x in msg.reactions.results if x.chosen_order is not None] if msg.reactions else []
                if len(existing) < target_count:
                    try:
                        await client(functions.messages.SendReactionRequest(peer=peer, msg_id=post_id, reaction=selected))
                    except FloodWaitError as exc:
                        log.warning("Flood wait for %s: %ss", row["title"], exc.seconds)
                        await asyncio.sleep(exc.seconds + 1)
                        await client(functions.messages.SendReactionRequest(peer=peer, msg_id=post_id, reaction=selected))
                result = ",".join(str(reaction_key(x)[1]) for x in selected)
                self.store.delivery(post_id, row["name"], "done", result)
                log.info("Reacted %s post %s: %s", row["title"], post_id, result)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.store.delivery(post_id, row["name"], "error", error=error)
                log.warning("Reaction failed %s post %s: %s", row["title"], post_id, error)
            finally:
                await client.disconnect()
            await asyncio.sleep(random.uniform(self.cfg.delay_min, self.cfg.delay_max))

    async def worker(self):
        while True:
            try:
                if self.store.meta("paused", "1") == "1":
                    await asyncio.sleep(5)
                    continue
                self.store.sync_accounts(self.cfg)
                await self.scan_posts()
                for row in self.store.pending_posts():
                    if self.store.meta("paused", "1") == "1":
                        break
                    await self.process_post(row["id"])
            except Exception:
                log.exception("Worker iteration failed")
            await asyncio.sleep(self.cfg.poll_seconds)

    def status_text(self):
        s = self.store.summary()
        return (f"Канал: подключено {s.get('joined', 0)}, исключено {s.get('excluded', 0)}, "
                f"ожидает {s.get('pending', 0)}.\nПостов: {s['posts']}. Успешных действий: {s['done']}. "
                f"Ошибок реакций: {s['errors']}.\nРежим: {'пауза' if self.store.meta('paused','1')=='1' else 'работает'}.")

    async def bot(self):
        base = f"https://api.telegram.org/bot{self.cfg.bot_token}"
        offset = int(self.store.meta("bot_offset", "0"))
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            while True:
                try:
                    async with http.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'}) as resp:
                        result = await resp.json()
                    if not result.get("ok"):
                        raise RuntimeError(f"Bot API getUpdates failed: {result.get('error_code')}")
                    for update in result["result"]:
                        offset = update["update_id"] + 1
                        self.store.set_meta("bot_offset", offset)
                        message = update.get("message", {})
                        sender = message.get("from", {}).get("id")
                        if sender != self.cfg.admin_id:
                            continue
                        command = message.get("text", "").split()[0].split("@")[0]
                        reply = None
                        if command in ("/start", "/status"):
                            reply = self.status_text() + "\n\nКоманды: /status /accounts /excluded /prepare /pause /resume /last5"
                        elif command == "/accounts":
                            rows = self.store.accounts()
                            reply = "Аккаунты:\n" + "\n".join(f"• {html.escape(x['title'])}: {x['membership']}, {'Premium' if x['premium'] else 'обычный' if x['premium']==0 else 'не проверен'}" for x in rows)
                        elif command == "/excluded":
                            rows = self.store.accounts("excluded")
                            reply = "Не удалось подключить:\n" + ("\n".join(f"• {html.escape(x['title'])}: {html.escape(x['error'] or '')}" for x in rows) if rows else "нет")
                        elif command == "/pause":
                            self.store.set_meta("paused", "1")
                            reply = "Поставлено на паузу."
                        elif command == "/prepare":
                            if self.store.meta("paused", "1") != "1":
                                reply = "Сначала поставьте на паузу командой /pause."
                            elif self.prepare_task and not self.prepare_task.done():
                                reply = "Подключение уже идёт. Текущий результат: /status."
                            else:
                                self.prepare_task = asyncio.create_task(self.prepare_and_report())
                                reply = "Подключение аккаунтов запущено. Реакции не ставятся; смотрите /status и /excluded."
                        elif command == "/resume":
                            if self.prepare_task and not self.prepare_task.done():
                                reply = "Подключение аккаунтов ещё идёт. Дождитесь завершения /prepare."
                            elif self.store.accounts("pending"):
                                reply = "Есть неподключённые аккаунты. Сначала выполните /prepare."
                            elif not self.store.accounts("joined"):
                                reply = "Нет доступных аккаунтов в канале. Проверьте /excluded."
                            else:
                                baseline_id = await self.baseline()
                                if baseline_id is None:
                                    reply = "Не удалось прочитать канал; работа осталась на паузе."
                                else:
                                    self.store.set_meta("paused", "0")
                                    reply = f"Работа включена. Посты до #{baseline_id} автоматически не обрабатываются."
                        elif command == "/last5":
                            if self.store.meta("paused", "1") == "1":
                                reply = "Сначала запустите работу командой /resume."
                            else:
                                count = await self.scan_posts(last_five=True)
                                reply = f"В очередь добавлены последние пять постов: {count or 0}. Уже обработанные аккаунты не повторяются."
                        if reply:
                            async with http.post(f"{base}/sendMessage", json={"chat_id": message["chat"]["id"], "text": reply[:3900], "parse_mode": "HTML"}) as resp:
                                await resp.read()
                except Exception as exc:
                    log.error("Control bot iteration failed: %s", type(exc).__name__)
                    await asyncio.sleep(5)


async def main():
    cfg = Config.from_env()
    service = Service(cfg)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "inspect":
        print("Dialog Hub accounts:", len(service.store.accounts()))
        first = service.store.accounts()[0]
        client = client_from_pyrogram(cfg, first["name"])
        try:
            await client.connect()
            await client.get_me()
            chat = await check_or_join(client, cfg.invite_hash, False)
            if chat:
                print("First account is already a member of:", chat.title)
                full = await client(functions.channels.GetFullChannelRequest(channel=chat))
                print("Allowed reactions:", [reaction_key(x) for x in allowed_reactions(full, cfg.default_reactions)])
                print("Recent post IDs:", [m.id for m in await client.get_messages(chat, limit=5) if isinstance(m, types.Message)])
            else:
                print("First account is not a member; invitation is valid.")
        finally:
            await client.disconnect()
    elif cmd == "join-first":
        await service.join_pending(first_only=True)
        print(service.status_text())
    elif cmd == "run":
        await asyncio.gather(service.worker(), service.bot())
    else:
        raise SystemExit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    asyncio.run(main())
