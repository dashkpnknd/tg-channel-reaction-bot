import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon import types

from app import Store, choose_reactions, distinct_posts, reaction_key, reaction_quota


class ReactionChoiceTests(unittest.TestCase):
    def setUp(self):
        self.options = [types.ReactionEmoji(emoticon=x) for x in ("👍", "❤️", "🔥", "👏")]

    def message(self, *items):
        counts = [SimpleNamespace(reaction=self.options[i], chosen_order=1 if mine else None)
                  for i, mine in items]
        return SimpleNamespace(reactions=SimpleNamespace(results=counts))

    def test_regular_account_uses_one_reaction(self):
        result = choose_reactions(self.options, self.message(), 8, 1)
        self.assertEqual(len(result), 1)

    def test_premium_keeps_own_and_respects_unique_limit(self):
        message = self.message((0, True), (1, False), (2, False))
        result = choose_reactions(self.options, message, 3, 3)
        self.assertEqual({reaction_key(x) for x in result},
                         {reaction_key(x) for x in self.options[:3]})

    def test_new_reactions_rejected_when_unique_limit_is_full(self):
        with self.assertRaises(ValueError):
            choose_reactions(self.options, self.message((0, False)), 1, 3)

    def test_last_account_uses_only_remaining_reactions(self):
        self.assertEqual(reaction_quota(True, 5, 2), 2)
        self.assertEqual(reaction_quota(True, 5, 1), 1)
        self.assertEqual(reaction_quota(False, 5, 2), 1)

    def test_media_album_counts_as_one_post_with_caption(self):
        messages = [SimpleNamespace(id=72, grouped_id=None, message="new"),
                    SimpleNamespace(id=70, grouped_id=99, message=""),
                    SimpleNamespace(id=69, grouped_id=99, message=""),
                    SimpleNamespace(id=62, grouped_id=99, message="caption"),
                    SimpleNamespace(id=61, grouped_id=None, message="")]
        self.assertEqual([m.id for m in distinct_posts(messages)], [72, 62, 61])


class StoreTests(unittest.TestCase):
    def test_new_store_starts_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.sqlite3")
            self.assertEqual(store.meta("paused", "1"), "1")

    def test_pilot_and_future_posts_only(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.sqlite3")
            store.db.execute("""INSERT INTO accounts(name,title,user_id,membership,updated_at)
                VALUES('a','A',1,'joined',0)""")
            store.db.commit()
            store.set_meta("start_from", "100")
            store.queue(99)
            store.queue(100, manual=True)
            store.queue(101)
            self.assertEqual([x["id"] for x in store.pending_posts()], [100, 101])
            store.delivery(100, "a", "error", error="Telegram rejected reaction")
            self.assertEqual([x["id"] for x in store.pending_posts()], [101])

    def test_new_posts_have_budget_and_rotate_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.sqlite3")
            store.db.executemany("""INSERT INTO accounts(name,title,user_id,membership,updated_at)
                VALUES(?,?,?,'joined',0)""", [(x, x, i) for i, x in enumerate("abc", 1)])
            store.db.commit()
            store.queue(201)
            target = store.post(201)["reaction_target"]
            self.assertIn(target, range(15, 19))
            self.assertEqual([r["name"] for r in store.rotated_accounts()], list("abc"))
            store.advance_account("b")
            self.assertEqual([r["name"] for r in store.rotated_accounts()], list("cab"))
            other = Store(Path(folder) / "other.sqlite3")
            other.db.executemany("""INSERT INTO accounts(name,title,user_id,membership,updated_at)
                VALUES(?,?,?,'joined',0)""", [(x, x, i) for i, x in enumerate("abc", 1)])
            other.db.commit()
            self.assertEqual([r["name"] for r in other.rotated_accounts(store.meta("last_used_account"))], list("cab"))
            store.delivery(201, "a", "done", "👍,🔥,❤️")
            store.delivery(201, "b", "done", "👍")
            self.assertEqual(store.reaction_count(201), 4)
            store.complete_post(201)
            self.assertEqual(store.pending_posts(), [])

    def test_migration_preserves_existing_posts_without_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite3"
            import sqlite3
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, manual INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO posts VALUES(180,1,1)")
            db.commit()
            db.close()
            store = Store(path)
            self.assertIsNone(store.post(180)["reaction_target"])
            store.queue(180, manual=True)
            self.assertIsNone(store.post(180)["reaction_target"])
            store.queue(181)
            self.assertIn(store.post(181)["reaction_target"], range(15, 19))


if __name__ == "__main__":
    unittest.main()
