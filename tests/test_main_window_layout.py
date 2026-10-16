"""How BLACS's main window divides its space.

The shot controls are a fixed strip above the device tabs, taking the height
they need; the device tabs get the rest. Asserted against the loaded window.
"""
import os
import unittest

from qtutils import UiLoader
from qtutils.qt.QtWidgets import QApplication, QSplitter, QWidget

import blacs


MAIN_UI = os.path.join(os.path.dirname(blacs.__file__), 'main.ui')

_qapplication = None


def load_main_window():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a garbage-collected QApplication
        # takes every widget built under it down.
        _qapplication = QApplication([])
    return UiLoader().load(MAIN_UI)


class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = load_main_window()
        # Shown, because Qt does not lay a window out until it is: unshown,
        # every height below reads 0.
        cls.ui.show()
        QApplication.instance().processEvents()

    def shot_controls(self):
        strip = self.ui.findChild(QWidget, 'verticalLayoutWidget_2')
        self.assertIsNotNone(strip, 'the shot controls strip')
        return strip

    def tabs(self):
        tabs = self.ui.findChild(QSplitter, 'tab_horizontal_splitter')
        self.assertIsNotNone(tabs, 'the device tab area')
        return tabs

    def test_the_device_tabs_get_the_rest_of_the_window(self):
        strip, tabs = self.shot_controls(), self.tabs()
        heights = {}
        for window_height in (700, 1100):
            self.ui.resize(900, window_height)
            QApplication.instance().processEvents()
            heights[window_height] = (strip.height(), tabs.height())

        self.assertEqual(
            heights[700][0],
            heights[1100][0],
            'the strip does not grow with the window',
        )
        self.assertGreater(
            heights[1100][1],
            heights[700][1],
            'the device tabs take the space the window gained',
        )


if __name__ == '__main__':
    unittest.main()
