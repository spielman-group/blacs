"""Theme change events reaching BLACS's main window.

``BLACSWindow`` recolours its tab headers whenever Qt tells the window
something about its own appearance has changed, which is how BLACS repaints
itself when the OS switches between light and dark. The window gets that news
in ``changeEvent``, a Qt virtual method, so an exception raised in it or in
what it triggers does not propagate to the caller: PyQt hands it to
``sys.excepthook`` instead and carries on. A test that only calls the slot
directly, or only checks that nothing raised, would pass either way -- it has
to deliver the event through Qt and watch the hook to tell a working handler
from a broken one.
"""
import sys
import unittest

from qtutils.qt.QtCore import QEvent
from qtutils.qt.QtWidgets import QApplication

# fixtures does the guarded import of BLACS, once, for every test module.
import fixtures
import blacs.__main__


def a_qapplication():
    """One QApplication for the whole process, as Qt requires."""
    return QApplication.instance() or QApplication(['test'])


class FakeBLACS(object):
    """Stands in for the real BLACS app, recording what a theme change does with it."""

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
        """Deliver a change event the way Qt does, and report what escaped.

        ``QApplication.sendEvent`` is what actually calls a virtual method
        like ``changeEvent``, and it is also what diverts an exception raised
        inside one to ``sys.excepthook`` rather than letting it raise here --
        so the hook has to be watched, or a broken handler would look clean.
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

    def test_a_style_change_is_handled(self):
        escaped = self.send(QEvent.Type.StyleChange)
        self.assertEqual([], escaped)
        self.assertEqual(1, len(self.window.blacs.tab_icon_and_text_calls))

    def test_application_palette_change_never_reaches_the_window(self):
        """ApplicationPaletteChange is delivered to the QApplication, not to
        any widget. QWidget.event() never routes it to changeEvent, so
        sending it straight to the window documents why naming it in
        changeEvent could never have matched anything real.
        """
        escaped = self.send(QEvent.Type.ApplicationPaletteChange)
        self.assertEqual([], escaped)
        self.assertEqual([], self.window.blacs.tab_icon_and_text_calls)


if __name__ == '__main__':
    unittest.main()
