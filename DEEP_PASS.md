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
- **The event loop** runs in `exec_qapplication`, with Ctrl-C closing the
  window, so it goes through BLACS's close sequence.
- **`Tab.statemachine_timeout_remove_all`** stays, though no tab in the suite
  calls it: upstream's device guide documents it.
- **`docs/How to add a new device/`** is upstream's device guide, kept with its
  TODOs. `docs/SPIELMAN_FORK_CHANGES.md` is the guide to merging upstream into
  the fork.

## Upstream

`master` is upstream's. CodeHealth stays as close to upstream as the fork's
design allows, and carries upstream's fixes in upstream's form, ported by hand
and credited in each commit: 9ea53bf, e35dfff, 8bfadff with 0c1bb86, 4a6562a,
9df6180, 1126665, 634f0cc with 9fa23e7, 803cf93, ecb3bef, 609a930, 4692a8b,
d67dbe4 (adapted: the shot is reported `aborted`, which runmanager handles as
any abort), 074dcda, 7a8f018, 5a226bb with 763bff2, e8d59dc, c3e319e (Ctrl-C
closes the window), 177f213, 8ff163e, the loop-wide handler of 80a3f0d,
0636b34, 7719dee with d2e7675, 7918c4d, 23376cd (with every logging call of the
fork's) and 79d460e's classifiers. The floors are Python 3.13, qtutils 4.4.1
and zprocess 3.0.1.

The tab state machine on the main thread (bfaaf25, 4c0f058, f89cc2b), with
workers ended by a quit message and the Stop button (69a2dd3, d00b690), is
upstream's: `tab_base_classes.py` differs from upstream's only by the fork's
changes in `DESIGN_TAB_STATE_MACHINE.md` (F2 to F4, F7, F9 to F15). A worker's
cleanup on a lost parent needs zprocess's `on_parent_death` hook (zprocess
33b3618).

Not ported:

- b52c85a, the shot's tab transitions on the main thread per group, is to be
  ported into `shot_execution.py`'s loop (`DESIGN_TAB_STATE_MACHINE.md`, F6).
- Opening a shot file once (5689970) is deferred (Ian).
- BLACS has no queue, analysis submission or pushed shots, which runmanager
  owns here: 2b6ec46, a10b3f8, 9bc1131, 1612e88, 66a7f4a, 6de2b2b, 905a1fe,
  9c6cf2b, and the queue half of 56a54c2.
- The window is restored through `LabscriptGuiApplication`'s shared geometry
  (Ian), not Qt's saved geometry: 6165951, 850e3cb.
- ce9a10a's `open_file` is reached through `open_in_editor`, shared by the
  applications (Ian).

## Compatibility

The campaign and the upstream stage keep no alias, old import path, shim,
legacy keyword or deprecation path. `blacs.plugins` re-exports
`labscript_utils.plugins`' `callback`, `Callback` and `DEFAULT_PRIORITY`, which
upstream defines there, and `BasePlugin` beside them. One fallback for an old
saved file stays (Ian): the connection table plugin takes a `.h5` globals file,
in its saved list or its file dialog, as the TOML file runmanager made from it
(`normalize_globals_path`), so a settings file saved before the TOML globals
keeps its globals list. It is offered upstream with the TOML globals.

## Left as it is

- Guards at a real boundary: `runmanager_rpc`'s catch and log-once,
  `manage()`'s catch, and the held outcome's delivery on the way out, all stated
  in `shot-management.rst`. `reset_failed_shot_file`'s swallowed errors are
  upstream's.
- Style: `os.path`, and Qt enum spelling (with the redundant PySide branches in
  the connection table's sort-order helpers).
- Tests are more than rule 13's tenth of the code. The tenth is not reached
  without cutting the integration fixture, which is what makes the rest real.
