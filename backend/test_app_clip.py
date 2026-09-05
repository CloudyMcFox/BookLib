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

    def test_guest_flags_fail_closed(self):
        os.environ["TEST_GUEST_FLAG"] = "flase"
        self.assertFalse(main.enabled_env("TEST_GUEST_FLAG"))
        os.environ["TEST_GUEST_FLAG"] = ""
        self.assertFalse(main.enabled_env("TEST_GUEST_FLAG"))
        os.environ["TEST_GUEST_FLAG"] = "true"
        self.assertTrue(main.enabled_env("TEST_GUEST_FLAG"))
        del os.environ["TEST_GUEST_FLAG"]

    def test_cover_urls_block_non_public_networks(self):
        blocked = [
            "http://example.com/cover.jpg",
            "https://127.0.0.1/cover.jpg",
            "https://10.0.0.1/cover.jpg",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/cover.jpg",
            "https://user:password@example.com/cover.jpg",
            "https://example.com:8443/cover.jpg",
        ]
        for url in blocked:
            self.assertIsNone(main._public_cover_target(url), url)

        self.assertIsNotNone(
            main._public_cover_target("https://8.8.8.8/cover.jpg")
        )

    def test_cover_hostname_rejects_private_or_mixed_dns_answers(self):
        def private_resolver(*args, **kwargs):
            return [
                (main.socket.AF_INET, main.socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))
            ]

        def mixed_resolver(*args, **kwargs):
            return [
                (main.socket.AF_INET, main.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (main.socket.AF_INET, main.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]

        def public_resolver(*args, **kwargs):
            return [
                (main.socket.AF_INET6, main.socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
                (main.socket.AF_INET, main.socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            ]

        url = "https://covers.example/cover with space.jpg#preview"
        self.assertIsNone(main._public_cover_target(url, private_resolver))
        self.assertIsNone(main._public_cover_target(url, mixed_resolver))
        target = main._public_cover_target(url, public_resolver)
        self.assertIsNotNone(target)
        self.assertEqual(
            target[2],
            ["2606:4700:4700::1111", "1.1.1.1"],
        )

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

    def test_guest_shelf_visibility_is_a_separate_opt_in(self):
        original = main.GUEST_SHELF_ACCESS_ENABLED
        try:
            book = main.Book(
                id=1,
                title="Shelf Test",
                shelf_id=1,
                shelf_column=2,
                shelf_row=3,
            )
            guest = {"username": "guest", "role": main.ROLE_GUEST}

            main.GUEST_SHELF_ACCESS_ENABLED = False
            hidden = main.book_for_user(book, guest)
            self.assertIsNone(hidden.shelf_id)
            with self.assertRaises(main.HTTPException):
                main.require_shelf_reader(guest)

            main.GUEST_SHELF_ACCESS_ENABLED = True
            visible = main.book_for_user(book, guest)
            self.assertEqual(
                (visible.shelf_id, visible.shelf_column, visible.shelf_row),
                (1, 2, 3),
            )
            self.assertEqual(main.require_shelf_reader(guest), guest)
        finally:
            main.GUEST_SHELF_ACCESS_ENABLED = original


if __name__ == "__main__":
    unittest.main()
