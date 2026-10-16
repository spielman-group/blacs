"""Theme change events reaching BLACS's main window.

``BLACSWindow`` recolours its tab headers when the OS switches between light
and dark, which reaches it in ``changeEvent``, a Qt virtual method. An
exception raised there or in what it triggers goes to ``sys.excepthook``
instead of the caller, so a test has to deliver the event through Qt and watch
the hook to tell a working handler from a broken one.
"""
import sys
import unittest

from qtutils.qt.QtCore import QEvent
from qtutils.qt.QtWidgets import QApplication

# fixtures imports BLACS, with its splash stubbed, for every test module.
import fixtures
import blacs.__main__


def a_qapplication():
    """One QApplication for the whole process."""
    return QApplication.instance() or QApplication(['test'])


class FakeBLACS:
    """Records what a theme change does to BLACS."""

    def __init__(self):
        self.tab_icon_and_text_calls = []

    def update_all_tab_icon_and_text(self, color):
        self.tab_icon_and_text_calls.append(color)


class ThemeEventTests(unittest.TestCase):
    def setUp(self):
        self.qapplication = a_qapplication()
        self.window = blacs.__main__.BLACSWindow()
        self.window.blacs = FakeBLACS()
        self.addCleanup(self.window.deleteLater)

    def send(self, event_type):
        """Deliver a change event the way Qt does, and return what escaped.

        ``sendEvent`` diverts an exception raised in a virtual method to
        ``sys.excepthook``, so the hook is watched here.
        """
        seen = []
        original = sys.excepthook
        sys.excepthook = lambda exc_type, exc, tb: seen.append(exc)
        try:
            self.qapplication.sendEvent(self.window, QEvent(event_type))
        finally:
            sys.excepthook = original
        return seen

    def test_a_palette_change_is_handled(self):
        escaped = self.send(QEvent.Type.PaletteChange)
        self.assertEqual([], escaped)
        self.assertEqual(1, len(self.window.blacs.tab_icon_and_text_calls))


if __name__ == '__main__':
    unittest.main()
