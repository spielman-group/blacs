# blacs deep pass

A planning document for the code-health campaign (`issues/code_health_campaign.md`
in the meta repo). It lists candidates for Ian's review and is deleted when the
pass is done. Nothing is changed until Ian has reviewed each entry.

## Plugin evaluation

`labscript_utils.plugins` (814 lines on labscript-utils' `CodeHealth`) already
runs blacs and hyde. blacs builds its `PluginManager` in `blacs/plugins/__init__.py`
and drives it through `setup_plugins()`, `MenuBuilder`, `setup_complete()`,
`get_event_handlers()` and `close_plugins()`. hyde subclasses `PluginManager`
and `MenuContext` and drives the contribution path: `collect_services()`,
`register_context()`, `setup_contexts()`, `setup_complete()` and
`get_event_handlers()`.

**Verdict:** it can serve blacs and hyde, and lyse and runmanager can adopt it,
but today it is two languages under one name. blacs's path (`setup_plugins()`,
`MenuBuilder`, `get_menu_class()`, settings pages, notifications) is BLACS
code living in the shared module, and a blacs plugin gains nothing by
inheriting `BasePlugin`, because the manager and blacs call seven hooks that
`BasePlugin` lacks. Moving blacs onto the contribution path makes it one language, and
three small additions (entry-point discovery, firing an event, collecting save
data) make it easy for lyse and runmanager. The changes are additive for hyde.

### Gaps and proposed changes

Sizes are estimates, in lines added and removed. "lu" is labscript-utils, whose
owner makes its changes.

| # | Gap | Proposed change | Size |
| --- | --- | --- | --- |
| P1 | Two lifecycles. `setup_plugins()` knows BLACS's settings pages, notification manager and menu classes; hyde uses contexts. A lyse or runmanager plugin author would face both. | blacs registers `menus` (a `MenuContext` on the menu bar), `settings` and `notifications` contexts, and its plugins return contributions. `setup_plugins()`, `MenuBuilder`, `get_menu_class()`, `set_menu_instance()` and `set_notification_instances()` go; a plugin reaches its notification instance through the setup data. `MenuContext` gains the `icon` key that `MenuBuilder` has. | lu −90 +5; blacs about −30 +40 |
| P2 | blacs's seven plugins do not inherit `BasePlugin`, and carry about 80 lines of hooks that return `None`, `[]` or `{}`. | With P1, `BasePlugin` gains no-op `get_save_data()` and `close()`; every blacs plugin inherits it and drops its no-op hooks. | lu +10; blacs −80 |
| P3 | Discovery reads only the application's own `plugins/` folder, so a plugin shipped by another package, such as labscript-agents giving lyse a plugin, cannot be found. | `discover_modules()` also reads `importlib.metadata` entry points in a group named per application (for example `lyse.plugins`), under the same enable flags in the labconfig. | lu +15 |
| P4 | Every event is fired by the same loop: get the handlers, call each, log one that raises. blacs has it four times in `shot_execution.py`, hyde once. | `PluginManager.fire_event(name, *args)` runs that loop and returns the handlers' results. Applications still choose the events, their names and when they fire. The earlier PRD ruled out an `emit()`; this asks Ian to reverse that. | lu +10; blacs −12 |
| P5 | Saved plugin state has no manager method: `front_panel_settings.py` loops over the plugins itself, and lyse and runmanager would repeat it. | `PluginManager.get_save_data()` returns `{name: data}`, the partner of `restore_save_data(saved)`. Each application stores it where it keeps its state. | lu +8; blacs −6 |
| P6 | `discover_modules()` requires a config object; hyde passes a `NullConfig` and replaces the method wholesale. | `config=None` loads every plugin found. hyde's override can then go, at hyde's choosing. | lu +3 |
| P7 | BLACS's `get_tab_classes()` and `tabs_created()` hooks: upstream's, no plugin in any suite repo uses them, and they already warn as deprecated. | Ian decides: keep, or delete the 38 lines in `blacs/__main__.py`. A lab's own plugin outside the repos would lose its tabs. | blacs −38 |

