import os
import tempfile
import unittest

os.environ.setdefault(
    "SECRET_KEY",
    "test-only-secret-key-with-more-than-32-characters",
)
_database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_database.close()
os.environ["BOOKLIB_DB"] = _database.name
os.environ["GUEST_ACCESS_ENABLED"] = "true"

from backend.app import main


class AppClipCheckoutTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        main.conn.close()
        os.unlink(_database.name)

    def setUp(self):
        main.conn.execute("DELETE FROM books")
        main.conn.commit()

    def test_isbn_10_and_13_are_equivalent(self):
        self.assertEqual(
            main.isbn_equivalents("0-306-40615-2"),
            {"0306406152", "9780306406157"},
        )
        self.assertEqual(
            main.isbn_equivalents("9780306406157"),
            {"0306406152", "9780306406157"},
        )
        self.assertEqual(main.isbn_equivalents("0306406153"), set())
        self.assertEqual(main.isbn_equivalents("9780306406158"), set())

    def test_lookup_returns_one_available_copy(self):
        main.conn.executemany(
            "INSERT INTO books (title, author, isbn, checked_out_at) VALUES (?, ?, ?, ?)",
            [
                ("Test Book", "Test Author", "0-306-40615-2", None),
                (
                    "Test Book",
                    "Test Author",
                    "9780306406157",
                    "2026-09-01T00:00:00Z",
                ),
            ],
        )
        main.conn.commit()

        result = main.guest_checkout_lookup(
            "9780306406157",
            current_user={"username": "guest", "role": main.ROLE_GUEST},
        )

        self.assertEqual(result.title, "Test Book")
        self.assertEqual(result.available_count, 1)
        self.assertEqual(result.total_count, 2)
        self.assertIsNotNone(result.book_id)


if __name__ == "__main__":
    unittest.main()
