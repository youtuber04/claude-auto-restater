import json, os, tempfile, unittest
import statusline as sl


class TestStatusline(unittest.TestCase):
    def test_write_mirror_roundtrips(self):
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "sub", "mirror.json")
        sl.write_mirror('{"a":1}', dest)
        self.assertTrue(os.path.exists(dest))
        with open(dest) as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_format_status_reads_model_and_five_hour(self):
        d = {"model": {"display_name": "Opus"},
             "rate_limits": {"five_hour": {"used_percentage": 42}}}
        self.assertEqual(sl.format_status(d), "Opus · 5h 42%")

    def test_format_status_none_degrades_to_bare_name(self):
        # main passes None when stdin wasn't a mirrorable blob
        self.assertEqual(sl.format_status(None), "Claude")

    def test_mirror_if_valid_writes_object_blob_and_returns_it(self):
        dest = os.path.join(tempfile.mkdtemp(), "mirror.json")
        self.assertEqual(sl.mirror_if_valid('{"a":1}', dest), {"a": 1})
        with open(dest) as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_mirror_if_valid_skips_garbage_and_keeps_old_blob(self):
        # a torn/empty/non-object stdin must never clobber a good mirror the
        # helper is reading mid-run (it parses the file and calls .get on it)
        dest = os.path.join(tempfile.mkdtemp(), "mirror.json")
        sl.mirror_if_valid('{"a":1}', dest)
        self.assertIsNone(sl.mirror_if_valid("", dest))
        self.assertIsNone(sl.mirror_if_valid("not json", dest))
        self.assertIsNone(sl.mirror_if_valid("42", dest))
        with open(dest) as f:
            self.assertEqual(json.load(f), {"a": 1})


if __name__ == "__main__":
    unittest.main()
