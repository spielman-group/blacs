# Tab state machine and worker shutdown

## Purpose

The fork takes upstream's tab state machine on the main thread and its worker shutdown
by a quit message (upstream 4c0f058, bfaaf25, f89cc2b, 69a2dd3, d00b690). This design
says what operators, device authors and the shot loop rely on, where the fork differs,
and how a worker releases its hardware, NI-DAQmx tasks above all, however it is ended.
Line numbers are of blacs `e4a704a` ("fork"), upstream/master `a3cb37a` ("upstream"),
labscript-devices `bfd7a65`, spielman-devices `cff9cf1` and zprocess `db42229`.

## Requirements

[fork]: the fork adds it. [not upstream]: upstream does not meet it.

- R1. A state's own code runs in the main thread, one state at a time per tab, in
  priority then queue order, only in the modes it allows.
- R2. Device code works unchanged: `define_state`, generators yielding `queue_work()`,
  a state returning before its first yield, modes, timeouts and `event_queue.put`.
- R3. The shot loop calls a tab's states and reads `tab.mode` from its own thread, and
  the restart receivers tell it of a tab restarted or stopped.
- R4. A worker job never blocks the GUI; different tabs' jobs run at once.
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
- R10. A worker's `shutdown()` runs before it exits, however it is ended, unless its
  main thread is stuck in a call that does not return; this clears NI-DAQmx tasks.
  [fork] [not upstream: only as a state, on restart, Stop or quit, if reached in 2 s]
- R11. A restart, Stop, a tab's close as BLACS quits, and a fatal error all end workers
  by R8 to R10; a restart or Stop first gives `shutdown()` 2 s as a state.
- R12. Stop leaves the tab open as "stopped" with a message that cannot be dismissed,
  so the shot loop refuses it as a tab in error; the button then reads Start. A tab
  stopped when BLACS quits starts as usual next time, as upstream's does (Ian).
- R13. BLACS's quit aborts the shot in hand, waits for the shot loop, gives each tab's
  `shutdown_workers` 2 s, then ends all tabs' workers at once and quits after.
- R14. The shot loop reports a tab restarted or stopped during a shot as "restarted or
  stopped". [fork: in `shot_execution.py`, F1]

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
- **Ending workers.** `Tab._end_workers` (upstream 825-876), in any thread, one call at
  a time per tab, pops the workers, sends each `'quit'` with a 1 s timeout, kills each
  it cannot reach, and kills any still alive at `WORKER_QUIT_TIMEOUT`.
- **Worker on quit.** The receiver interrupts the job, if any. [fork] It queues a quit
  for the main thread, which, once the job stops, runs `shutdown()` unless a `shutdown`
  job has returned, logs any error, and calls `kill_lock.exit_when_released(0,
  WORKER_KILL_LOCK_TIMEOUT)` (zprocess process_tree.py:173-179). The receiver gives the
  job 5 s and `shutdown()` 5 s more, then exits the same way, so `WORKER_QUIT_TIMEOUT`
  is 5+5+60+5 = 75 s. Upstream's receiver exits when the job stops (upstream 1162-1177).

### Message flows

- **A state.** Caller → `put` (main) → run → `yield queue_work(...)` → job thread:
  `to_worker.put`, two `from_worker.get` → `inmain_later` → `generator.send(result)`
  (main) → ... → `StopIteration` → `'idle'` → next run.
- **Restart.** Call the restart receivers, disable Restart and Stop, queue
  `_shut_down_workers` at priority -1 and start a 2 s timer; the first to finish calls
  `close_tab(finalise=False)`, which closes the state machine (interrupting the worker
  queues and the job thread). A thread runs `finalise_close_tab`: join the job thread
  2 s, `_end_workers`, stop the output box. Then `__init__` rebuilds the tab.
- **Stop.** As a restart up to `shutdown()`; then close the state machine, show the
  message, set `'stopping'`, `_end_workers` in a thread, `'stopped'`; Start restarts.
