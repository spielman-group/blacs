# Tab state machine and worker shutdown

## Purpose

The fork takes upstream's tab state machine on the main thread and its worker shutdown
by a quit message (upstream 4c0f058, bfaaf25, f89cc2b, 69a2dd3, d00b690). This design
says what operators, device authors and the shot loop rely on, where the fork differs,
and how far a worker's cleanup goes to release its hardware, NI-DAQmx tasks above all.
Line numbers are of blacs `3ac393b` ("fork"), upstream/master `a3cb37a` ("upstream"),
labscript-devices `9009e07`, spielman-devices `874e25e` and zprocess `1b9de75`.

## Requirements

[fork]: the fork adds it. [not upstream]: upstream does not meet it.

- R1. A state's own code runs in the main thread, one state at a time per tab, in
  priority then queue order, only in the modes it allows.
- R2. Device code works unchanged: `define_state`, generators yielding `queue_work()`,
  a state returning before its first yield, modes, timeouts and `event_queue.put`.
- R3. The shot loop calls a tab's states and reads `tab.mode` from its own thread, and
  the restart receivers tell it of a tab restarted or stopped.
- R4. A worker job never blocks the GUI; different tabs' jobs run at once. Closing a
  tab never waits in the GUI thread for a worker to start. [not upstream: F10]
- R5. A state costs the main thread only its own code: no blocking hand-off to it.
- R6. A job's error is shown on the tab and its state goes on with `None`; an error in
  a state's own code, or a refused job, is a fatal error.
- R7. A tab's state label, error text, icon and colour follow its state without
  blocking the caller, and are redrawn only when they change.
- R8. A worker is ended by a quit message: its job is interrupted and it exits once the
  job stops. A job that does not stop in 5 s (`WORKER_ABORT_TIMEOUT`) delays the exit
  until no HDF5 file is open for writing, at most 60 s (`WORKER_KILL_LOCK_TIMEOUT`).
- R9. The tab kills a worker not gone by `WORKER_QUIT_TIMEOUT`, and at once one not
  started, unreachable in 1 s, or exited [fork, F2]; a remote server gets 5 s a request.
- R10. A worker's cleanup is a bounded attempt. On a quit message or a lost parent (its
  heartbeats unanswered) it interrupts its job, then runs `shutdown()`, which clears
  NI-DAQmx tasks; the job gets 5 s and `shutdown()` 5 s more, then the worker exits.
  [fork] [not upstream: only as a state, on restart, Stop or quit, if reached in 2 s]
- R11. A restart, Stop, a tab's close as BLACS quits, a fatal error and a lost parent
  end workers by R8 to R10; a restart or Stop first gives `shutdown()` 2 s as a state.
  As `shutdown()` may come mid-shot, it joins the worker's own threads before it closes
  their hardware.
- R12. Stop leaves the tab open as "stopped" with a message that cannot be dismissed,
  so the shot loop refuses it as a tab in error; the button then reads Start. A tab
  stopped when BLACS quits starts as usual next time, as upstream's does (Ian).
- R13. BLACS's quit aborts the shot in hand, waits for the shot loop, gives each tab's
  `_shut_down_workers_then` 2 s, then ends all tabs' workers at once and quits after.
- R14. The shot loop reports a tab restarted or stopped during a shot as "restarted or
  stopped". [fork: in `shot_execution.py`, F1]

### What cleanup does not cover

`shutdown()` does not run, or does not finish, and the tasks stay held, when:

- the worker crashes;
- the tab kills it, unreachable in 1 s or alive at `WORKER_QUIT_TIMEOUT`; one killed
  before its `init` runs has opened no task;
- BLACS is killed with its workers, as a process-tree kill does; BLACS killed alone is
  a lost parent (R10);
- the job's 5 s or `shutdown()`'s 5 s run out;
- the job or `shutdown()` is stuck in a driver call that does not return.

## Architecture

### Components and threads

