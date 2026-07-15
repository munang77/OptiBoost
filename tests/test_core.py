# -*- coding: utf-8 -*-
"""OptiBoost 핵심 로직(순수 함수) 자동 테스트."""
import os
import sys
import tempfile
import importlib.util
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "PCOptimizer.pyw")
_spec = importlib.util.spec_from_file_location("optiboost", _SRC)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(m._parse_ver("v1.2"), (1, 2))
        self.assertEqual(m._parse_ver("1.2.3"), (1, 2, 3))

    def test_compare(self):
        self.assertTrue(m._parse_ver("1.3") > m._parse_ver("1.2"))
        self.assertTrue(m._parse_ver("v2.0") > m._parse_ver("v1.9"))
        self.assertFalse(m._parse_ver("1.0") > m._parse_ver("1.0"))


class HumanSizeTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(m.human(0), "0 B")
        self.assertEqual(m.human(512), "512 B")

    def test_scales(self):
        self.assertIn("KB", m.human(2048))
        self.assertIn("MB", m.human(5 * 1024 * 1024))
        self.assertIn("GB", m.human(3 * 1024 ** 3))


class FileScanTests(unittest.TestCase):
    def test_reparse_normal_dir_false(self):
        d = tempfile.mkdtemp()
        self.assertFalse(m.is_reparse_point(d))

    def test_scan_size(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.bin"), "wb") as f:
            f.write(b"x" * 1000)
        total, count = m.scan_size([d])
        self.assertEqual(total, 1000)
        self.assertEqual(count, 1)


class CategoryTests(unittest.TestCase):
    def test_has_recyclebin(self):
        cats = m.build_categories()
        self.assertTrue(len(cats) >= 5)
        self.assertTrue(any(c["key"] == "recyclebin" for c in cats))


class HealthTests(unittest.TestCase):
    def test_grades(self):
        self.assertEqual(m.health_grade(95)[0], "S")
        self.assertEqual(m.health_grade(82)[0], "A")
        self.assertEqual(m.health_grade(40)[0], "D")


if __name__ == "__main__":
    unittest.main()
