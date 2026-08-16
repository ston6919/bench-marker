#!/usr/bin/env python3
"""Tests for incremental SVG preview extraction."""

from __future__ import annotations

import unittest

import db


class ExtractPreviewSvgTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(db.extract_preview_svg(""), "")
        self.assertEqual(db.extract_preview_svg("   "), "")

    def test_chatter_before_svg(self) -> None:
        out = db.extract_preview_svg("Sure! Here you go:\n<svg viewBox='0 0 10 10'><circle cx='5' cy='5' r='3'/></svg>")
        self.assertTrue(out.startswith("<svg"))
        self.assertIn("<circle", out)
        self.assertTrue(out.endswith("</svg>"))
        self.assertNotIn("Sure", out)

    def test_fenced_svg(self) -> None:
        raw = "```svg\n<svg viewBox='0 0 10 10'><rect width='4' height='4'/></svg>\n```"
        out = db.extract_preview_svg(raw)
        self.assertIn("<rect", out)
        self.assertNotIn("```", out)

    def test_partial_path_is_held_back(self) -> None:
        raw = '<svg viewBox="0 0 10 10"><circle cx="1" cy="1" r="1"/><path d="M10'
        out = db.extract_preview_svg(raw)
        self.assertIn("<circle", out)
        self.assertNotIn("<path", out)
        self.assertTrue(out.endswith("</svg>"))

    def test_missing_close_svg(self) -> None:
        raw = '<svg viewBox="0 0 10 10"><rect width="2" height="2"></rect>'
        out = db.extract_preview_svg(raw)
        self.assertTrue(out.endswith("</svg>"))
        self.assertIn("<rect", out)

    def test_strips_script(self) -> None:
        raw = (
            '<svg viewBox="0 0 10 10">'
            '<script>alert(1)</script>'
            '<circle cx="1" cy="1" r="1"/>'
            "</svg>"
        )
        out = db.extract_preview_svg(raw)
        self.assertNotIn("script", out.lower())
        self.assertNotIn("alert", out)
        self.assertIn("<circle", out)

    def test_strips_event_handler_and_external_href(self) -> None:
        raw = (
            '<svg viewBox="0 0 10 10">'
            '<circle cx="1" cy="1" r="1" onclick="alert(1)" href="https://evil.example"/>'
            '<use href="#ok"/>'
            "</svg>"
        )
        out = db.extract_preview_svg(raw)
        self.assertNotIn("onclick", out.lower())
        self.assertNotIn("https://evil.example", out)
        self.assertIn('href="#ok"', out)

    def test_adds_viewbox_and_xmlns(self) -> None:
        out = db.extract_preview_svg("<svg><rect width='1' height='1'/></svg>")
        self.assertIn("viewBox=", out)
        self.assertIn("xmlns=", out)

    def test_wrap_svg_preview(self) -> None:
        html = db.wrap_svg_preview('<svg viewBox="0 0 1 1"></svg>')
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<svg", html)

    def test_attach_draw_preview_only_for_draw(self) -> None:
        html_run = {"task_kind": "html", "output_text": "<svg></svg>"}
        self.assertIsNone(db.attach_draw_preview(html_run).get("preview_html"))
        draw_run = {
            "task_kind": "draw",
            "output_text": '<svg><circle cx="1" cy="1" r="1"/></svg>',
        }
        attached = db.attach_draw_preview(draw_run)
        self.assertTrue(attached["has_preview"])
        self.assertIn("<circle", attached["preview_html"])


if __name__ == "__main__":
    unittest.main()
