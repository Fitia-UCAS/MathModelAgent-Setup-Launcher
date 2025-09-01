# app/tests/test_common_utils.py

import unittest

from app.utils.common_utils import split_footnotes


class TestCommonUtils(unittest.TestCase):
    def test_split_footnotes(self):
        text = "Example[^1]\n\n[^1]: Footnote content"
        main, notes = split_footnotes(text)
        self.assertEqual(main, "Example")
        self.assertEqual(notes, [("1", "Footnote content")])


if __name__ == "__main__":
    unittest.main()
