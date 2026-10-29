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

## Left as it is

- Guards at a real boundary: `runmanager_rpc`'s catch and log-once,
  `manage()`'s catch, and the held outcome's delivery on the way out, all stated
  in `shot-management.rst`. `reset_failed_shot_file`'s swallowed errors are
  upstream's.
- Style: `os.path`, Qt enum spelling (with the redundant PySide branches in the
  connection table's sort-order helpers), and two `%` halves in
  `Tab.mainloop`, whose escaped HTML cannot go in an f-string under Python 3.11.
- Tests are more than rule 13's tenth of the code. The tenth is not reached
  without cutting the integration fixture, which is what makes the rest real.