- **Main thread.** Each tab's `StateMachine` owns its `StateQueue` (4c0f058). `put`
  keeps `@inmain_decorator(True)` (fork tab_base_classes.py:92-93), so any thread may
  queue a state, and schedules a run with `QTimer.singleShot(0)`. A run takes the first
  state allowed in `tab.mode`, calls it, and steps its generator to each yield. Setting
  `tab.state` posts one merged update (bfaaf25), applied only on change (f89cc2b).
- **Job thread, one per tab.** It takes each yielded job, starts the worker process
  for `init`, sends the job, waits for its acknowledgement and result, and posts the
  result back with `inmain_later`. It replaces the mainloop thread (fork 286-288,
  714-812), which ran each step of a state through a blocking `inmain` (735, 742, 790).
- **Worker process.** Its main thread runs jobs; a receiver thread reads the parent's
  queue, so a quit arrives during a job (69a2dd3). `interrupt_main_thread` (zprocess
  utils.py:116-142) sends SIGINT, which raises `KeyboardInterrupt` only in a job.
- **Ending workers.** `Tab._end_workers` (upstream 825-876), in a thread, one call at a
  time per tab, pops the workers, interrupts and awaits each one's startup, sends each
  `'quit'` with a 1 s timeout, and kills each unreachable or alive at the timeout.
- **Worker's quit.** [fork] A quit message, read by the receiver, or zprocess's
  `on_parent_death` hook starts it once; neither runs device code. It interrupts the
  job and queues a quit for the main thread, which, once the job stops, runs
  `shutdown()` unless a `shutdown` job has returned, logs any error, and exits by
  `kill_lock.exit_when_released(0, WORKER_KILL_LOCK_TIMEOUT)` (zprocess
  process_tree.py:173-179). Its thread gives the job 5 s and `shutdown()` 5 s more,
  then exits the same way: `WORKER_QUIT_TIMEOUT` is 5+5+60+5 = 75 s. Upstream's quit
  exits once the job stops (upstream 1162-1177).
- **Lost parent.** The worker sets `process_tree.heartbeat_client.on_parent_death`
  (process_tree.py:219, 1575); heartbeat failure calls it in a thread of its own, then
  exits when the kill lock is free `PARENT_DEATH_TIMEOUT` later (278-281). The quit
  fills the default 10 s exactly; the worker sets 15 s, so its own deadlines govern.
  Without a shared secret, a send to a lost parent never returns: its PAIR socket, not
  reconnecting (1534-1545), never becomes writable again, and the worker's `WriteQueue`
  has no timeout or child to check (497). With one, the socket stays writable and the
  send is queued, so the suite, run with a shared secret, does not reach this case. So the hook first interrupts `to_parent` (523), and the main thread
  drops a send that raises `Interrupted` (upstream 1121, 1152) and takes the quit.

### Message flows

- **A state.** Caller → `put` (main) → run → `yield queue_work(...)` → job thread:
  `to_worker.put`, two `from_worker.get` → `inmain_later` → `generator.send(result)`
  (main) → ... → `StopIteration` → `'idle'` → next run.
- **Restart.** Call the restart receivers, disable Restart and Stop, queue
  `_shut_down_workers` at priority -1 and start a 2 s timer; the first to finish calls
  `close_tab(finalise=False)`, which closes the state machine (interrupting the worker
  queues and the job thread). A thread runs `finalise_close_tab`: interrupt each
  worker's startup (F10), join the job thread 2 s, `_end_workers`, stop the output box;
  a tab's override may release more after (Compatibility). `__init__` rebuilds the tab.
- **Stop.** As a restart up to `shutdown()`; then close the state machine, show the
  message, set `'stopping'`, `_end_workers` in a thread, `'stopped'`; Start restarts.