Coupling: P1, P2 and P4 change hook names and calls on both sides, so the
labscript-utils and blacs changes land as one combined change.

With P1 to P5, lyse taking plugins is a `PluginManager` in a `lyse/plugins`
package plus one `fire_event('analysis_complete', ...)` where analysis ends; the
labscript-agents plugin then lives in labscript-agents, registered as an entry
point.

### Ian's decisions

- **P1** yes: blacs uses the contexts pattern from `labscript_utils.plugins`, as
  hyde does; nothing is imported from hyde.
- **P2**, **P3**, **P6** yes.
- **P4** yes. With P6, hyde's session is briefed to move onto `fire_event` and
  drop `NullConfig` on hyde's `CodeHealth`.
- **P5** studied, then decided, below: one mechanism in labscript-utils, used by
  every plugin.
- **P7** kept.
- P1 to P4 land as one change with labscript-utils' owner, after Ian's decision
  on P5.

### P5: plugin state

What exists:

| App | What a plugin saves | Where | Restored |
| --- | --- | --- | --- |
| blacs | `get_save_data()`, a dict per plugin | `repr()` in an attribute of the front-panel HDF5 file | `eval()` at start-up, passed to `Plugin(initial_settings)` |
| blacs | a settings page's `save()`, through `labscript_utils.settings.Settings` | `repr()` per page in the same file's `preferences` group, written at once when the dialog closes | `eval()` when the page is added |
| hyde | `get_session_toml_data()`, merged into the session at the top level, not per plugin | the project's `session.toml` | on the `project_loaded` event, each plugin reading its own keys back |
| lyse, runmanager, runviewer | no plugins | the application's TOML configuration, through `LabscriptGuiApplication` | `load_configuration()` |

hyde's `get_session_restore_source()` is Python replayed in its kernel, not GUI
state; it stays hyde's. `labscript_utils.settings` has no user but blacs.

Decided by Ian: the plugin says what, and the application says where.

- Every plugin, blacs's and hyde's, saves and restores through
  `BasePlugin.get_save_data()` and `BasePlugin.restore_save_data(data)`, and
  nothing else. No plugin outside the suite uses the old hooks, so there is no
  compatibility path: `Plugin(initial_settings)` and its forwarding go.
- `PluginManager.get_save_data()` returns `{plugin name: data}`, logging a plugin
  that raises. `PluginManager.restore_save_data(saved)` hands each plugin its own
  part, at start-up and for a configuration or project loaded while the
  application runs.
- blacs keeps writing its plugins' data into its HDF5 settings file, as now
  (Ian): moving BLACS's saved state is a bigger change than this pass. Settings
  pages keep their HDF5 storage and save when their dialog closes.
- hyde's plugins override the two `BasePlugin` methods, calling `super()` first
  for the base GUI state, and hyde decides where its session stores them. hyde's
  session is briefed when this lands.

| Who | Change | Size |
| --- | --- | --- |
| labscript-utils | the two `BasePlugin` hooks, the two manager methods, `Plugin()` built with no settings | +25 |
| blacs | every plugin implements the two hooks; `front_panel_settings.py` stops looping over plugins | about −20 |
| hyde | its plugins override the two hooks; `get_session_toml_data` goes | about ±40, hyde's session |

### The plugin change: API

labscript-utils, `labscript_utils/plugins.py`, by its owner:

- `BasePlugin()` takes no arguments. Its no-op hooks: `get_event_handlers()`
  returns `{}`, `get_ui_contributions()` and `get_menu_contributions()` `[]`,
  `get_services()` `{}`, `get_save_data()` `{}`; `restore_save_data(data)` and
  `close()` do nothing. `plugin_setup_complete(data)` stays optional.
