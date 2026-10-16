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