- **BLACS quit.** `closeEvent` stops the shot loop and waits (fork __main__.py:131-149).
  `on_save_exit` calls each device tab's `_shut_down_workers_then` (upstream 893-914),
  passing over plugin tabs, which share `tablist` (__main__.py:271), as upstream does
  (F9), with a callback setting `tab.shutdown_workers_complete`, called at once for a closed or
  failed state machine (898-900). `finalise_quit` closes a tab once that is set, after
  2 s, or in `'fatal error'`, `'stopping'` or `'stopped'`, in a thread (474-490), and
  quits after (508). A tab whose worker will not exit delays the quit by at most 2 s
  for the state, 2 s for the job thread, 1 s per worker for its quit message and
  `WORKER_QUIT_TIMEOUT`, plus R9's 5 s per remote request. The wait for a worker's
  startup to cancel is not bounded (F10): it ends when a local `Popen` or a name lookup
  returns, and to stop waiting sooner blacs would need a thread to kill a child that
  `Popen` returns later.
- **Fatal error.** Stop running states, show the error, set `'fatal error'`, disable
  the error's close button, `_end_workers` in a thread (upstream 301-315).

### The shot loop's thread

The shot loop runs on `ShotExecutor.manager` (fork shot_execution.py:136), safe
unchanged: `transition_device_to_buffered` and `get_device_error_state` run in the main
thread (463-475); `abort_buffered`, `start_run` and `transition_to_manual` (481, 756,
774, 811, 880) only queue states; `tab.mode` (480, 810) is set in the main thread
(device_base_class.py:622); restart receivers, which Stop calls too, put to a
`queue.Queue` (621-623); a stopped tab's message is an error, which fails it (464-470).

### Changes per file

| File | Change | From | Lines |
| --- | --- | --- | --- |
| `blacs/tab_base_classes.py` | `StateMachine`, job thread; merged display update, set only on change | 4c0f058, bfaaf25, f89cc2b | +228 −198 |
| same | timeouts, `_end_workers`, worker receiver, `_quit` | 69a2dd3 | +109 −26 |
| same | Stop/Start, `Worker.shutdown`, fatal error ends workers | d00b690 | +118 −7 |
| same | F2, F3 with the lost parent, F10; F9 drops `shutdown_workers` | fork | +28 −8 |
| `blacs/tab_frame.ui` | `button_stop` | d00b690 | +14 |
| `blacs/__main__.py` | `finalise_quit` stopped states; `on_save_exit` (F9) | d00b690, fork | +5 −5 |
| `blacs/shot_execution.py` | "restarted or stopped" (741, 821) | d00b690 | 2 |
| `docs/source/device-tabs.rst` | 234-240: main thread and job thread | fork | 5 |
| `tests/` | see Tests | fork | +80 |
| `DEEP_PASS.md` | 42-44: the entry moves to what CodeHealth carries | fork | 3 |

### Build

The build applies upstream's five commits as they are, with upstream's later edits to
their lines (23376cd's logging arguments, 7a8f018's `Icon`), then adds each F-item as
a commit of its own, and nothing else. Once built,
`git diff upstream/master CodeHealth -- blacs/tab_base_classes.py` holds only F-items.

| Upstream | Taken | Except |
| --- | --- | --- |
| bfaaf25, f89cc2b | as is | |
| 4c0f058 | as is | `close()` leaves `interrupt_startup()` to `finalise_close_tab` (F10) and iterates a copy of the workers (F12) |
| 69a2dd3 | as is | `_end_workers` catches `ProcessExited` (F2); the worker runs `shutdown()` on quit and on a lost parent, dropping its sends to a lost parent (F3) |
| d00b690 | as is | Stop calls `_stop_workers` (F7); `shutdown_workers` goes, with `on_save_exit`'s call (F9); the status texts are in `shot_execution.py` (F1) |

The file first takes upstream's text in place of the fork's style pass, so that the
commits apply as they are. The commits, `e56147f..` on CodeHealth, with the suite at
each:

| Commit | From | What it does | Suite |
| --- | --- | --- | --- |
| 413f87a | fork | `tab_base_classes.py` is upstream's with the five commits reverted, keeping F4 and F11; the example tab's `button=None` (9fa23e7) | 28 passed |
| 360b9ae | bfaaf25 | posts the state label and tab icon update | 28 passed |
| 9eee99c | 4c0f058 | the state machine in the main thread, with a job thread | 1 failed: the next commit's test |
| 2b8b372 | fork | the state machine test closes its tab with `close_tab()` | 28 passed |
| 3d6bfd7 | f89cc2b | error text, icon and colour set only on change; its `QIcon` is 7a8f018's `Icon` | 28 passed |
| 080d4f6 | 23376cd, its lines here | logging arguments passed to the state machine's logger | 28 passed |
| 106083b | 69a2dd3 | workers ended by a quit message; the worker's receiver thread | 28 passed |
| 2babc61 | d00b690 | the Stop button, `Worker.shutdown`, a fatal error ends the workers; without `experiment_queue.py` | 28 passed |
| e16c84e | 7a8f018, its line here | the stop button's `Icon` | 28 passed |
| 653982d | F1 | "restarted or stopped" in `shot_execution.py` | 28 passed |
| 3a97708 | F2 | `_end_workers` catches `ProcessExited` | 28 passed |
| c03388b | F12 | `close()` iterates a copy of the workers | 28 passed |
| c709783 | F10 | `interrupt_startup()` moves to `finalise_close_tab` | 28 passed |
| a6dcf12 | F7 | the button calls `_stop_workers`, not `stop` | 28 passed |
| b584192 | F9 | `shutdown_workers` goes; `on_save_exit` calls `_shut_down_workers_then` | 28 passed |
| 95d7317 | F3 | the worker's quit calls `shutdown()`, also on a lost parent | 28 passed |
| abd494c | fork | Tests | 30 passed, 8 subtests |
| 6f2d38a | fork | `device-tabs.rst` | 30 passed, 8 subtests |
| 367dd44 | fork | `DEEP_PASS.md` | 30 passed, 8 subtests |
| a7c8ff5 | fork | this design: a send to a lost parent blocks only without a shared secret | 30 passed, 8 subtests |

Against upstream/master: `blacs/tab_base_classes.py` differs by F2, F3, F4, F7, F9,
F10, F11 and F12 only (+85 −36); `blacs/tab_frame.ui` is identical; `blacs/__main__.py`
keeps d00b690's `finalise_quit` line as it is, and `on_save_exit` differs by F9 and the
fork's earlier changes; `shot_execution.py` is the fork's (upstream's queue is
`experiment_queue.py`).

### Where the fork differs from upstream

- F1. Status texts are in `shot_execution.py`: runmanager owns the shot queue.
- F2. `_end_workers` catches `ProcessExited` with `TimeoutError` (upstream 853-854),
  which the fork's zprocess raises from a put to an exited local worker
  (process_tree.py:515-516, 1510). Uncaught, it ends a restart before the rebuild.
- F3. The worker runs `shutdown()` on quit and on a lost parent (R10), for NI-DAQmx,
  dropping its sends to a lost parent. Upstream ends workers after a fatal error
  without it, its quit runs no device code before `os._exit`, and nor does its
  heartbeat failure (process_tree.py:281).
- F10. `StateMachine.close()` calls `interrupt_startup()` (upstream 326), which sets
  the cancel, then waits without timeout until `subprocess()` returns (zprocess
  process_tree.py:1188-1201, 1108); the cancel does not cut short a local `Popen` or a
  remote worker's name lookups (1496, 1475). The call moves to the start of
  `finalise_close_tab`, before the job thread is joined; `_end_workers` makes it for
  Stop and a fatal error (upstream 847). Each close starts that thread at once.
- F12. `StateMachine.close()` iterates `self.workers.values()` (upstream 324) while
  `_end_workers_locked()` pops from that dict under a lock `close()` does not take, so
  a fatal error racing a restart or quit raises "dictionary changed size during
  iteration" (reproduced with F10 applied). `close()` iterates `list(...)` of it.