- `PluginManager(plugin_package, plugins_dir, config=None, config_section=None,
  default_plugins=(), logger=None, entry_point_group=None)`.
  - `discover_modules()` finds the folders in `plugins_dir`, then the
    `importlib.metadata` entry points in `entry_point_group`, each naming a
    module, the entry point's name being the plugin's. With `config=None` every
    plugin found loads; otherwise both kinds obey the enable flags as now.
  - `instantiate_plugins()` builds `module.Plugin()`, with no settings.
  - `fire_event(name, *args, **kwargs)` calls the handlers
    `get_event_handlers(name)` returns, in priority order, logs one that raises
    and goes on, and returns their results in a list.
  - `get_save_data()` returns `{plugin name: data}`, leaving out a plugin that
    returns nothing or raises (logged). `restore_save_data(saved)` gives each
    plugin `saved.get(name, {})`, logging one that raises.
  - `setup_plugins()` and `MenuBuilder` go. `get_event_handlers`,
    `register_context`, `setup_contexts`, `collect_services`, `setup_complete`
    and `close_plugins` stay.
- `MenuContext(logger=None, icon_factory=None)`: a contribution's `icon` is set
  on its action as `icon_factory(icon)`.

blacs, by its owner: the plugins inherit `BasePlugin`, contribute menus,
settings pages and notifications to contexts that `__main__.py` registers
(`menus`, a `MenuContext` on the menu bar; `settings`; `notifications`), fire
events with `fire_event`, and save through the manager.

hyde, by its session, in the same round, since `Plugin()` loses its argument:
`HydePlugin` and its plugins take no constructor settings, fire events with
`fire_event`, pass `config=None` in place of `NullConfig`, and override
`get_save_data` and `restore_save_data`, calling `super()` first.

## Step 1: contract

What each existing test protects. Marks: **keep**; **fix** (the behaviour is
real, but the test needs changing: it uses a stand-in for runmanager where the
integration fixture's real runmanager can run, or another test is folded into
it); **cut**, with the reason.

Rule 13's tenth: tests are 2,557 lines against 7,414 lines of code (34%). With
these marks they come to about 1,250 lines (17%). The tenth is not reached
without cutting the integration fixture, which is what makes the rest real.

### Totals

- Test files: 7, plus `fixtures.py` and `conftest.py`. Tests: 68, with 20
  subtests.
- Marks, per test: keep 12, fix 8, cut 48. Where two tests become one, the second counts as cut. Approved by Ian and applied: 20 tests, 1,375 lines (19% of the code). Each rewritten test was seen to fail under a mutation of what it protects.

### tests/test_integration.py

A real runmanager and a real BLACS shot loop, talking through their real servers.
- `test_a_queued_shot_runs_is_removed_and_the_next_one_is_offered`: queued shots
  run in order, leave the queue, reach lyse, and are shown running. **keep**.
- `test_the_completed_shot_is_retired_before_the_next_one_is_chosen`: no shot is
  handed out twice. **cut**: the test above asserts the same run list.
- `test_a_lost_offer_reply_costs_a_pass_and_not_the_shot`: a lost reply re-offers
  the row, which runs once. **keep**.
- `test_an_aborted_shot_stops_requests_stays_red_and_is_retried`: an abort stops
  requests, the row stays red with the reason, and a retry runs it. **fix**: fold in
  the status-pull check of `test_the_status_pull_carries_the_reason_blacs_stopped`.
- `test_blacs_reports_it_and_carries_on`: a shot BLACS cannot read is held as
  rejected and BLACS keeps asking. **keep**.
- `test_deleting_the_row_lets_the_queue_go_on`: runmanager's row deletion.
  **cut**: runmanager's behaviour, for runmanager's tests.
- `test_runmanager_says_what_blacs_reported`: runmanager's output-box text.
  **cut**: message text; the tooltip check above carries the reason.
- `test_a_paused_queue_leaves_blacs_running_its_own_shot`: a paused queue leaves
  BLACS on its local override shot. **fix**: fold in the next test's
  checks, an unreachable runmanager as a second case (from `NoShotOfferedTests`),
  and the light's "Runmanager queue paused".
