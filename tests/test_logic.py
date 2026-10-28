import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon import types

from app import Store, choose_reactions, reaction_key


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


if __name__ == "__main__":
    unittest.main()