- F2, F3 with its use of the hook, F4, F7, F9, F10, F11 and F12 are offered upstream as
  one pull request, and zprocess's `on_parent_death` hook to zprocess's upstream (Ian).
- F4. The fork's workerarg rule stays (fork 843): a workerarg overrides a device
  worker's class attribute, its default, and only the Worker base class's names are
  refused, now `shutdown` too. Upstream refuses any name the worker has.
- F5. The Qt lock comment (fork 61-63) goes with the blocking `get` it explains;
  `_initialise_worker` runs first as it is queued at priority -1 (502).
- F6. Upstream b52c85a, a shot's tab transitions in the main thread per group, builds
  on 4c0f058; it follows this change, ported into `shot_execution.py`'s loop.
- F7. Stop calls a private `Tab._stop_workers`, not upstream's `Tab.stop` (upstream
  927): PulseBlaster/blacs_tabs.py:155 and NarwhalPulseGenerator/blacs_tabs.py:384
  define a `stop` state that stops the board, so Stop would queue that instead.
  Upstream labscript-devices has both (PulseBlaster:183, NarwhalPulseGenerator:384).
- F8. The worker's receiver thread keeps upstream's name, `Worker._receive` (upstream
  1155); LightCrafterDMDWorker's reply reader, which would replace it, is renamed
  `_receive_reply` (labscript-devices c3865f0; upstream form UpstreamBugFixes c1bd755).
- F9. One mechanism shuts a tab's workers down: `_shut_down_workers_then` and its
  `_shut_down_workers(callback)` state (upstream 893-919). The `shutdown_workers` state
  (upstream 784-789, fork 551-556) goes. `on_save_exit` calls `_shut_down_workers_then`
  in its place (fork __main__.py:452-453), behind upstream's `hasattr` test, which
  passes over plugin tabs, as `finalise_quit`'s test of `finalise_close_tab` does; `_end_workers`'s timeout
  replaces `finalise_close_tab`'s own (fork 596-601). `Worker.shutdown()` takes no
  arguments: of 25 overrides one calls `super()`'s, and PineBlaster's `abort` calls it.
- F11. `PluginTab` has `get_device_save_data()` (fork 963-965): a shot file's tab data
  calls it on every tab (front_panel_settings.py:210), plugin tabs among them
  (__main__.py:271). Upstream's `PluginTab` has none (upstream 1248-1256), so the call
  fails for a plugin tab.

## What the quit gives NI-DAQmx

This settles labscript-devices DEEP_PASS.md:167-168. An idle NI worker holds tasks (the
outputs' NI_DAQmx/blacs_workers.py:72, 101-132; the acquisition's 439).

- Without R10 the quit ends an idle worker with `os._exit`, clearing nothing. An
  interrupted job runs NI's `finally: release_tasks()` (79-80, 391-392, 569-570,
  910-911), but `transition_to_buffered` (331-354) has none. A waiting wait monitor
  holds `kill_lock` (818), so its worker ends with its tasks after 60 s.
- With R10 the worker runs NI's `shutdown()` (82-83, 441-443, 780-781) once the job
  stops. It clears every task (`release_tasks`, 42-59) and stops the wait monitor
  (889-897), releasing the kill lock. Task attributes default to `None` (64-65, 414,
  737-738), so it is safe after an interrupted `init`.
- An interrupt between `setattr(self, name, None)` (52) and `ClearTask()` (55) drops a
  task from its attribute; PyDAQmx's `Task.__del__` (Task.py:116) clears it.

The cases under "What cleanup does not cover" leave tasks held; KNOWN_ISSUES.md:13 is
such a call. NI_DAQmx needs no change. Its KNOWN_ISSUES.md:14 then reads: a wait
monitor thread that will not stop holds `kill_lock`, so its worker exits after 60 s.

## Compatibility

labscript-devices has 35 `@define_state` and 35 `queue_work(` uses, spielman-devices
53 and 34; all keep working (R2). Neither repo uses `Tab.mainloop`, `_mainloop_thread`,
`StateQueue`, `event_queue`, `from_parent` or a SIGINT handler, or overrides `mainloop`.