- `test_a_local_override_shot_reaches_neither_runmanager_nor_lyse`: the override
  shot is neither reported nor analysed. **cut**: folded into the test above.
- `test_the_status_pull_follows_the_shot_blacs_is_running`: runmanager's status
  pull names the running shot, then shows requesting. **keep**.
- `test_a_blacs_that_comes_back_answers_again`: a client reaches a restarted
  server. **cut**: a zprocess fact.
- `test_the_status_pull_carries_the_reason_blacs_stopped`: the status pull says
  why BLACS stopped. **cut**: folded into the abort test.
- `test_a_default_shot_is_a_visible_queue_row_and_reaches_lyse`: runmanager's
  default shot. **cut**: to BLACS it is an ordinary queued shot; runmanager's tests.

### tests/test_shot_execution.py

The shot loop and its exchange, over a stand-in runmanager.
- `StatusSnapshotTests`, four tests: the snapshot's fields, answered without the
  GUI thread, cleared after a shot. **cut**: message shape; the integration status
  pull runs on the server thread while the loop holds the main one.
- `SnapshotDuringCompletionTests`, three tests: the id stays while
  `shot_complete` handlers run. **cut**: bug history; the status is one
  `PublishedStatus`, written in one place.
- `test_a_reader_between_the_writes_never_sees_two_statuses_mixed`: the same,
  through a `__setattr__` hook. **cut**: as above.
- `test_a_held_outcome_is_delivered_when_the_loop_ends`: **cut**: covered by the
  quit test below.
- `test_it_is_delivered_even_when_the_loop_died_on_an_error`: **cut**: bug
  history.
- `test_an_outcome_that_cannot_be_delivered_names_its_shot`: **cut**: log text.
- `test_a_quit_while_an_outcome_is_held_waits_for_it_to_be_delivered`: quitting
  waits until the last shot's outcome reaches runmanager. **fix**: over the
  integration fixture's runmanager.
- `test_quitting_is_not_delayed_when_there_is_nothing_to_deliver`: **cut**: the
  next test bounds every quit.
- `test_a_runmanager_that_never_answers_cannot_hold_the_quit_open`: a dead
  runmanager cannot stop BLACS closing. **fix**: over the integration fixture,
  with its reply stalled.
- `test_an_outcome_that_could_not_be_delivered_is_named_before_the_exit`:
  **cut**: log text.
- `test_request_shots_is_not_part_of_saved_state`: **cut**: saved key names; the
  next test covers the behaviour.
- `test_restoring_saved_state_never_starts_requesting_shots`: BLACS never starts
  requesting shots by itself at start-up. **keep**.
- `ExchangeTests`: `reports_the_finished_shot_and_takes_the_next_one`,
  `is_not_reported_again` and `only_a_shot_runmanager_offered_has_an_outcome_to_report`
  are **cut**: the integration tests cover them. `only_runmanager_saying_it_is_paused_makes_it_paused`
  is **cut**: response shape; the light test covers what a user sees.
  `an_outcome_that_did_not_get_through_rides_on_the_next_exchange`: an outcome
  survives an unreachable runmanager. **fix**: over the integration fixture, its
  server down for one pass.
- `LivenessProbeTests`, four tests: the probe's timeout, its configuration, and
  its answer. **cut**: configuration plumbing, through a stand-in client.
- `test_requesting_shots_asks_runmanager_for_work`: **cut**: every integration test.
- `test_not_requesting_shots_makes_no_exchange_at_all` and
  `test_no_shot_is_taken_up_while_not_requesting_shots`: with Request shots off,
  nothing runs and nothing is asked. **fix**: one test over the integration
  fixture, with the light's "Not requesting shots" from
  `test_whatever_was_on_screen_gives_way_to_not_requesting_shots`.
- `test_the_last_outcome_still_goes_out_after_requests_are_switched_off`: turning
  requests off after a shot still reports it. **fix**: over the integration fixture.
