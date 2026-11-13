# Power

An Omarchy bar widget for laptop power: live battery status, a historical ASCII charge graph, top resource consumers, and power-profile switching.

## Install

```bash
omarchy plugin add https://github.com/duketopceo/omarchy-power
omarchy plugin enable lukedaduke.power
```

## Features

- Battery percentage/state from `omarchy-battery-status`
- ASCII charge-history graph sampled by `battery_helper.py` into
  `~/.local/state/omarchy/battery_history.json`
- Top CPU/memory consumers
- Power profiles via `omarchy-powerprofiles-list` /
  `omarchy-powerprofiles-set` (persists under
  `~/.local/state/omarchy/powerprofiles`)
- Exposes a `lukedaduke.power` IPC target (open, close, toggle, togglePercentage); the `omarchy.power` target belongs to the first-party Omarchy 4.5 power widget

## External dependencies

- `power-profiles-daemon` + the stock `omarchy-*` helper commands

## Remove

```bash
omarchy plugin disable lukedaduke.power
omarchy plugin remove lukedaduke.power
```

MIT — see [LICENSE](LICENSE).
