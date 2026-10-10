# AGENTS.md — Power (`lukedaduke.power`)

> This file is the agent entry point for this repo.
> Full agent context lives at: https://github.com/duketopceo/luke-agents

Inherits from [luke-agents/AGENTS.md](https://github.com/duketopceo/luke-agents/blob/main/AGENTS.md). This file specializes; it does not replace.

## What This Repo Does

Laptop power widget for the Omarchy bar: live battery percentage and charge
state, a historical ASCII charge graph, the top CPU/memory consumers, and
power-profile switching.

## Provenance — edit in the umbrella, not here

This repo is the published subtree of
[`duketopceo/omarchy-plugins`](https://github.com/duketopceo/omarchy-plugins) at
`plugins/lukedaduke.power/`. `scripts/publish.sh` runs `git subtree split` and
fast-forwards this repo's `main`. **A commit made directly here is deleted on the
next publish.** Make the change in the umbrella, then `scripts/publish.sh power`.

## Layout

| Path | Role |
|---|---|
| `manifest.json` | Plugin contract. `kinds: ["bar-widget"]`, `barWidget.category: "System"` |
| `Panel.qml` | Bar text + dropdown. ~760 lines |
| `battery_helper.py` | Samples charge history, emits bounded JSON. **Repo root, not `bin/`** |
| `Model.js` | JS model backing the panel's lists |
| `preview.png` | Marketplace listing image |

Note the layout exception: this is the only plugin in the family whose Python
helper sits at the repo root instead of `bin/`. The panel execs it with the
absolute interpreter `/usr/bin/python3`. Do not "tidy" it into `bin/` without
updating the exec path in `Panel.qml` — it is the kind of move that silently
breaks the widget at runtime.

## Runtime Contract

- **No build step.** Nothing to compile. `manifest.json` must stay valid JSON.
- **QML cannot be checked outside Omarchy.** `Panel.qml` imports `Quickshell`,
  `Quickshell.Io`, `Quickshell.Services.UPower`, `qs.Commons`, and `qs.Ui`. The
  `Quickshell.*` and `qs.*` modules are supplied by the host shell at runtime, so
  `qmllint` will report unresolvable imports in a plain checkout. Not a bug.
- `moduleName` and `ipcTarget` must both equal the manifest `id`
  (`lukedaduke.power`).
- Power profiles go through UPower. Profile changes are system state, not local
  UI state — a failed switch must surface as an error, not a silent revert.

## Validation

There is no test suite in this repo. From the umbrella:

```bash
python3 scripts/validate-manifests.py
python3 -m pytest tests/ -q          # includes tests/test_power.py
```

Standalone:

```bash
python3 -m py_compile battery_helper.py
```

The umbrella suite requires Linux (it exercises GNU `head -z` and `/proc/meminfo`
used elsewhere in the family), so on macOS expect unrelated failures from
`test_agents.py` / `test_fan_stats.py` while `test_power.py` passes.

Real verification is on a Linux laptop with the plugin enabled: battery
percentage matches `omarchy-battery-status`, the graph accumulates samples, and
switching a power profile takes effect.

## Runtime Requirements

- `python3` (the panel execs `/usr/bin/python3`)
- `omarchy-battery-status` — battery percentage and state
- `omarchy-powerprofiles-list` — available power profiles
- UPower reachable over D-Bus (`Quickshell.Services.UPower`)

## Conventions

- Theme with `qs.Commons` `Color` / `Style` only. No hardcoded palette hex.
- Keep the helper stdlib-only. This repo has no package manifest or lockfile to
  carry a dependency.
- Keep child `PATH` pinned to a fixed safe list and exec helpers by absolute
  path, so a `PATH`-preceding shadow binary cannot execute.
- Bound the helper's stdout — the QML side parses it.
- Never edit `/usr/share/omarchy/`.
- Bump `version` in `manifest.json` when shipping a behavior change.