- `NoShotOfferedTests`, three tests: BLACS keeps requesting, falls back to its
  override shot, and the light says why. **cut**: folded into the paused-queue test.
- `test_a_completed_fallback_shot_is_not_reported_to_runmanager`: **cut**:
  covered by the paused-queue test.
- `test_each_fallback_repetition_gets_a_file_of_its_own`: override reruns never
  write over data. **keep**.
- `test_a_rerun_copy_is_the_same_shot_without_the_previous_data`: a rerun copy
  keeps the shot's attributes and drops its data. **keep**.
- `test_repetitions_of_one_shot_differ_only_in_the_repeat_number`: **cut**: the
  test above.
- `test_a_failed_shot_is_reset_for_another_go_at_the_same_run`: a failed shot is
  reset in place, keeping its repeat number. **keep**.
- `test_a_shot_blacs_cannot_read_is_reported_without_stopping`: **cut**: the
  integration rejected-shot test.
- `test_requesting_shots_again_clears_the_error_and_asks_for_work`: **cut**: the
  integration abort test's retry.
- `test_a_local_override_shot_that_cannot_run_stops_requests_silently`: an
  override shot that cannot run stops requests and says so. **fix**: over the
  integration fixture.
- `test_a_shot_loop_that_dies_stops_requests_and_says_why`: an internal error
  stops requests and shows why. **keep**.
- `test_whatever_was_on_screen_gives_way_to_not_requesting_shots`: **cut**: folded
  into the requests-off test.
- `test_a_reason_requests_stopped_is_not_written_over`: **cut**: the abort test
  reads the reason after later passes.

### tests/test_architecture.py

- `test_blacs_offers_runmanager_nothing_that_changes_it`: the server's
  `handle_` names. **cut**: attribute names. Ian agreed: the rule needs no test, as no test says lyse does not control runmanager.

### tests/test_status_server.py

- `test_a_status_request_gets_what_blacs_is_doing`: **cut**: the integration
  status pull, with the real executor.

### tests/test_theme_events.py

- `test_a_palette_change_is_handled`: a light/dark switch recolours the tab
  headers. **keep**.
- `test_a_style_change_is_handled`: **cut**: the same handler.
- `test_application_palette_change_never_reaches_the_window`: **cut**: a Qt fact.

### tests/test_front_panel_settings.py

- `test_the_front_panel_can_be_written_and_read_back`: saved channel values and
  tab placement read back. **keep**.
- `test_the_saved_strings_are_bytes_as_earlier_versions_wrote_them`: **cut**:
  the numpy 2 dtype rename.

### tests/test_main_window_layout.py

- `test_the_device_tabs_get_the_rest_of_the_window`: resizing gives the space to
  the device tabs, and the shot controls stay their size. **keep**.
- `test_the_window_loads_with_the_controls_blacs_reaches_for`: **cut**:
  widget names; a real start reaches them.
- `test_the_shot_controls_are_not_in_a_splitter`: **cut**: structure; the
  keep above covers what an operator sees.
- `test_the_shot_controls_take_the_height_they_need_and_no_more`: **cut**: as above.

## Settled inputs

From `issues/seen_while_mapping.md` (blacs section) and Ian, as relayed by the
coordinator.

- **Transition to manual** waits until every pseudoclock has finished. This pass
  chooses the mechanism by comparing a labscript compile-time change, a block in
  blacs, and any other option.
- **BLACS's saved state** stays in its HDF5 settings file, the shot file's
  `/front_panel` group stays, and Settings keeps its HDF5 storage (Ian: changing
  the save files is a much bigger change than expected).

## Step 2.1: behaviour no one asked for

Compared file by file, `CodeHealth` against `master`. The fork's shot
execution (`shot_execution.py`, `client.py`, the status server, the runmanager
light and the liveness timeout) is the design that `docs/source/shot-management.rst`
states, which Ian agreed as the contract of record, so what it states was asked
for. The other changes are renames, the suite's shared plumbing (`file_dialogs`,
`ThemedWindow`, `ensure_str`, `best_contrast`, `package_version`), and upstream
code taken out on Ian's branches (BLACS's queue, analysis submission, the
easter egg).

