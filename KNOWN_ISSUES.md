# Known issues

- `shot_execution.py`, transition to manual: restarting a device of a later stop group while an earlier group is waiting raises `KeyError` at `del transition_list[name]`, so the shot fails as "Error saving data after the run".
