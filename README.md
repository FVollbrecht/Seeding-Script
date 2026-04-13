# Squad Server Shutdown Monitor

A lightweight Windows desktop app that monitors a [Squad](https://joinsquad.com/) game server's player count via the [BattleMetrics API](https://www.battlemetrics.com/) and automatically terminates the local Squad process when a configured player threshold is reached.

---

## Features

- **Live player monitoring** — polls the BattleMetrics API at a configurable interval
- **Threshold-based process kill** — terminates `Squad.exe` / `SquadGame.exe` / `SquadGame-Win64-Shipping.exe` when the server fills up
- **Confirmation dialog** — shows a countdown prompt before acting; cancellable at any time
- **Squad process status indicator** — real-time display of whether Squad is running (refreshes every 5 s)
- **Settings dialog** — all configuration editable live via the UI, no manual config.json editing needed
- **System tray support** — minimise to tray (requires `pystray` + `Pillow`)
- **Daily log file** — `monitor_YYYY-MM-DD.log` written next to the script
- **Dark UI** — tkinter/ttk dark theme

---

## Requirements

- **Python 3.10+** (uses `match`-free syntax, but `tuple[bool, str]` type hints require 3.10+)
- **Windows** (process kill via `psutil`; tray via `pystray`)

### Python packages

```
pystray>=0.19.4
Pillow>=10.0.0
psutil>=5.9.0
```

Install with:

```bash
pip install -r requirements.txt
```

---

## Quick Start

### Option A — via `start.bat`

Double-click `start.bat`. It activates the correct directory and launches `monitor.py`.

### Option B — manually

```bash
cd "path\to\seeding-script"
pip install -r requirements.txt
python monitor.py
```

---

## Configuration

Settings are stored in `config.json` next to `monitor.py` and can be edited either directly or via the **⚙ Einstellungen** button in the UI.

| Key | Default | Description |
|-----|---------|-------------|
| `server_id` | `"1972911"` | BattleMetrics server ID (find it in the server's BattleMetrics URL) |
| `threshold` | `65` | Player count at which Squad will be terminated |
| `check_interval_seconds` | `30` | How often the API is polled (seconds) |
| `prompt_timeout_seconds` | `60` | Seconds the confirmation dialog waits before auto-confirming |
| `shutdown_delay_seconds` | `30` | Delay between confirmation and the actual process kill |

**Example `config.json`:**

```json
{
    "server_id": "1972911",
    "threshold": 65,
    "check_interval_seconds": 60,
    "prompt_timeout_seconds": 60,
    "shutdown_delay_seconds": 30
}
```

> Missing keys are automatically filled in with defaults on next launch.

---

## How It Works

```
Poll BattleMetrics API
        │
        ▼
  players >= threshold?
        │ yes
        ▼
  Show confirmation dialog (countdown)
        │ confirmed or timed out
        ▼
  Wait shutdown_delay_seconds
        │
        ▼
  Kill Squad.exe / SquadGame.exe / SquadGame-Win64-Shipping.exe
```

The kill can be cancelled at any point using the **⛔ Shutdown abbrechen** button in the main window.

---

## Project Structure

```
seeding-script/
├── monitor.py          # Main application
├── config.json         # Runtime configuration (auto-created on first run)
├── requirements.txt    # Python dependencies
├── start.bat           # Windows launcher
└── monitor_YYYY-MM-DD.log  # Daily log (auto-created)
```

---

## Finding Your BattleMetrics Server ID

1. Go to [battlemetrics.com](https://www.battlemetrics.com/)
2. Search for your server
3. The ID is the number at the end of the URL:  
   `https://www.battlemetrics.com/servers/squad/`**`1972911`**

---

## Building a Standalone Executable

Run `build.bat` to produce a single `.exe` that needs no Python installation:

```bat
build.bat
```

The script installs PyInstaller and outputs `dist\SquadMonitor.exe`.  
Copy `SquadMonitor.exe` and `config.json` to any Windows machine — no further setup required.

---

## License

MIT
