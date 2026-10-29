"""Stand-ins shared by BLACS's tests.

``FakeUi`` writes down main.ui's widget names and ``make_executor`` builds a
``ShotExecutor`` with every field set, each once, so a renamed widget or a new
field cannot leave a second copy describing something that no longer exists.
"""
import logging
import queue
import sys
import threading
import types


class _SilentSplash:
    """A splash screen that is never built or shown."""

    def __init__(self, *args, **kwargs):
        pass

    def show(self):
        pass

    def hide(self):
        pass

    def update_text(self, *args, **kwargs):
        pass


def _import_blacs_without_starting_it():
    """Import ``blacs.__main__`` and ``runmanager.__main__`` without their splash."""
    # Both build a Splash, and so the QApplication, at module scope and hide it
    # only under __main__, which leaves the banner on screen. The splash module
    # is stood in for over the imports and put back afterwards.
    fake_splash = types.ModuleType('labscript_utils.splash')
    fake_splash.Splash = _SilentSplash
    fake_splash.get_qapplication = lambda *args, **kwargs: None

    saved = sys.modules.get('labscript_utils.splash')
    sys.modules['labscript_utils.splash'] = fake_splash
    try:
        import blacs.__main__
        import runmanager.__main__
        return blacs.__main__
    finally:
        if saved is None:
            del sys.modules['labscript_utils.splash']
        else:
            sys.modules['labscript_utils.splash'] = saved


blacs_main = _import_blacs_without_starting_it()
BlacsServer = blacs_main.BlacsServer

from blacs.shot_execution import PublishedStatus, ShotExecutor
from labscript_utils.qtwidgets.link_indicator import LinkIndicator
from qtutils.qt.QtWidgets import QApplication

# The runmanager light is real and needs an application, held for the life of
# the process.
_qapplication = QApplication.instance() or QApplication([])


class FakeTextWidget:
    def __init__(self):
        self._text = ''

    def text(self):
        return self._text

    def setText(self, value):
        self._text = str(value)


class FakeButton:
    def __init__(self):
        self._checked = False
        self.clicked = types.SimpleNamespace(
            connect=lambda callback: None, disconnect=lambda callback: None
        )

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = bool(value)

    def setEnabled(self, value):
        pass


class FakeUi:
    """The widget names main.ui carries, and nothing behind them."""

    def __init__(self):
        self.local_override_lineEdit = FakeTextWidget()
        self.shot_request_button = FakeButton()
        self.shot_abort_button = FakeButton()
        self.shot_status = FakeTextWidget()
        self.running_shot_name = FakeTextWidget()


class FakeConfig:
    def getfloat(self, section, option, fallback=None):
        return fallback


class FakeBLACS:
    """A BLACS with nothing in it, for tests that never reach a device."""

    exp_config = FakeConfig()


def make_executor(ui=None, blacs=None, logger_name='test.shot_executor'):
    """A ShotExecutor with every field set and nothing behind it.

    Built with ``__new__`` because ``__init__`` reaches for the application. A
    field added to ShotExecutor is added here once, so a test that misses one
    fails loudly instead of through a snapshot that quietly raises.
    """
    executor = ShotExecutor.__new__(ShotExecutor)
    executor._ui = FakeUi() if ui is None else ui
    executor.BLACS = FakeBLACS() if blacs is None else blacs
    executor._logger = logging.getLogger(logger_name)
    executor._manager_running = False
    executor._requesting_shots = False
    executor._pending_outcome = None
    executor._current_shot_id = None
    executor._next_rep_index = {}
    executor._runmanager_request_client = None
    executor.notify_queue = queue.Queue()
    executor._runmanager_request_error_logged = False
    executor.local_error = None
    executor.published_status = PublishedStatus('', None, None)
    executor.last_opened_shots_folder = ''
    executor.master_pseudoclock = None
    # __init__ starts the shot loop's thread; a test that wants one running
    # puts a started thread here.
    executor.manager = threading.Thread(target=lambda: None)
    # Built but not started: no test here probes a runmanager for the light.
    executor._runmanager_link = LinkIndicator('runmanager', 'localhost', 0)
    return executor
