# blacs deep pass

The code-health campaign's record for blacs (`issues/code_health_campaign.md`
in the meta repo): the decisions that hold, and what was left as it is on
purpose. It is deleted when the pass is done.

## Decisions that hold

- **Plugins** use the contexts of `labscript_utils.plugins`, as hyde's do.
  Every plugin inherits `BasePlugin`, events are fired with
  `PluginManager.fire_event`, and a plugin saves and restores its state through
  `get_save_data()` and `restore_save_data(data)`, which BLACS keeps in its HDF5
  settings file.
- **The plugin tab hooks** `get_tab_classes()` and `tabs_created()`, and the
  `PluginTab` they use, stay (Ian), though no plugin in the suite uses them.
- **BLACS's saved state** stays in its HDF5 settings file, the shot file's
  `/front_panel` group stays, and Settings keeps its HDF5 storage (Ian).
- **`exec_qapplication`** is not used: its Ctrl-C ends the event loop without
  BLACS's close sequence, which saves the front panel and shuts down the
  workers.
- **`Tab.statemachine_timeout_remove_all`** stays, though no tab in the suite
  calls it: upstream's device guide documents it.
- **`docs/How to add a new device/`** is upstream's device guide, kept with its
  TODOs. `docs/SPIELMAN_FORK_CHANGES.md` is the guide to merging upstream into
  the fork.

## Upstream

CodeHealth carries, ported by hand and credited in each commit, upstream's
fixes for bugs it shared: 9ea53bf, e35dfff, 8bfadff with 0c1bb86, 4a6562a,
9df6180, 1126665, the open parts of 634f0cc with 9fa23e7, 803cf93, ecb3bef,
609a930 and 4692a8b; closing BLACS aborting the shot in hand (d67dbe4, adapted:
the shot is reported `aborted`, which runmanager handles as any abort); the
exit before any tab when another BLACS holds the port (074dcda); and the 2x
icons (7a8f018). The floors are Python 3.13, qtutils 4.4.1 and zprocess 3.0.1.

Not taken:

- `setup_application()` (0636b34) waits until labscript-utils provides it.
- The palette-derived digital output colours (e8d59dc) wait for a decision.
- The tab state machine on the main thread (4c0f058, bfaaf25, f89cc2b), ending
  workers with a quit message and the Stop button (69a2dd3, d00b690), the
  progress bar on a main-thread timer (5a226bb, 763bff2), the exit teardown
  (c3e319e), the run time's UTC offset (7918c4d, with lyse and runmanager) and
  `excepthook.install()` (d2e7675, 7719dee) are design or dependency work for
  Ian.
- Upstream's queue, analysis submission and pushed-shot changes, which
  runmanager owns here, and 177f213 and 8ff163e, which contradict
  `shot-management.rst` and the shot file's `/front_panel` format.

## Left as it is

- Guards at a real boundary: `runmanager_rpc`'s catch and log-once,
  `manage()`'s catch, and the held outcome's delivery on the way out, all stated
  in `shot-management.rst`. `reset_failed_shot_file`'s swallowed errors are
  upstream's.
- Style: `os.path`, and Qt enum spelling (with the redundant PySide branches in
  the connection table's sort-order helpers).
- Tests are more than rule 13's tenth of the code. The tenth is not reached
  without cutting the integration fixture, which is what makes the rest real.