### Candidates

| # | Behaviour | Where | Evidence | Proposal |
| --- | --- | --- | --- | --- |
| A1 | Every start warns that a plugin's `get_tab_classes()`/`tabs_created()` are deprecated | `__main__.py`, plugin tabs | Added with the plugin extraction (`6053911`); upstream has the hooks and no warning; Ian keeps the hooks (P7) | Delete the warning (−8) |

### Not candidates

- The local override shot, `rejected` against `failed`, the liveness timeout,
  the final outcome at quit and its warning, the status snapshot, Request shots
  starting off: all stated in `shot-management.rst`.
- The runmanager light: `LinkCheck`, Ian's.
- An unreachable runmanager is logged once until it answers again
  (`runmanager_rpc`): without it an outage writes a warning every pass.
- The legacy `queue_data` key and the leftover `blacs/measure_ball/` files are
  step 4's.

## Step 2.2: duplication

lyse, runmanager and runviewer are `LabscriptGuiApplication`s and run
`exec_qapplication`. BLACS is a plain `LabscriptApplication`, and does several
of the same jobs by hand. Searched with the librarian's inventory and
capability index, and by grep in every repo.

### Candidates

| # | Job | Should it exist? | Copies | Proposal | Size |
| --- | --- | --- | --- | --- | --- |
| D1 | Saving and restoring the main window's place and its splitters | Yes | `BLACS.restore_window` and the window data in `FrontPanelSettings.get_save_data`, measuring the frame by hand (its own comment calls the result unreliable); `LabscriptGuiApplication.geometry_save_data` and `restore_geometry`, which skip a restore once the screens have changed | BLACS becomes a `LabscriptGuiApplication` and uses the shared pair, still storing the result in its HDF5 file. An older file's window place is not restored, once. Restoring a maximised window goes, as the shared pair has no such case | blacs −35 +10 |
| D2 | A warning or a Yes/No question in a box | Yes | Eight boxes built by hand in `__main__.py` and `front_panel_settings.py`; `LabscriptGuiApplication.error_dialog` and `question_dialog` | With D1, the shared pair. The front-panel overwrite question would lose its default of No | blacs −45 +15 |
| D3 | A temporary file name | No: the standard library has it | `tempfilename` in `shot_execution.py`, upstream's | `tempfile.mkstemp` in `reset_failed_shot_file` | blacs −6 +2 |
| D4 | The TOML path of a legacy HDF5 globals file | Yes, while saved lists may still name `.h5` files | The connection table plugin's `normalize_globals_path`; `runmanager.globals_file.default_toml_path` | The plugin calls runmanager's | blacs −2 |
| D5 | An application's saved-config path in `app_saved_configs/<app>` | Yes | BLACS's start-up builds it by hand; `labscript_utils.labconfig.get_default_appconfig_file` | Use the shared function; the path is unchanged | blacs −3 +1 |

### Not candidates

- `exec_qapplication`: its Ctrl-C ends the event loop without BLACS's close
  sequence, which saves the front panel and shuts down the workers.
- Save and Load front panel against the shared configuration handlers: BLACS
  keeps its HDF5 save files (Ian).
- `CompileAndRestart` already uses the shared `OutputBox` and runmanager's
  compile. `runmanager_rpc` and the notifications have no second copy.

## Step 2.3: root causes