- **BLACS quit.** `closeEvent` stops the shot loop and waits (fork __main__.py:131-149);
  `on_save_exit` queues each `shutdown_workers` (441-453); `finalise_quit` closes a tab
  once that is done, 2 s have passed, or the tab is `'fatal error'`, `'stopping'` or
  `'stopped'`, with `finalise_close_tab` in a thread (474-490), and quits after (508).
- **Fatal error.** Stop running states, show the error, set `'fatal error'`, disable
  the error's close button, `_end_workers` in a thread (upstream 301-315).

### The shot loop's thread

The shot loop runs on `ShotExecutor.manager` (fork shot_execution.py:136) and stays
safe unchanged. `transition_device_to_buffered` and `get_device_error_state` run in
the main thread (463-475). `abort_buffered`, `start_run` and `transition_to_manual`
(481, 756, 774, 811, 880) only call `event_queue.put`. It reads `tab.mode` (480, 810),
which states set in the main thread (device_base_class.py:622). Restart receivers run
in the main thread and put to a `queue.Queue` (621-623); Stop calls them too. A
stopped tab's message is an error, so the next shot refuses it (464-470).

### __main__'s quit sequence

Only `finalise_quit` changes: `tab.state == 'fatal error'` (fork __main__.py:474-475)
also takes `'stopping'` and `'stopped'` (d00b690). A worker that does not exit holds
the quit for 2 s plus `WORKER_QUIT_TIMEOUT`, all tabs at once.

### Changes per file

| File | Change | From | Lines |
| --- | --- | --- | --- |
| `blacs/tab_base_classes.py` | `StateMachine`, job thread | 4c0f058 | +198 −198 |
| same | merged display update; set only on change | bfaaf25, f89cc2b | +30 |
| same | timeouts, `_end_workers`, worker receiver, `_quit` | 69a2dd3 | +109 −26 |
| same | Stop/Start, `Worker.shutdown`, fatal error ends workers | d00b690 | +118 −7 |
| same | F2, F3 | fork | +15 |
| `blacs/tab_frame.ui` | `button_stop` | d00b690 | +14 |
| `blacs/__main__.py` | `finalise_quit` stopped states | d00b690 | 2 |
| `blacs/shot_execution.py` | "restarted or stopped" (741, 821) | d00b690 | 2 |
| `docs/source/device-tabs.rst` | 234-240: main thread and job thread | fork | 5 |
| `tests/test_state_machine.py` | see Tests | fork | +45 |
| `DEEP_PASS.md` | 42-44: the entry moves to what CodeHealth carries | fork | 3 |

### Where the fork differs from upstream

- F1. Status texts are in `shot_execution.py`: runmanager owns the shot queue.
- F2. `_end_workers` catches `ProcessExited` with `TimeoutError` (upstream 853-854),
  which the fork's zprocess raises from a put to an exited local worker
  (process_tree.py:506-507, 1501). Uncaught, it ends a restart before the rebuild.
- F3. The worker runs `shutdown()` on quit (R10), for NI-DAQmx. Upstream ends workers
  after a fatal error without it, and its quit runs no device code before `os._exit`.
- F2 and F3 are offered upstream as a pull request (Ian).
- F4. The fork's workerarg rule stays (fork 843), now refusing `shutdown` too.
- F5. The Qt lock comment (fork 61-63) goes with the blocking `get` it explains;
  `_initialise_worker` runs first as it is queued at priority -1 (502).
- F6. Upstream b52c85a, a shot's tab transitions in the main thread in one pass per
  group, builds on 4c0f058. It follows this change, ported into `shot_execution.py`'s
  loop, which replaced the queue manager it rewrites.

## What the quit message gives NI-DAQmx

This settles labscript-devices DEEP_PASS.md:145. An idle NI worker holds tasks (the
outputs' NI_DAQmx/blacs_workers.py:72, 101-129; the acquisition's 439).

