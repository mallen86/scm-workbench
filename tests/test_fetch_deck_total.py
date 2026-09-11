"""The second fetch stage's denominator, taken from the job's own decklist.

A prefetching fetch plugin (MTG over an MPCFill XML) runs two stages: it
prefetches unique images, then walks the decklist slot by slot. A card played
several times is one image and several slots, so the prefetch count can be well
short of the slots that follow. MPCFill XML states the slot count in
<details><quantity>N</quantity>, which is exactly what the plugin sizes its slot
list from, and that is the only thing read from the file.
"""

import os
import tempfile
import unittest
from pathlib import Path

from scm_workbench import server


def decklist_xml(quantity, pad=""):
    return (f'<?xml version="1.0"?><order><details><quantity>{quantity}'
            f'</quantity></details><fronts>{pad}</fronts></order>')


class DecklistTotalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deck-total-")
        self.cwd = Path(self.temp.name)
        (self.cwd / "game" / "decklist").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_decklist(self, name, text):
        (self.cwd / "game" / "decklist" / name).write_text(text, encoding="utf-8")

    def test_a_pasted_decklist_reports_its_declared_slot_count(self):
        args = {"deck_source": "paste", "deck_text": decklist_xml(100)}
        self.assertEqual(server._fetch_decklist_total(args, self.cwd), 100)

    def test_a_decklist_file_reports_its_declared_slot_count(self):
        # The real case: a 100 slot decklist whose images number 88.
        self.write_decklist("defender.xml", decklist_xml(100))
        args = {"deck_source": "file", "deck_file": "defender.xml"}
        self.assertEqual(server._fetch_decklist_total(args, self.cwd), 100)

    def test_a_decklist_without_a_quantity_reports_nothing(self):
        # Every other format, and XML without the field, must leave the bar on
        # its prefetch count rather than produce a made up denominator.
        self.write_decklist("plain.txt", "4 Lightning Bolt\n2 Counterspell\n")
        args = {"deck_source": "file", "deck_file": "plain.txt"}
        self.assertEqual(server._fetch_decklist_total(args, self.cwd), 0)
        self.assertEqual(server._fetch_decklist_total({"deck_source": "paste", "deck_text": "1 Card"}, self.cwd), 0)

    def test_unknown_or_unsafe_sources_report_nothing(self):
        for args in (
            {"deck_source": "url", "deck_url": "https://example.invalid/d.xml"},
            {"deck_source": "file", "deck_file": "missing.xml"},
            {"deck_source": "file", "deck_file": ""},
            {"deck_source": "file", "deck_file": "../secrets.xml"},
            {"deck_source": "file", "deck_file": "sub/dir.xml"},
            {"deck_source": "file", "deck_file": "..\\win.xml"},
            {},                      # no source at all
        ):
            self.assertEqual(server._fetch_decklist_total(args, self.cwd), 0, args)
        # A directory named like a decklist is not a decklist.
        (self.cwd / "game" / "decklist" / "adir.xml").mkdir()
        self.assertEqual(server._fetch_decklist_total(
            {"deck_source": "file", "deck_file": "adir.xml"}, self.cwd), 0)

    def test_a_symlinked_decklist_reports_nothing(self):
        # A link could point anywhere; the count is not worth following one for.
        outside = Path(self.temp.name) / "outside.xml"
        outside.write_text(decklist_xml(5), encoding="utf-8")
        link = self.cwd / "game" / "decklist" / "link.xml"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable here")
        self.assertEqual(server._fetch_decklist_total(
            {"deck_source": "file", "deck_file": "link.xml"}, self.cwd), 0)

    def test_the_read_is_bounded_and_only_sees_the_first_slice(self):
        # A quantity beyond the scan window is not found, which is correct: a
        # declaration that far into a file is not the one being honoured here.
        huge = "x" * (server.DECKLIST_SCAN_BYTES + 16) + decklist_xml(7)
        self.write_decklist("huge.xml", huge)
        args = {"deck_source": "file", "deck_file": "huge.xml"}
        self.assertEqual(server._fetch_decklist_total(args, self.cwd), 0)
        # Within the window it is found, however much padding precedes it.
        self.write_decklist("padded.xml", "y" * 4096 + decklist_xml(42))
        self.assertEqual(server._fetch_decklist_total(
            {"deck_source": "file", "deck_file": "padded.xml"}, self.cwd), 42)

    def test_only_the_quantity_element_is_matched_and_bounded(self):
        # The scan reads one scalar; anything else in the file is irrelevant.
        for text, expected in (
            ("<quantity>100</quantity>", 100),
            ('<quantity>\n  200  \n</quantity>', 200),
            ("<QUANTITY>50</QUANTITY>", 50),
            ("<quantity>0</quantity>", 0),
            ("<quantity></quantity>", 0),
            ("<quantity>abc</quantity>", 0),
            ("<quantity>999999999999</quantity>", 0),   # longer than the bound
            ("<cardquantity>9</cardquantity>", 0),      # not the element
        ):
            self.assertEqual(server._decklist_quantity(text), expected, text)


if __name__ == "__main__":
    unittest.main()