Every guard the fork added, in `CodeHealth` against `master`, traced to what
it guards against. Guards at a real boundary (the network to runmanager, the
shot loop's thread, start-up restore, a dialog cancelled) are not candidates.

### Candidates

| # | Guard | Where | Cause | Proposal | Size |
| --- | --- | --- | --- | --- | --- |
| R1 | A reply from runmanager that is not a dict counts as "nothing offered", and each field is read with a fallback | `exchange_with_runmanager` | None in the suite: every path of runmanager's `queue_exchange` and `offer_shot` returns `{'state', 'shot_id', 'path'}`, and a raised error reaches BLACS as an unreachable runmanager | Read the reply's fields directly. A malformed reply then stops the shot loop visibly, where today it is silent. `shot-management.rst` loses "or its reply could not be read" | blacs −5, docs −2 |
| R2 | Closing the window skips shutting the server down if there is no server yet | `BLACSWindow.closeEvent`, with the module global `blacs_server = None` | The server is created after `BLACS()` returns, because its handler reads the global `app`; the window is already shown, and the splash's `update_text` processes events in between | `BlacsServer` is given the BLACS it reports on, and is created at the end of `BLACS.__init__`, just before the window is shown; `closeEvent` shuts down `self.blacs.server`. The `None` guard and the global go | blacs −8 +6, test fixture ±2 |

### Not candidates

- `runmanager_rpc`'s catch and log-once, `manage()`'s catch, and the held outcome's
  delivery on the way out: boundaries, stated in `shot-management.rst`.
- `reset_failed_shot_file`'s swallowed errors and its `_retry` name: upstream's.
- `_restore_shot_execution_data`'s `queue_data` key and `NameError` catch: a
  compatibility leftover, for step 4.
- The connection table plugin's `settings_changed` handler, which reads its
  notification only once setup is complete: nothing fires before then.

## Step 2.4: dead code and compatibility leftovers

Every function, method and class blacs defines was counted across every
repo's checked-out branch (`.py`, `.ui`, `.rst`, `.md`, `.toml`, `.json`), hyde
and zprocess included; four appear nowhere but their own definition.

### Candidates

| # | What | Evidence | Proposal | Size |
| --- | --- | --- | --- | --- |
| K1 | `blacs/measure_ball/`: the Measure Ball game's seven files (`RabiBall.exe`, a DLL, its data, sound and splash) | The button that ran it was removed (`3b18ec7`); nothing refers to the folder | Delete the folder | −7 files, 6.6 MB |
| K2 | `CompileAndRestart.on_activate_default` | Upstream's GTK leftover: it calls `get_sensitive()`, a GTK method, and nothing connects it | Delete | −6 |
| K3 | `Notifications.close_all` | Upstream's, never called; it calls `close()` on the notification classes rather than their instances | Delete | −7 |
| K4 | `Tab.statemachine_timeout_remove_all` | Upstream's device-tab method, called by no tab in any repo | Delete. A lab's own device tab outside the repos that calls it would break. Held: it is documented in upstream's device guide (`docs/How to add a new device/`, `.tex` and `.pdf`), which the search missed | −10 |
| K5 | `FrontPanelSettings.restore`'s "legacy restore from GTK save data" (`AO`, `DO`, `DDS` datasets) | Front panels saved by the GTK BLACS, from before 2013 | Delete | −13 |
| K6 | `_restore_shot_execution_data`'s `queue_data` key, and its `NameError` catch for saves from before qtutils 2 | `queue_data` is what Production's BLACS writes; the fork writes `shot_execution_data` | Delete, and read `shot_execution_data` only. A settings file last saved by Production's BLACS then loses its local override path and last shots folder, once | −12 |

`PluginTab` is unused too, but it is what the plugin tab hooks use, which Ian
keeps (P7).

### Branches

| Branch | Content | Proposal |
| --- | --- | --- |
| `RunmanagerControl` | All of it is in `Development`, none in `Production`. Kept by a note so that `Production` could take it as a merge of its own | Delete: `Production` takes `Development` as a whole |
| `LivenessTimeout` | All of it is in `Development`. Kept by Ian's note, to follow `RunmanagerControl` to `Production` | Delete with `RunmanagerControl`, or keep both |
| `ServerInterfaces` | All of it is in `Development`. Kept live for the rule-13 test pass, which this pass's contract has now done | Delete |
| `PluginRefactor` | Three commits not in `Development`: two merges, and `57bb71b`, which CODEX says must never be merged. This pass's plugin change supersedes it | Delete |
| `RunmanagerTOMLGlobals` | Three merge commits not in `Development`, whose content is | Delete |
| `spielman_retired/share` | Reference only, in five repos | Left to the coordinator, as one decision for all five |

## Step 2.5: documents, TODO, FIXME, XXX

Marks: **do** (easy and worthwhile), **docs** (worthwhile but not easy: it
moves to a "Possible improvements" section of the docs), **go** (not
worthwhile, or already done; deleted with any commented-out code beside it).
There are no FIXMEs, and the XXXs are `_repXXXXX.h5` in the docs.

### TODOs

| # | Where | TODO | Mark |
| --- | --- | --- | --- |
| T1 | `output_classes.py:146`, `:156` | Name the device and calibration class in a unit-conversion error | go: the message names the channel and its connection |
| T2 | `output_classes.py:284`, `:514`, `:636`, `:726` | "Make this error better", for removing a widget that is not registered | go: a programming error, not a user's |
| T3 | `output_classes.py:1011` | A DDS case in the file's own demo | go |
| T4 | `front_panel_settings.py:70` | Fill `tab_data` for `restore_window` | go: `restore_window` handles a missing key |
| T5 | `front_panel_settings.py:313` | Save outputs whose value is a string | docs |
| T6 | `shot_execution.py:367` | Show which connections differ when a shot's connection table does not fit | docs |
| T7 | `shot_execution.py:618` | Make the 300 s programming timeout a setting | go: no one has asked for it |
| T8 | `shot_execution.py:923` | A race if BLACS closes as the master pseudoclock starts | go: a missing tab raises inside the shot, which stops requests |
| T9 | `device_base_class.py:462` | Only update a channel the user has not changed | go: the line below already checks it |
| T10 | `device_base_class.py:499`, `:501` | Keep the radio buttons' state, and sort the channels, in the changed-values dialog | go |
| T11 | `device_base_class.py:589` | Disable a device's widgets while its values differ, with a GTK line commented out | go |
| T12 | `notifications.py:39` | "Do we need to pass anything in here?" | go |
| T13 | `notifications.py:80` | Make the minimised widget, with `#ui2.hide()` | go: the next line makes it |
| T14 | `notifications.py:99` | "Cleanup a little more" | go |
| T15 | `__main__.py:212`, `:423` | Show the channels a front panel could not restore (`restore()`'s `question` and `error`, now dropped silently) | docs |
| T16 | `__main__.py:416` | Warn that locked channels are not restored | go: the warning already says so |
| T17 | `plugins/general/__init__.py:62` | Save settings of types other than strings | go |
| T18 | `docs/source/conf.py:114` | intersphinx for desktop-app and spinapi | go (approved) |

The three docs items go in a "Possible improvements" section added to
`docs/source/usage.rst`.

### Commented-out code

All of it goes, about 25 lines: `__main__.py:115`, `:177`, `:183`, `:450-451`;
`output_classes.py:408`; `device_base_class.py:592`; `tab_base_classes.py:211`,
`:393-394`, `:405`, `:548`, `:619`, `:783`, `:1000`, `:1065`, `:1094`, `:1104`;
`notifications.py:81`; and the commented `Menu` and `Notification` classes in
`plugins/general/__init__.py`. The property dictionaries commented in
`device_base_class.py:103-164` stay: they document the format a tab passes.

### Documents

| # | Document | Proposal |
| --- | --- | --- |
| P1 | `docs/source/plugins.rst` says plugins reach "the shot queue" | Say the shot executor (do) |
| P2 | `SPIELMAN_FORK_CHANGES.md`, at the root: the guide to merging upstream into the fork | Ian decides: keep it as the fork's merge guide, or move it into `docs/` |
| P3 | `docs/How to add a new device/` (`main.tex`, `main.pdf`): upstream's device guide | Keep: upstream's documentation |
