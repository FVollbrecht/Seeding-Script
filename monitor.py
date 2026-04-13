"""
Squad Server Shutdown Monitor
Überwacht Spieleranzahl via BattleMetrics-API und beendet bei Bedarf den Squad-Prozess.

Abhängigkeiten für Systemtray: pip install pystray Pillow
"""

import json
import logging
import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import pystray # pyright: ignore[reportMissingImports]
    from PIL import Image, ImageDraw # pyright: ignore[reportMissingImports]

    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

try:
    import psutil  # type: ignore[import-untyped]

    PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PSUTIL_AVAILABLE = False

# ── Pfade ───────────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_BASE_DIR, "config.json")
LOG_FILE = os.path.join(_BASE_DIR, f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log")

# ── Konstanten ──────────────────────────────────────────────────────────────────
API_BASE = "https://api.battlemetrics.com/servers/"
SERVER_PAGE_BASE = "https://www.battlemetrics.com/servers/squad/"
DEFAULT_CONFIG: dict = {
    "server_id": "1972911",
    "threshold": 65,
    "check_interval_seconds": 30,
    "prompt_timeout_seconds": 60,
    "shutdown_delay_seconds": 30,
}
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 580
SQUAD_PROCESS_NAMES = ("Squad.exe", "SquadGame.exe", "SquadGame-Win64-Shipping.exe")


def _squad_is_running() -> tuple[bool, str]:
    """Prüft ob ein Squad-Prozess läuft. Gibt (gefunden, Prozessname) zurück."""
    if not PSUTIL_AVAILABLE:
        return False, ""
    names_lower = {n.lower() for n in SQUAD_PROCESS_NAMES}
    for proc in psutil.process_iter(["name"]):
        try:
            pname = proc.info["name"] or ""
            if pname.lower() in names_lower:
                return True, pname
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False, ""


# ── Konfiguration ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, val in DEFAULT_CONFIG.items():
                data.setdefault(key, val)
            return data
        except Exception as exc:
            print(f"[config] Fehler beim Lesen: {exc} – Standardwerte werden verwendet.")
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
    return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    """Schreibt die aktuelle Konfiguration zurück in config.json."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as exc:
        print(f"[config] Fehler beim Speichern: {exc}")


# ── Logging ─────────────────────────────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("ShutdownMonitor")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


# ── Tray-Icon ───────────────────────────────────────────────────────────────────
def _make_tray_image(size: int = 64) -> "Image.Image":
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill=(59, 130, 246, 255))
    return img


# ── Haupt-App ───────────────────────────────────────────────────────────────────
class BattleMetricsMonitorApp:
    def __init__(self, root: tk.Tk, config: dict, logger: logging.Logger):
        self.root = root
        self.config = config
        self.logger = logger

        self.server_id: str = str(config["server_id"])
        self.threshold: int = int(config["threshold"])
        self.check_interval: int = int(config["check_interval_seconds"])
        self.prompt_timeout: int = int(config["prompt_timeout_seconds"])
        self.shutdown_delay: int = int(config["shutdown_delay_seconds"])

        self.api_url = f"{API_BASE}{self.server_id}"
        self.server_page_url = f"{SERVER_PAGE_BASE}{self.server_id}"

        self.current_players: int = 0
        self.max_players: int = 100
        self.server_name: str = "BattleMetrics Server"
        self.last_update: str = "–"
        self.next_check_remaining: int = self.check_interval
        self.prompted_for_current_high: bool = False
        self.fetch_in_progress: bool = False
        self.running: bool = True
        self.shutdown_pending: bool = False
        self._squad_kill_timer_id: str | None = None
        self._squad_detected: bool = False
        self._squad_process_name: str = ""
        self.tray_icon = None
        self._hiding: bool = False

        self.root.title("Squad Server Shutdown Monitor")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.minsize(720, 520)
        self.root.configure(bg="#111827")

        self._build_style()
        self._build_ui()

        self.logger.info(
            "Gestartet. Server-ID=%s, Schwelle=%d, Intervall=%ds",
            self.server_id, self.threshold, self.check_interval,
        )
        self.append_info(
            f"Server {self.server_id} | Schwelle {self.threshold} | "
            f"Intervall {self.check_interval}s | Retry {RETRY_ATTEMPTS}x"
        )

        self.update_clock()
        self.update_countdown()
        self.update_squad_status()
        self.schedule_fetch(initial=True)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if TRAY_AVAILABLE:
            self.root.bind("<Unmap>", self._on_unmap)
            self._setup_tray()
        else:
            self.append_info("Hinweis: pystray/Pillow fehlt – Systemtray nicht verfügbar.")

    # ── Systemtray ──────────────────────────────────────────────────────────────
    def _setup_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", self._tray_show),
            pystray.MenuItem("Beenden", self._tray_quit),
        )
        self.tray_icon = pystray.Icon(
            "ShutdownMonitor", _make_tray_image(), "Squad Shutdown Monitor", menu
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _tray_show(self, icon=None, item=None) -> None:
        self.root.after(0, self._restore_window)

    def _tray_quit(self, icon=None, item=None) -> None:
        self.root.after(0, self.on_close)

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event: tk.Event) -> None:
        if event.widget is self.root and self.running and not self._hiding:
            self._hiding = True
            self.root.after(0, self._hide_to_tray)

    def _hide_to_tray(self) -> None:
        self.root.withdraw()
        self._hiding = False

    # ── Stil ────────────────────────────────────────────────────────────────────
    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        labels = {
            "Card.TFrame": {"background": "#1f2937", "relief": "flat"},
            "Header.TFrame": {"background": "#0f172a", "relief": "flat"},
            "Dark.TLabel": {
                "background": "#1f2937", "foreground": "#f9fafb",
                "font": ("Segoe UI", 11),
            },
            "Muted.TLabel": {
                "background": "#1f2937", "foreground": "#9ca3af",
                "font": ("Segoe UI", 10),
            },
            "BigValue.TLabel": {
                "background": "#1f2937", "foreground": "#f9fafb",
                "font": ("Segoe UI", 26, "bold"),
            },
            "HeaderTitle.TLabel": {
                "background": "#0f172a", "foreground": "#f9fafb",
                "font": ("Segoe UI", 18, "bold"),
            },
            "HeaderSub.TLabel": {
                "background": "#0f172a", "foreground": "#9ca3af",
                "font": ("Segoe UI", 10),
            },
        }
        for name, cfg in labels.items():
            style.configure(name, **cfg)

        progress_bars = {
            "Accent.Horizontal.TProgressbar": "#3b82f6",
            "Threshold.Horizontal.TProgressbar": "#22c55e",
            "Warning.Horizontal.TProgressbar": "#f59e0b",
        }
        for name, color in progress_bars.items():
            style.configure(
                name,
                troughcolor="#0b1220", background=color,
                bordercolor="#0b1220", lightcolor=color,
                darkcolor=color, thickness=18,
            )

    # ── UI-Aufbau ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 14))
        header.pack(fill="x", padx=14, pady=(14, 10))
        ttk.Label(header, text="Squad Server Shutdown Monitor", style="HeaderTitle.TLabel").pack(anchor="w")
        self.header_sub_var = tk.StringVar(
            value=f"Server {self.server_id}  •  Schwelle: {self.threshold} Spieler  •  Intervall: {self.check_interval}s"
        )
        ttk.Label(
            header,
            textvariable=self.header_sub_var,
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        container = tk.Frame(self.root, bg="#111827")
        container.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # Zeile 1: Server & Status
        top_row = tk.Frame(container, bg="#111827")
        top_row.pack(fill="x")

        server_card = ttk.Frame(top_row, style="Card.TFrame", padding=14)
        server_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        status_card = ttk.Frame(top_row, style="Card.TFrame", padding=14)
        status_card.pack(side="left", fill="both", expand=True, padx=(6, 0))

        ttk.Label(server_card, text="Server", style="Muted.TLabel").pack(anchor="w")
        self.server_name_var = tk.StringVar(value=self.server_name)
        ttk.Label(
            server_card, textvariable=self.server_name_var,
            style="Dark.TLabel", wraplength=330,
        ).pack(anchor="w", pady=(4, 6))
        ttk.Label(
            server_card, text=self.server_page_url,
            style="Muted.TLabel", wraplength=330,
        ).pack(anchor="w")

        ttk.Label(status_card, text="Status", style="Muted.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value="Warte auf erste Daten…")
        ttk.Label(
            status_card, textvariable=self.status_var,
            style="Dark.TLabel", wraplength=330,
        ).pack(anchor="w", pady=(4, 6))
        self.last_update_var = tk.StringVar(value="Letzte Aktualisierung: –")
        ttk.Label(
            status_card, textvariable=self.last_update_var,
            style="Muted.TLabel", wraplength=330,
        ).pack(anchor="w")

        btn_frame = tk.Frame(status_card, bg="#1f2937")
        btn_frame.pack(anchor="w", pady=(10, 0))

        # Squad-Prozess-Status
        squad_row = tk.Frame(status_card, bg="#1f2937")
        squad_row.pack(anchor="w", pady=(8, 0))
        tk.Label(
            squad_row, text="Squad:",
            bg="#1f2937", fg="#9ca3af",
            font=("Segoe UI", 9),
        ).pack(side="left")
        self.squad_status_var = tk.StringVar(value="⏳ Prüfe...")
        self.squad_status_label = tk.Label(
            squad_row, textvariable=self.squad_status_var,
            bg="#1f2937", fg="#fbbf24",
            font=("Segoe UI", 9, "bold"),
        )
        self.squad_status_label.pack(side="left", padx=(6, 0))

        self.cancel_btn = tk.Button(
            btn_frame, text="⛔  Shutdown abbrechen",
            command=self.cancel_shutdown,
            bg="#b91c1c", fg="white",
            activebackground="#991b1b", activeforeground="white",
            relief="flat", padx=10, pady=6,
            font=("Segoe UI", 9, "bold"),
            state="disabled",
        )
        self.cancel_btn.pack(side="left")
        tk.Button(
            btn_frame, text="⚙  Einstellungen",
            command=self.open_settings,
            bg="#374151", fg="white",
            activebackground="#1f2937", activeforeground="white",
            relief="flat", padx=10, pady=6,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(10, 0))

        # Zeile 2: Aktuelle Spieler & Schwelle
        middle_row = tk.Frame(container, bg="#111827")
        middle_row.pack(fill="x", pady=(10, 0))

        current_card = ttk.Frame(middle_row, style="Card.TFrame", padding=14)
        current_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        threshold_card = ttk.Frame(middle_row, style="Card.TFrame", padding=14)
        threshold_card.pack(side="left", fill="both", expand=True, padx=(6, 0))

        ttk.Label(current_card, text="Aktuelle Spieler", style="Muted.TLabel").pack(anchor="w")
        self.players_var = tk.StringVar(value="0 / 100")
        ttk.Label(current_card, textvariable=self.players_var, style="BigValue.TLabel").pack(
            anchor="w", pady=(2, 6)
        )
        self.utilization_bar = ttk.Progressbar(
            current_card, style="Accent.Horizontal.TProgressbar",
            mode="determinate", maximum=100, value=0,
        )
        self.utilization_bar.pack(fill="x", pady=(4, 2))
        self.utilization_var = tk.StringVar(value="Auslastung: 0 %")
        ttk.Label(current_card, textvariable=self.utilization_var, style="Muted.TLabel").pack(anchor="w")

        ttk.Label(threshold_card, text="Schwellenwert", style="Muted.TLabel").pack(anchor="w")
        self.threshold_value_var = tk.StringVar(value=f"{self.threshold} Spieler")
        ttk.Label(
            threshold_card, textvariable=self.threshold_value_var, style="BigValue.TLabel"
        ).pack(anchor="w", pady=(2, 6))
        self.threshold_bar = ttk.Progressbar(
            threshold_card, style="Threshold.Horizontal.TProgressbar",
            mode="determinate", maximum=max(self.threshold, 1), value=0,
        )
        self.threshold_bar.pack(fill="x", pady=(4, 2))
        self.threshold_text_var = tk.StringVar(value=f"Bis zur Schwelle: {self.threshold} Spieler")
        ttk.Label(threshold_card, textvariable=self.threshold_text_var, style="Muted.TLabel").pack(anchor="w")

        # Zeile 3: Countdown & Log
        bottom_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        bottom_card.pack(fill="both", expand=True, pady=(10, 0))

        ttk.Label(bottom_card, text="Überwachung", style="Muted.TLabel").pack(anchor="w")
        self.countdown_var = tk.StringVar(value=f"Nächste Prüfung in {self.check_interval} Sekunden")
        ttk.Label(bottom_card, textvariable=self.countdown_var, style="Dark.TLabel").pack(
            anchor="w", pady=(4, 6)
        )
        self.countdown_bar = ttk.Progressbar(
            bottom_card, style="Warning.Horizontal.TProgressbar",
            mode="determinate", maximum=max(self.check_interval, 1),
            value=self.check_interval,
        )
        self.countdown_bar.pack(fill="x", pady=(0, 10))

        self.info_text = tk.Text(
            bottom_card, height=8,
            bg="#0f172a", fg="#e5e7eb", bd=0, relief="flat",
            insertbackground="#e5e7eb", font=("Consolas", 10),
            wrap="word", padx=10, pady=10,
        )
        self.info_text.pack(fill="both", expand=True)
        self.info_text.insert(
            "1.0",
            "Monitor gestartet.\n"
            f"  Konfiguration: Server {self.server_id} | Schwelle {self.threshold} | "
            f"Intervall {self.check_interval}s\n"
            f"  Retry-Logik: {RETRY_ATTEMPTS} Versuche mit je {RETRY_DELAY_SECONDS}s Pause\n"
            + ("  Systemtray verfügbar – Minimieren blendet das Fenster in den Tray aus.\n"
               if TRAY_AVAILABLE else ""),
        )
        self.info_text.config(state="disabled")

    # ── Hilfsmethoden ───────────────────────────────────────────────────────────
    def append_info(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}\n"
        self.info_text.config(state="normal")
        self.info_text.insert("end", line)
        line_count = int(float(self.info_text.index("end-1c").split(".")[0]))
        if line_count > 120:
            self.info_text.delete("1.0", "15.0")
        self.info_text.see("end")
        self.info_text.config(state="disabled")
        self.logger.info(text)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_shutdown_pending(self, pending: bool) -> None:
        self.shutdown_pending = pending
        self.cancel_btn.config(state="normal" if pending else "disabled")

    def cancel_shutdown(self) -> None:
        timer_id = getattr(self, "_squad_kill_timer_id", None)
        if timer_id is not None:
            self.root.after_cancel(timer_id)
            self._squad_kill_timer_id = None
        self.append_info("Beenden von Squad abgebrochen.")
        self.set_status("Abgebrochen")
        self.logger.warning("Squad-Beenden wurde durch Benutzer abgebrochen.")
        self._set_shutdown_pending(False)

    def update_squad_status(self) -> None:
        def check():
            found, pname = _squad_is_running()
            self.root.after(0, lambda f=found, n=pname: self._apply_squad_status(f, n))

        threading.Thread(target=check, daemon=True).start()
        if self.running:
            self.root.after(5000, self.update_squad_status)

    def _apply_squad_status(self, found: bool, pname: str) -> None:
        self._squad_detected = found
        self._squad_process_name = pname
        if found:
            self.squad_status_var.set(f"✅ Aktiv ({pname})")
            self.squad_status_label.config(fg="#4ade80")
        else:
            self.squad_status_var.set("❌ Nicht erkannt")
            self.squad_status_label.config(fg="#f87171")

    def update_clock(self) -> None:
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.last_update_var.set(f"Letzte Aktualisierung: {self.last_update}  •  Lokal: {now}")
        if self.running:
            self.root.after(1000, self.update_clock)

    def update_countdown(self) -> None:
        self.countdown_var.set(f"Nächste Prüfung in {self.next_check_remaining} Sekunden")
        self.countdown_bar["maximum"] = max(self.check_interval, 1)
        self.countdown_bar["value"] = self.next_check_remaining
        if self.next_check_remaining > 0:
            self.next_check_remaining -= 1
        if self.running:
            self.root.after(1000, self.update_countdown)

    # ── Datenabruf ──────────────────────────────────────────────────────────────
    def schedule_fetch(self, initial: bool = False) -> None:
        if not self.running:
            return
        if not self.fetch_in_progress:
            self.fetch_in_progress = True
            self.set_status("Daten werden geladen…")
            threading.Thread(target=self._fetch_with_retry, daemon=True).start()
        delay_ms = 500 if initial else self.check_interval * 1000
        self.root.after(delay_ms, lambda: self.schedule_fetch(initial=False))

    def _fetch_with_retry(self) -> None:
        last_error = "Unbekannter Fehler"
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                req = Request(
                    self.api_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (ShutdownMonitor/2.0)",
                        "Accept": "application/json",
                    },
                )
                with urlopen(req, timeout=20) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))

                attrs = raw.get("data", {}).get("attributes", {})
                players = attrs.get("players")
                max_players = attrs.get("maxPlayers")
                server_name = attrs.get("name") or "BattleMetrics Server"

                if not isinstance(players, int) or not isinstance(max_players, int):
                    raise ValueError("Spielerzahl nicht im erwarteten Format.")

                self.root.after(
                    0,
                    lambda p=players, m=max_players, n=server_name: self._apply_data(n, p, m),
                )
                self.fetch_in_progress = False
                return

            except HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.reason}"
            except URLError as exc:
                last_error = f"Verbindungsfehler: {exc.reason}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < RETRY_ATTEMPTS:
                msg = f"API-Fehler (Versuch {attempt}/{RETRY_ATTEMPTS}): {last_error} – Retry in {RETRY_DELAY_SECONDS}s"
                self.root.after(0, lambda m=msg: self.append_info(m))
                time.sleep(RETRY_DELAY_SECONDS)

        final_msg = f"API nicht erreichbar nach {RETRY_ATTEMPTS} Versuchen: {last_error}"
        self.root.after(0, lambda m=final_msg: self._handle_fetch_error(m))
        self.fetch_in_progress = False

    def _handle_fetch_error(self, text: str) -> None:
        self.last_update = "Fehler"
        self.set_status("Verbindungsfehler – Nächster Versuch läuft…")
        self.append_info(text)
        self.logger.error(text)
        self.next_check_remaining = self.check_interval

    def _apply_data(self, server_name: str, players: int, max_players: int) -> None:
        self.server_name = server_name
        self.current_players = players
        self.max_players = max_players

        self.server_name_var.set(server_name)
        self.players_var.set(f"{players} / {max_players}")

        self.utilization_bar["maximum"] = max(max_players, 1)
        self.utilization_bar["value"] = min(players, max_players)
        utilization = round((players / max_players) * 100) if max_players > 0 else 0
        self.utilization_var.set(f"Auslastung: {utilization} %")

        self.threshold_bar["maximum"] = max(self.threshold, 1)
        self.threshold_bar["value"] = min(players, self.threshold)
        if players < self.threshold:
            self.threshold_text_var.set(f"Bis zur Schwelle: {self.threshold - players} Spieler")
        elif players == self.threshold:
            self.threshold_text_var.set("Schwelle genau erreicht")
        else:
            self.threshold_text_var.set(f"Schwelle überschritten um {players - self.threshold}")

        self.last_update = datetime.now().strftime("%H:%M:%S")
        self.set_status(f"Online  •  {players}/{max_players} Spieler  •  {utilization} %")
        self.next_check_remaining = self.check_interval
        self.append_info(f"Spielerstand: {players}/{max_players}  (Auslastung {utilization} %)")

        if players >= self.threshold:
            if not self.prompted_for_current_high:
                self.prompted_for_current_high = True
                self.root.after(200, lambda: self._trigger_shutdown_prompt(players, max_players))
        else:
            if self.prompted_for_current_high:
                self.append_info(
                    "Spielerzahl wieder unter Schwelle – bei nächster Überschreitung wird erneut gefragt."
                )
            self.prompted_for_current_high = False

    # ── Shutdown-Logik ──────────────────────────────────────────────────────────
    def _trigger_shutdown_prompt(self, players: int, max_players: int) -> None:
        self.append_info(f"Schwelle erreicht: {players}/{max_players}. Dialog wird geöffnet…")
        self.set_status("Schwelle erreicht  •  Warte auf Bestätigung…")
        self._restore_window()

        prompt = ShutdownPrompt(
            master=self.root,
            players=players,
            max_players=max_players,
            timeout_seconds=self.prompt_timeout,
            threshold=self.threshold,
        )
        result = prompt.show()

        if result in (True, None):
            reason = "Benutzer bestätigt" if result is True else "Timeout"
            self.append_info(f"Shutdown gestartet ({reason}). Spieler: {players}")
            self.logger.warning("Shutdown ausgelöst. Spieler: %d, Grund: %s", players, reason)
            self.set_status("Shutdown wird gestartet…")
            self._execute_shutdown(players)
        else:
            self.append_info("Benutzer hat Shutdown abgelehnt. Überwachung läuft weiter.")
            self.logger.info("Shutdown abgelehnt. Spieler: %d", players)
            self.set_status("Überwachung läuft")

    def _execute_shutdown(self, player_count: int) -> None:
        self.append_info(
            f"Squad wird in {self.shutdown_delay} Sekunden beendet "
            f"({player_count} Spieler – Schwelle erreicht)."
        )
        # Messagebox zuerst anzeigen – erst danach den Timer starten,
        # damit der Countdown nicht während der modalen Box abläuft.
        messagebox.showinfo(
            "Squad wird beendet",
            f"Squad.exe wird in {self.shutdown_delay} Sekunden beendet.\n\n"
            f"Zum Abbrechen: Schaltfläche '⛔ Shutdown abbrechen' klicken.",
        )
        self._squad_kill_timer_id = self.root.after(
            self.shutdown_delay * 1000, self._kill_squad_and_shutdown
        )
        self._set_shutdown_pending(True)

    def _kill_squad_and_shutdown(self) -> None:
        self._squad_kill_timer_id = None
        if not PSUTIL_AVAILABLE:
            self.append_info("psutil nicht verfügbar – Squad kann nicht beendet werden.")
            self._set_shutdown_pending(False)
            return
        killed = False
        names_lower = {n.lower() for n in SQUAD_PROCESS_NAMES}
        for proc in psutil.process_iter(["name"]):
            pname = ""
            try:
                pname = proc.info["name"] or ""
                if pname.lower() in names_lower:
                    proc.kill()
                    self.append_info(f"{pname} wurde beendet.")
                    self.logger.info("%s beendet.", pname)
                    killed = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception as exc:
                self.append_info(f"Fehler beim Beenden von {pname or 'unbekannt'}: {exc}")
                self.logger.error("Fehler beim Beenden von %s: %s", pname or "unbekannt", exc)
        if not killed:
            self.append_info("Kein Squad-Prozess gefunden – läuft möglicherweise nicht.")
            self.logger.warning("Kein Squad-Prozess gefunden.")
        self._set_shutdown_pending(False)

    # ── Einstellungen ───────────────────────────────────────────────────────────
    def open_settings(self) -> None:
        dialog = SettingsDialog(master=self.root, config=self.config)
        new_config = dialog.show()
        if new_config is not None:
            self._apply_settings(new_config)

    def _apply_settings(self, new_config: dict) -> None:
        old_id = self.server_id
        self.config.update(new_config)
        save_config(self.config)

        self.server_id = str(new_config["server_id"])
        self.threshold = int(new_config["threshold"])
        self.check_interval = int(new_config["check_interval_seconds"])
        self.prompt_timeout = int(new_config["prompt_timeout_seconds"])
        self.shutdown_delay = int(new_config["shutdown_delay_seconds"])

        if self.server_id != old_id:
            self.api_url = f"{API_BASE}{self.server_id}"
            self.server_page_url = f"{SERVER_PAGE_BASE}{self.server_id}"
            self.prompted_for_current_high = False

        self.header_sub_var.set(
            f"Server {self.server_id}  •  Schwelle: {self.threshold} Spieler  •  Intervall: {self.check_interval}s"
        )
        self.threshold_value_var.set(f"{self.threshold} Spieler")
        self.threshold_bar["maximum"] = max(self.threshold, 1)
        self.threshold_bar["value"] = min(self.current_players, self.threshold)
        if self.current_players < self.threshold:
            self.threshold_text_var.set(f"Bis zur Schwelle: {self.threshold - self.current_players} Spieler")
        elif self.current_players == self.threshold:
            self.threshold_text_var.set("Schwelle genau erreicht")
        else:
            self.threshold_text_var.set(f"Schwelle überschritten um {self.current_players - self.threshold}")

        self.next_check_remaining = self.check_interval
        self.append_info(
            f"Einstellungen gespeichert: Server {self.server_id} | Schwelle {self.threshold} | "
            f"Intervall {self.check_interval}s | Timeout {self.prompt_timeout}s | "
            f"Shutdown-Delay {self.shutdown_delay}s"
        )

    # ── Beenden ─────────────────────────────────────────────────────────────────
    def on_close(self) -> None:
        self.running = False
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.logger.info("Anwendung beendet.")
        self.root.destroy()


# ── Einstellungs-Dialog ─────────────────────────────────────────────────────────
class SettingsDialog:
    FIELDS = [
        ("server_id",              "Server-ID",                    "str",  "BattleMetrics Server-ID (z. B. 1972911)"),
        ("threshold",              "Schwellenwert (Spieler)",      "int",  "Shutdown wird ausgelöst ab dieser Spieleranzahl"),
        ("check_interval_seconds", "Prüfintervall (Sekunden)",     "int",  "Wie oft die API abgefragt wird"),
        ("prompt_timeout_seconds", "Dialog-Timeout (Sekunden)",    "int",  "Automatischer Shutdown nach dieser Zeit ohne Reaktion"),
        ("shutdown_delay_seconds", "Shutdown-Verzögerung (Sek.)",  "int",  "Verzögerung zwischen Befehl und tatsächlichem Shutdown"),
    ]

    def __init__(self, master: tk.Tk, config: dict):
        self.master = master
        self.config = config
        self.result: dict | None = None
        self.window: tk.Toplevel | None = None
        self._entries: dict[str, tk.StringVar] = {}

    def show(self) -> dict | None:
        self.window = tk.Toplevel(self.master)
        self.window.title("Einstellungen")
        self.window.configure(bg="#111827")
        self.window.resizable(False, False)
        self.window.grab_set()
        self.window.attributes("-topmost", True)

        wrap = tk.Frame(self.window, bg="#111827", padx=24, pady=20)
        wrap.pack(fill="both", expand=True)

        tk.Label(
            wrap, text="⚙  Einstellungen",
            bg="#111827", fg="#f9fafb", font=("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        for idx, (key, label, typ, hint) in enumerate(self.FIELDS):
            base_row = 1 + idx * 2
            tk.Label(
                wrap, text=label + ":",
                bg="#111827", fg="#d1d5db",
                font=("Segoe UI", 10, "bold"), anchor="w", width=30,
            ).grid(row=base_row, column=0, sticky="nw", padx=(0, 12), pady=(6, 0))

            var = tk.StringVar(value=str(self.config.get(key, "")))
            self._entries[key] = var
            tk.Entry(
                wrap, textvariable=var,
                bg="#1f2937", fg="#f9fafb",
                insertbackground="#f9fafb",
                relief="flat", font=("Consolas", 10),
                width=24, bd=4,
                highlightbackground="#374151",
                highlightcolor="#3b82f6",
                highlightthickness=1,
            ).grid(row=base_row, column=1, sticky="ew", pady=(6, 0))

            tk.Label(
                wrap, text=hint,
                bg="#111827", fg="#6b7280",
                font=("Segoe UI", 8), anchor="w",
            ).grid(row=base_row + 1, column=1, sticky="w", pady=(1, 0))

        sep_row = 1 + len(self.FIELDS) * 2
        tk.Frame(wrap, bg="#374151", height=1).grid(
            row=sep_row, column=0, columnspan=2, sticky="ew", pady=12
        )

        self.error_label = tk.Label(
            wrap, text="", bg="#111827", fg="#f87171",
            font=("Segoe UI", 9), anchor="w",
        )
        self.error_label.grid(row=sep_row + 1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        btn_row = tk.Frame(wrap, bg="#111827")
        btn_row.grid(row=sep_row + 2, column=0, columnspan=2, sticky="e")

        tk.Button(
            btn_row, text="Abbrechen", command=self._cancel,
            bg="#374151", fg="white", activebackground="#1f2937",
            relief="flat", padx=14, pady=7, font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 10))
        tk.Button(
            btn_row, text="✔  Speichern", command=self._save,
            bg="#16a34a", fg="white", activebackground="#15803d",
            relief="flat", padx=14, pady=7, font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        wrap.columnconfigure(1, weight=1)
        self._center()
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        self.master.wait_window(self.window)
        return self.result

    def _center(self) -> None:
        assert self.window is not None
        self.window.update_idletasks()
        w = self.window.winfo_width()
        h = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() - w) // 2
        y = (self.window.winfo_screenheight() - h) // 2
        self.window.geometry(f"{w}x{h}+{x}+{y}")

    def _save(self) -> None:
        assert self.window is not None
        new_cfg: dict = {}
        for key, label, typ, _ in self.FIELDS:
            raw = self._entries[key].get().strip()
            if not raw:
                self.error_label.config(text=f"'{label}' darf nicht leer sein.")
                return
            if typ == "int":
                if not raw.isdigit() or int(raw) < 1:
                    self.error_label.config(text=f"'{label}' muss eine positive ganze Zahl sein.")
                    return
                new_cfg[key] = int(raw)
            else:
                new_cfg[key] = raw
        self.result = new_cfg
        self.window.destroy()

    def _cancel(self) -> None:
        assert self.window is not None
        self.result = None
        self.window.destroy()


# ── Shutdown-Dialog ──────────────────────────────────────────────────────────────
class ShutdownPrompt:
    def __init__(
        self,
        master: tk.Tk,
        players: int,
        max_players: int,
        timeout_seconds: int,
        threshold: int,
    ):
        self.master = master
        self.players = players
        self.max_players = max_players
        self.timeout_seconds = timeout_seconds
        self.threshold = threshold
        self.remaining = timeout_seconds
        self.result: bool | None = None
        self.window: tk.Toplevel | None = None

    def show(self) -> bool | None:
        self.window = tk.Toplevel(self.master)
        self.window.title("Shutdown bestätigen")
        self.window.configure(bg="#111827")
        self.window.attributes("-topmost", True)
        self.window.grab_set()
        self.window.resizable(False, False)

        wrap = tk.Frame(self.window, bg="#111827", padx=22, pady=20)
        wrap.pack(fill="both", expand=True)

        tk.Label(
            wrap, text="⚠  Schwelle erreicht",
            bg="#111827", fg="#f9fafb", font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w")

        tk.Label(
            wrap,
            text=(
                f"Der Server hat {self.players}/{self.max_players} Spieler.\n"
                f"(Konfigurierter Schwellenwert: {self.threshold})\n\n"
                "Soll Squad.exe jetzt beendet werden?"
            ),
            bg="#111827", fg="#e5e7eb",
            font=("Segoe UI", 11), justify="left",
        ).pack(anchor="w", pady=(10, 12))

        self.countdown_label = tk.Label(
            wrap,
            text=f"Automatisches Beenden in {self.remaining} Sekunden.",
            bg="#111827", fg="#fbbf24", font=("Segoe UI", 10, "bold"),
        )
        self.countdown_label.pack(anchor="w", pady=(0, 6))

        style = ttk.Style()
        style.configure(
            "Prompt.Horizontal.TProgressbar",
            troughcolor="#0b1220", background="#ef4444",
            bordercolor="#0b1220", lightcolor="#ef4444",
            darkcolor="#ef4444", thickness=16,
        )
        self.bar = ttk.Progressbar(
            wrap, style="Prompt.Horizontal.TProgressbar",
            mode="determinate",
            maximum=max(self.timeout_seconds, 1),
            value=self.timeout_seconds,
            length=420,
        )
        self.bar.pack(fill="x", pady=(0, 16))

        btn_row = tk.Frame(wrap, bg="#111827")
        btn_row.pack(fill="x")
        tk.Button(
            btn_row, text="Ja, Squad beenden", command=self._yes,
            bg="#dc2626", fg="white", activebackground="#b91c1c",
            relief="flat", padx=14, pady=8, font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        tk.Button(
            btn_row, text="Nein, weiterlaufen", command=self._no,
            bg="#374151", fg="white", activebackground="#1f2937",
            relief="flat", padx=14, pady=8, font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(12, 0))

        self.window.protocol("WM_DELETE_WINDOW", self._no)
        self._center()
        self._tick()
        self.master.wait_window(self.window)
        return self.result

    def _center(self) -> None:
        assert self.window is not None
        self.window.update_idletasks()
        w = self.window.winfo_width()
        h = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() - w) // 2
        y = (self.window.winfo_screenheight() - h) // 2
        self.window.geometry(f"{w}x{h}+{x}+{y}")

    def _tick(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            return
        self.countdown_label.config(
            text=f"Automatisches Beenden in {self.remaining} Sekunden."
        )
        self.bar["value"] = self.remaining
        if self.remaining <= 0:
            self.result = None
            self.window.destroy()
            return
        self.remaining -= 1
        self.window.after(1000, self._tick)

    def _yes(self) -> None:
        assert self.window is not None
        self.result = True
        self.window.destroy()

    def _no(self) -> None:
        assert self.window is not None
        self.result = False
        self.window.destroy()


# ── Einstiegspunkt ───────────────────────────────────────────────────────────────
def main() -> None:
    config = load_config()
    logger = setup_logger()
    root = tk.Tk()
    BattleMetricsMonitorApp(root, config, logger)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
