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
| P5 | Saved plugin state has no manager method: `front_panel_settings.py` loops over the plugins itself, and lyse and runmanager would repeat it. | `PluginManager.get_save_data()` returns `{name: data}`, the partner of `instantiate_plugins(plugin_settings)`. blacs stores it in its configuration with the front-panel move (seen-while-mapping). | lu +8; blacs −6 |
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
- `Settings` keeps its pages' data in a dict that the application saves and
  loads, in place of its own HDF5 file; `get_save_data()` returns
  `{page name: data}`.
- Saved data is whatever TOML can hold, as in every application's configuration:
  a blacs plugin that saves `None` today leaves that key out instead.
- blacs stores the two under `plugins` and `settings` in its one TOML
  configuration, `blacs.toml` in the labconfig's `app_saved_configs/blacs` folder,
  found by `get_default_appconfig_file` as lyse's and runmanager's are. The old
  `<host>_BLACS.h5` there is read only at a first start that finds no TOML (see
  "Settled inputs").
- hyde's plugins override the two `BasePlugin` methods, calling `super()` first
  for the base GUI state, and hyde decides where its session stores them. hyde's
  session is briefed when this lands.

| Who | Change | Size |
| --- | --- | --- |
| labscript-utils | the two `BasePlugin` hooks, the two manager methods, `Plugin()` built with no settings | +25 |
| labscript-utils | `Settings` stores a dict; its HDF5 storage goes | −45 +15 |
| blacs | every plugin implements the two hooks; `front_panel_settings.py` stops looping over plugins; the `eval()` of saved plugin data goes. Part of the front-panel move | about −20 |
| hyde | its plugins override the two hooks; `get_session_toml_data` goes | about ±40, hyde's session |

Open with Ian: a settings page is now written to its file the moment its dialog
closes. In a `LabscriptGuiApplication` it would be saved with the
configuration, so on Save, or through the prompt on quitting, as lyse and
runmanager save everything else.

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

With the front-panel move, after Ian's answers on it:
`labscript_utils.settings.Settings(saved_data=None, parent=None, page_classes=())`
builds each page from `saved_data.get(page name, {})`, keeps a page's `save()`
when the dialog is accepted, and returns `{page name: data}` from
`get_save_data()`; its HDF5 storage goes.

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

Both go with the front-panel move onto the application configuration.
- `test_the_front_panel_can_be_written_and_read_back`: saved channel values and
  tab placement read back. **keep** until then.
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
- **The front panel's state** moves off its HDF5 file onto
  `LabscriptGuiApplication`'s configuration save and window restore, as lyse,
  runmanager and runviewer use.
- **The old HDF5 front-panel file** is still read when no TOML configuration
  exists, once, after which the state is saved as TOML. That path is marked as
  labscript-utils' INI fallback is (`labconfig.py:261`): the comment
  `# LEGACY HDF5 FRONT PANEL COMPATIBILITY. DEPRECATED CODE, WILL BE REMOVED.`,
  and a `warnings.warn(..., FutureWarning)` when the old file is actually read,
  saying it will be converted to TOML.
