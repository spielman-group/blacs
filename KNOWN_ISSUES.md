# Known issues

- `shot_execution.py`, `_manage` run polling: device-error checks occur only after a successful notification read, so a fatal master with no completion notification can leave a shot waiting indefinitely (source review; not executed).
- `shot_execution.py`, `_manage`, and `plugins/cycle_time`: Abort during the post-programming delay returns from the callback but still reaches master.start_run before consuming the abort (source review; not executed).
- `__main__.py`, closeEvent, and delete_repeated_shots/progress_bar plugin close methods: GUI-thread shutdown performs unbounded joins before hardware cleanup and can block on a plugin waiting for an HDF5 lock (source review).
- `front_panel_settings.py`, `check_row`: raw byte-string channel names are compared with the text sentinel '-', misclassifying unnamed saved channels during restoration (source review).
- `__main__.py`, `on_save_exit`: a settings-save exception after exit_started is set prevents worker cleanup and leaves subsequent close attempts latched out (source review; not executed).
- Shared `Worker.mainloop` catches Exception and KeyboardInterrupt but not SystemExit, so a job raising SystemExit can exit without running shutdown (inherited lifecycle limitation; source-confirmed).
- Shared `WorkersMixin._end_workers_locked` sends kill after the quit deadline but does not wait for confirmed process exit before returning, including before a restart (inherited lifecycle limitation; source-confirmed).
- Shared `StateMachine._do_job` sends have no deadline; remote zprocess WriteQueue instances have no child-death check, so a send can remain blocked before either reply wait begins (inherited transport limitation; source-confirmed).