- `TriggerableCameraWorker.shutdown()` aborts and joins a shot still acquiring before
  it closes the camera, and does not resume continuous acquisition paused for the shot
  (common/triggerable_camera/blacs_workers.py:554-563). `abort()` disarms a camera
  armed for a shot whether or not its acquisition thread exists or has started, and
  cleanup joins the thread, kept before it starts, after any interruption; a worker
  whose `init` failed part way still closes its camera. An AndorSolis abort leaves the
  camera disarmed though no acquisition started, so its `shutdown`, which refuses an
  armed camera (andor_utils.py:647-651), goes through. These are labscript-devices
  8fdcc7b, c2496ce, 9009e07, 260e8a3 and f88b9cc, with tests; upstream forms
  UpstreamBugFixes b71e7d9, 1fed408, 36ac52a, b9d34b4 and 7fb8550.
- The continuous thread polls for the tab's reply to each frame every 100 ms and gives
  up once continuous acquisition is stopped, its REQ socket relaxed and correlated
  (blacs_workers.py:272-276, 338-340; 9009e07, upstream form fe3768b). So
  `stop_continuous` (369-375) returns once BLACS has died, or once the tab's image
  receiver, which `TriggerableCameraTab`'s `restart` override shuts
  (blacs_tabs.py:375-378), is gone. The tab needs no change.
- `close_tab` overrides (CiceroOpalKellyXEM3001/blacs_tabs.py:73, EnviroMUX/
  blacs_tabs.py:157, arduino_tabs.py:43) run when a stopped tab is started, not on Stop.
  Meanwhile the Arduino loop (arduino_tabs.py:55-62, started at Arduino_Device/
  blacs_tabs.py:495) queues a state every 2 s and EnviroMUX's timeout (blacs_tabs.py:92)
  one more; none runs, and Start discards them. No change.
- common/pseudoclock.py:90-96 calls `self._restart_receiver` itself, and RFBlaster/
  blacs_tabs.py:82, 102 set `self.mode` in a state; both work as now.
- Other worker threads (NI_DAQmx/blacs_workers.py:976, AlazarTechBoard/
  blacs_workers.py:139, EnviroMUX/blacs_workers.py:37) are not interrupted; they end
  where `shutdown()` stops them, or with the process.
- 23 workers in labscript-devices and 2 in spielman-devices define `shutdown()`. The
  quit skips it after a `shutdown` job has returned, as the camera's closes its camera.

## Tests

The tests prove that each way a worker ends releases its hardware, that a hanging
cleanup is cut off, and the camera's order. They drive real tabs and worker processes.
Only hardware is faked: a task that writes a release file when cleared, proving the
release and not just the call, and a camera. A worker's job writes a file once it has
started, which the test waits for. Of the present tests only
tests/test_state_machine.py:63-64 changes, to `close_tab()`.

| Test | Requirements | How |
| --- | --- | --- |
| Cleanup | R8, R10, R11 | A restart, Stop, `close_tab()` as BLACS quits, and a fatal error, each with the worker idle and in a sleeping job: the release file exists once the worker has exited. |
| Lost parent | R10 | A helper process starts the worker, idle and in a sleeping job, and is killed; the release file appears. |
| Bounded | R10 | A `shutdown()` waiting on an event never set: the worker exits after its 5 s, with no release file. |
| Camera mid-shot | R11 | labscript-devices `ShutdownTests` (8fdcc7b), changed so its fake camera's `grab` sets an `Event` on entry: the test waits for it, calls `shutdown()`, and finds the acquisition thread ended before the camera closed. |
| Untested by choice (Ian) | R1-R7, R9, R12-R14; R8's wait for an HDF5 file; R10's 5 s for a job; R11's 2 s for `shutdown()` as a state; F2, F4, F7, F9, F10, F11, F12 | |

The blacs tests come to about 80 lines beside about 490 of code.