- The quit alone ends an idle worker with `os._exit`, clearing nothing. An interrupted
  job runs NI's `finally: release_tasks()` (79-80, 391-392, 553-554, 894-895), but
  `transition_to_buffered` (331-354) has none. A waiting wait monitor holds `kill_lock`
  (803), so its worker ends with its tasks after 60 s.
- With R10 the worker runs NI's `shutdown()` (82-83, 441-443, 765-766) once the job
  stops, on every path. It clears every task (`release_tasks`, 42-59) and stops the
  wait monitor (873), releasing the kill lock. Task attributes default to `None`
  (64-65, 414, 722-723), so it is safe after an interrupted `init`.
- An interrupt between `setattr(self, name, None)` (52) and `ClearTask()` (55) drops a
  task from its attribute; PyDAQmx's `Task.__del__` clears it as the exception unwinds.
- A DAQmx call that does not return in 5 s keeps `shutdown()` from running, and the
  kill ends the worker with its tasks; KNOWN_ISSUES.md:13 describes such a call.

NI_DAQmx needs no change. Its KNOWN_ISSUES.md:14 then reads: a wait monitor thread
that will not stop holds `kill_lock`, so its worker exits only after 60 s.

## Compatibility

labscript-devices has 35 `@define_state` and 35 `queue_work(` uses, spielman-devices
53 and 34; all keep working (R2). Neither repo uses `Tab.mainloop`, `_mainloop_thread`,
`StateQueue`, `event_queue` or `from_parent`, overrides `Worker.mainloop`, or sets a
SIGINT handler.

- TriggerableCamera/blacs_tabs.py:368-371 shuts the image receiver in a `restart`
  override, now before `shutdown()`. In continuous mode `shutdown()` then never
  returns: the continuous thread waits for the gone receiver on a REQ socket with no
  timeout (blacs_workers.py:213, 269), and `stop_continuous` joins it (301). Change:
  shut the receiver in a `close_tab` override instead.
- `close_tab` overrides (CiceroOpalKellyXEM3001/blacs_tabs.py:73,
  EnviroMUX/blacs_tabs.py:157, arduino_tabs.py:45) run when a stopped tab is started,
  not on Stop. Meanwhile the Arduino loop (arduino_tabs.py:57-63, started at
  Arduino_Device/blacs_tabs.py:495) queues a state every 2 s and EnviroMUX's timeout
  (blacs_tabs.py:92) one more; none runs, and Start discards them. No change.
- pseudoclock.py:90-96 calls `self._restart_receiver` itself, and RFBlaster/
  blacs_tabs.py:107, 127 set `self.mode` in a state; both work as now.
- Worker threads (NI_DAQmx/blacs_workers.py:960, AlazarTechBoard/blacs_workers.py:139,
  TriggerableCamera/blacs_workers.py:291, 356, EnviroMUX/blacs_workers.py:37) are not
  interrupted; they end where `shutdown()` stops them, or with the process.
- 23 workers in labscript-devices and 2 in spielman-devices define `shutdown()`. R10
  calls it at most once per process, as TriggerableCamera's closes the camera
  (blacs_workers.py:471-474).

## Tests

tests/test_state_machine.py:63-64 waits on `tab._mainloop_thread`; it calls
`tab.close_tab()` instead. tests/test_worker.py:35 and tests/test_integration.py:129
stay. New tests drive `Tab` and real workers, in about 45 lines:

1. A state that returns before its first yield leaves the tab running the next state
   (the present test, adapted). R2.
2. `shutdown()` runs before the worker exits on a restart, Stop, `close_tab()` and a
   fatal error, idle and in a sleeping job (it writes a file). R8, R10, R11.
3. A job that ignores the interrupt is killed at `WORKER_QUIT_TIMEOUT`, set short in
   the parent only. R9.
4. A stopped tab runs none of the states it is given, and Start brings up a worker
   that runs jobs. R12.
