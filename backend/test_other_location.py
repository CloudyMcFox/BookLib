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

from fastapi import HTTPException

from backend.app import main

USER = {"username": "test", "role": main.ROLE_ADMIN}


class OtherLocationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        main.conn.close()
        os.unlink(_database.name)

    def setUp(self):
        main.conn.execute("DELETE FROM books")
        main.conn.commit()
        self.shelf_id = main.conn.execute("SELECT id FROM shelves LIMIT 1").fetchone()["id"]

    def add(self, title, **fields):
        return main.add_book(main.Book(title=title, format="Other", **fields),
                             main.BackgroundTasks(), current_user=USER)

    def locate(self, book_id, **fields):
        return main.set_book_location(book_id, main.BookLocation(**fields), current_user=USER)

    def titles(self, **filters):
        return [b.title for b in main.list_books(sort="title", current_user=USER, **filters)]

    def test_a_typed_place_keeps_a_book_out_of_the_unplaced_list(self):
        self.add("Unplaced")
        bedroom = self.add("Bedside")
        self.locate(bedroom.id, other_location="  Bedroom  ")

        self.assertEqual(self.titles(placed=False), ["Unplaced"])
        self.assertEqual(self.titles(placed=True), ["Bedside"])
        self.assertEqual(self.titles(q="bedroom"), ["Bedside"])
        self.assertEqual(main.list_other_locations(current_user=USER), ["Bedroom"])

    def test_a_shelf_and_a_typed_place_replace_each_other(self):
        book = self.add("Mover")
        on_shelf = self.locate(book.id, shelf_id=self.shelf_id, shelf_column=1, shelf_row=1)
        self.assertIsNone(on_shelf.other_location)

        elsewhere = self.locate(book.id, other_location="Office")
        self.assertEqual(elsewhere.other_location, "Office")
        self.assertIsNone(elsewhere.shelf_id)

        back = self.locate(book.id, shelf_id=self.shelf_id, shelf_column=2, shelf_row=1)
        self.assertIsNone(back.other_location)
        self.assertEqual(back.shelf_column, 2)

        cleared = self.locate(book.id)
        self.assertIsNone(cleared.shelf_id)
        self.assertIsNone(cleared.other_location)

    def test_both_at_once_is_rejected(self):
        book = self.add("Greedy")
        with self.assertRaises(HTTPException):
            self.locate(book.id, shelf_id=self.shelf_id, shelf_column=1, shelf_row=1,
                        other_location="Bedroom")

    def test_editing_a_book_without_the_field_keeps_its_place(self):
        book = self.locate(self.add("Kept").id, other_location="Bedroom")
        updated = main.update_book(book.id, main.Book(title="Kept, renamed"), current_user=USER)
        self.assertEqual(updated.other_location, "Bedroom")

    def test_location_sort_puts_shelved_then_elsewhere_then_unplaced(self):
        self.add("C unplaced")
        self.locate(self.add("B elsewhere").id, other_location="Bedroom")
        self.locate(self.add("A shelved").id, shelf_id=self.shelf_id, shelf_column=1, shelf_row=1)
        books = main.list_books(sort="location", current_user=USER)
        self.assertEqual([b.title for b in books], ["A shelved", "B elsewhere", "C unplaced"])


if __name__ == "__main__":
    unittest.main()
