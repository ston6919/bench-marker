#!/usr/bin/env python3
"""Tests for incremental pen-command parsing."""

from __future__ import annotations

import unittest

import db


class ExtractPenCommandsTests(unittest.TestCase):
    def test_basic_stroke(self) -> None:
        text = "DOWN 10 10\nTO 20 20\nTO 30 25\nUP\n"
        cmds = db.extract_pen_commands(text, partial=False)
        self.assertEqual(
            cmds,
            [
                {"op": "down", "x": 10, "y": 10},
                {"op": "to", "x": 20, "y": 20},
                {"op": "to", "x": 30, "y": 25},
                {"op": "up"},
            ],
        )

    def test_holds_back_incomplete_line(self) -> None:
        text = "DOWN 10 10\nTO 20"
        cmds = db.extract_pen_commands(text, partial=True)
        self.assertEqual(cmds, [{"op": "down", "x": 10, "y": 10}])

    def test_xy_pen_flag(self) -> None:
        text = "10 10 1\n20 20 1\n20 20 0\n"
        cmds = db.extract_pen_commands(text, partial=False)
        self.assertEqual(cmds[0]["op"], "down")
        self.assertEqual(cmds[1]["op"], "to")
        self.assertEqual(cmds[2]["op"], "up")

    def test_ignores_chatter_and_fences(self) -> None:
        text = "```\nSure!\nDOWN 5 5\nTO 8 8\n```\n"
        cmds = db.extract_pen_commands(text, partial=False)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0]["x"], 5)

    def test_clamps_coordinates(self) -> None:
        cmds = db.extract_pen_commands("DOWN -10 2000\n", partial=False)
        self.assertEqual(cmds[0]["x"], 0)
        self.assertEqual(cmds[0]["y"], 1024)

    def test_commands_hidden_in_thoughts(self) -> None:
        thoughts = (
            "I'll sketch a mug.\n"
            "DOWN 100 200\n"
            "TO 140 210\n"
            "TO 180 200\n"
            "UP\n"
        )
        cmds = db.extract_pen_commands_from_sources("", thoughts, partial=False)
        self.assertGreaterEqual(len(cmds), 4)
        self.assertEqual(cmds[0]["op"], "down")

    def test_attach_pen_preview(self) -> None:
        run = {
            "task_kind": "pen",
            "status": "completed",
            "output_text": "DOWN 1 1\nTO 2 2\n",
        }
        attached = db.attach_draw_preview(run)
        self.assertTrue(attached["has_preview"])
        self.assertEqual(len(attached["pen_commands"]), 2)


if __name__ == "__main__":
    unittest.main()
