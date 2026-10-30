"""Trading Bot window. Stage-1 controls only; buy execution stays unavailable."""

from __future__ import annotations

from datetime import datetime
import logging
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from src.control.cli import format_help
from src.desktop.calendar_panel import SessionCalendarPanel, completion_percent, session_activity_line
from src.control.instance import AlreadyRunning
from src.control.permissions import LaunchOptions, resolve_buy_permission
from src.control.service import ControlService
from src.control.status import current_status
from src.data.provider import MarketDataUnavailable
from src.data.scan_scheduler import SCAN_FINISHED, run_download_scheduler
from src.data.session_download import (
    CALENDAR_LOOKBACK_DAYS,
    NoTrackedSymbols,
    calendar_session_dates,
    delete_sessions,
    download_calendar_days,
    download_scan_window,
    ny_today,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
API_PORT = 8000
WEB_UI_PORT = 5173
WEB_UI_URL = f"http://127.0.0.1:{WEB_UI_PORT}"


def buy_checkbox_state(enable_scheduler: bool, enable_watch_engine: bool, requested: bool) -> tuple[bool, str]:
    """``yes_buy`` can be requested only while both services are selected."""
    allowed = bool(enable_scheduler) and bool(enable_watch_engine)
    return bool(requested) and allowed, "normal" if allowed else "disabled"


def universe_activity_line(enable_scheduler: bool, enable_watch_engine: bool, *, repeat: bool) -> str:
    """One dialog line for a Universe start. Names only the services that are on."""
    action = "new scan universe" if repeat else "start universe"
    if enable_scheduler and enable_watch_engine:
        return f"{action} with scheduler and watch engine"
    if enable_scheduler:
        return f"{action} with scheduler"
    if enable_watch_engine:
        return f"{action} with watch engine"
    return action


def web_ui_is_up(port: int = WEB_UI_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def launch_web_ui(
    *,
    root: Path = ROOT,
    port_open=None,
    api_port_open=None,
    popen=None,
    open_browser=None,
    python: str | None = None,
    vite_command=None,
) -> str:
    """Open the React Universe page. Starts the API and Vite when they are down."""
    opener = open_browser or webbrowser.open
    spawn = popen or subprocess.Popen
    ui_up = (port_open or web_ui_is_up)()
    api_up = (api_port_open or (lambda: web_ui_is_up(API_PORT)))()
    started = False
    if not api_up:
        _spawn(
            spawn,
            [
                python or _console_python(),
                "-m",
                "uvicorn",
                "src.ui.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
            ],
            root,
        )
        started = True
    if not ui_up:
        _spawn(spawn, vite_command or _vite_command(root), root / "web")
        started = True
    if started:
        return "started"
    opener(WEB_UI_URL)
    return "opened"


def _vite_command(root: Path) -> list[str]:
    web = root / "web"
    name = "vite.cmd" if sys.platform == "win32" else "vite"
    local = web / "node_modules" / ".bin" / name
    if local.exists():
        return [str(local), "--host", "127.0.0.1", "--port", str(WEB_UI_PORT)]
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    return [npm, "run", "dev"]


def _spawn(popen, command: list[str], cwd: Path) -> None:
    kwargs = {"cwd": str(cwd)}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    popen(command, **kwargs)


def _default_sixty_delete(dates):
    """Remove stored bars for the cash sessions in the last 60 calendar days."""
    from src.data.bar_store import BarStore

    return delete_sessions(list(dates), store=BarStore())


def _default_scheduler_symbols():
    from scripts.scheduler import resolve_symbols

    return resolve_symbols()


def _default_slot_download(slot, symbols, should_stop=None, wait_if_paused=None):
    """One scheduled window for the tracked universe. Does not score it."""
    import pandas as pd

    from src.data.bar_store import BarStore
    from src.data.provider import AlpacaMarketDataProvider

    return download_scan_window(
        slot,
        provider=AlpacaMarketDataProvider(),
        store=BarStore(),
        symbols=symbols,
        clock=pd.Timestamp.now(tz="UTC"),
        should_stop=should_stop,
        wait_if_paused=wait_if_paused,
    )


def _default_sixty_download(dates, on_progress=None, should_stop=None, wait_if_paused=None):
    """Cash sessions for the supplied days. Does not score or arm the scheduler."""
    import pandas as pd

    from scripts.scheduler import resolve_symbols
    from src.data.bar_store import BarStore
    from src.data.provider import AlpacaMarketDataProvider

    return download_calendar_days(
        dates,
        provider=AlpacaMarketDataProvider(),
        store=BarStore(),
        symbols=resolve_symbols(),
        clock=pd.Timestamp.now(tz="UTC"),
        on_progress=on_progress,
        should_stop=should_stop,
        wait_if_paused=wait_if_paused,
    )


def _console_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(executable)


class RoundIconButton(tk.Canvas):
    """Circular control. ``icon`` is ``stop`` (square) or ``pause`` (two bars)."""

    def __init__(self, master, icon: str, command, size: int = 28):
        bg = _panel_background(master)
        super().__init__(
            master,
            width=size,
            height=size,
            highlightthickness=0,
            bd=0,
            bg=bg,
            cursor="arrow",
        )
        self.icon = icon
        self._command = command
        self._size = size
        self._state = "normal"
        self._paused = False
        self.bind("<Button-1>", self._click)
        self._draw()

    def resize(self, size: int) -> None:
        size = max(22, int(size))
        if size == self._size:
            return
        self._size = size
        super().configure(width=size, height=size)
        self._draw()

    def configure(self, cnf=None, **kwargs):
        if cnf is None and not kwargs:
            return super().configure()
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
        elif cnf is not None:
            return super().configure(cnf, **kwargs)
        state = kwargs.pop("state", None)
        if kwargs:
            super().configure(**kwargs)
        if state is not None:
            self._state = state
            if state == "disabled":
                self._paused = False
            self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        return super().cget(key)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused) and self._state != "disabled"
        self._draw()

    def _click(self, _event) -> None:
        if self._state == "disabled":
            return
        self._command()

    def _draw(self) -> None:
        self.delete("all")
        size = self._size
        if self._state == "disabled":
            fill = "#2a3544"
            mark = "#6d7c8d"
        elif self.icon == "pause" and self._paused:
            fill = "#c47d12"
            mark = "#ffffff"
        elif self.icon == "stop":
            fill = "#c0392b"
            mark = "#ffffff"
        else:
            fill = "#1d4f91"
            mark = "#ffffff"
        self.create_oval(1, 1, size - 1, size - 1, fill=fill, outline=fill)
        if self.icon == "stop":
            inset = int(size * 0.34)
            self.create_rectangle(inset, inset, size - inset, size - inset, fill=mark, outline=mark)
        else:
            bar_w = max(3, size // 9)
            gap = max(3, size // 10)
            top = int(size * 0.30)
            bottom = size - top
            center = size // 2
            self.create_rectangle(center - gap - bar_w, top, center - gap, bottom, fill=mark, outline=mark)
            self.create_rectangle(center + gap, top, center + gap + bar_w, bottom, fill=mark, outline=mark)
        super().configure(cursor="hand2" if self._state == "normal" else "arrow")


SURFACE = "#0d141c"
PANEL = "#141c27"
INK = "#d5deea"
ACCENT = "#7dcea0"


def apply_trading_theme(root: tk.Misc) -> None:
    """Dark market-terminal surface for the desktop window."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=SURFACE, foreground=INK, fieldbackground=PANEL)
    style.configure("TFrame", background=SURFACE)
    style.configure("TLabel", background=SURFACE, foreground=INK)
    style.configure("TLabelframe", background=SURFACE, foreground=ACCENT, bordercolor="#243044")
    style.configure("TLabelframe.Label", background=SURFACE, foreground=ACCENT)
    style.configure("TButton", background=PANEL, foreground=INK, bordercolor="#243044", focuscolor=PANEL)
    style.map(
        "TButton",
        background=[("active", "#1e2c3d"), ("disabled", "#101820")],
        foreground=[("disabled", "#6d7c8d")],
    )
    style.configure("TCheckbutton", background=SURFACE, foreground=INK)
    style.map("TCheckbutton", background=[("active", SURFACE)])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#101820", bordercolor="#101820")
    root.configure(background=SURFACE)


def _panel_background(widget) -> str:
    color = ttk.Style(widget).lookup("TFrame", "background")
    return color or SURFACE


class TradingBotApp:
    def __init__(self, root: tk.Tk | None = None, service_factory=None, status_reader=None, web_opener=None, calendar_today=None, load_month=None, download_sessions=None, download_sixty=None, delete_sixty=None, slot_download=None, scheduler_symbols=None, scheduler=None, scheduler_clock=None):
        self.root = root or tk.Tk()
        self.root.title("Trading Bot")
        self.service_factory = service_factory or ControlService
        self.status_reader = status_reader or current_status
        self.web_opener = web_opener or launch_web_ui
        self._calendar_today = calendar_today
        self._load_month = load_month
        self._download_sessions = download_sessions
        self._download_sixty = download_sixty or _default_sixty_download
        self._delete_sixty = delete_sixty or _default_sixty_delete
        self._slot_download = slot_download or _default_slot_download
        self._scheduler_symbols = scheduler_symbols or _default_scheduler_symbols
        self._scheduler = scheduler or run_download_scheduler
        self._scheduler_clock = scheduler_clock
        self._mode = None
        self._stop_download = threading.Event()
        self._pause_download = threading.Event()
        self.service = None
        self._active_options = None
        self._scans_started = 0
        self._web_process = None
        self._web_starting = False
        self.worker = None
        self._closed = threading.Event()
        self.help_window = None
        self.status_window = None
        self.help_text = None
        self.status_text = None
        self.scheduler_var = tk.BooleanVar(value=False)
        self.watch_var = tk.BooleanVar(value=False)
        self.buy_var = tk.BooleanVar(value=False)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def run(self) -> None:
        self.root.mainloop()

    def _build(self) -> None:
        apply_trading_theme(self.root)
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.calendar = SessionCalendarPanel(
            frame,
            today=self._calendar_today,
            load_month=self._load_month,
            download=self._download_sessions,
            on_busy=self._download_busy,
            on_sixty_download=self.download_sixty_days,
            on_sixty_delete=self.delete_sixty_days,
            on_log=self._log,
        )
        self.calendar.set_download_controls(self._download_should_stop, self._wait_if_download_paused)
        self.calendar.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        frame.rowconfigure(0, weight=2)
        self.start_button = self.calendar.sixty_download_button
        self.sixty_delete_button = self.calendar.sixty_delete_button
        self._progress_button = None

        self.pause_button = RoundIconButton(self.calendar.controls, "pause", self.pause_universe)
        self.pause_button.grid(row=0, column=0, padx=(0, 6))
        self.stop_button = RoundIconButton(self.calendar.controls, "stop", self.stop_universe)
        self.stop_button.grid(row=0, column=1)
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")

        self.scheduler_check = ttk.Checkbutton(
            frame,
            text="Enable Scheduler",
            variable=self.scheduler_var,
            command=self._sync_buy,
        )
        self.scheduler_check.grid(row=1, column=0, sticky="w")
        self.watch_check = ttk.Checkbutton(
            frame,
            text="Enable Watch Engine",
            variable=self.watch_var,
            command=self._sync_buy,
        )
        self.watch_check.grid(row=2, column=0, sticky="w")
        self.buy_check = ttk.Checkbutton(
            frame,
            text="Yes, Allow Buys",
            variable=self.buy_var,
            command=self._sync_buy,
        )
        self.buy_check.grid(row=3, column=0, sticky="w", pady=(0, 8))
        self._sync_buy()

        self.note = ttk.Label(
            frame,
            text="Buy execution is not implemented. Yes, Allow Buys only records approval.",
            wraplength=420,
        )
        self.note.grid(row=4, column=0, sticky="ew", pady=(0, 12))

        scheduler_box = ttk.LabelFrame(frame, text="Scheduler", padding=8)
        scheduler_box.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        scheduler_box.columnconfigure(0, weight=1)
        self.scheduler_box = scheduler_box
        self.scheduler_button = ttk.Button(
            scheduler_box,
            text="Εκκίνηση scheduler",
            command=self.start_scheduler,
        )
        self.scheduler_button.grid(row=0, column=0, sticky="ew")

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, sticky="ew", pady=(0, 12))
        ttk.Button(buttons, text="HELP", command=self.show_help).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="STATUS", command=self.show_status).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="WEB UI", command=self.open_web_ui).grid(row=0, column=2)

        dialog = ttk.LabelFrame(frame, text="Universe", padding=8)
        dialog.grid(row=7, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        frame.rowconfigure(7, weight=1)
        self.activity = ScrolledText(
            dialog,
            height=8,
            width=64,
            wrap="word",
            state="disabled",
            bg="#0a1016",
            fg="#c8f7d4",
            insertbackground="#c8f7d4",
            selectbackground="#1d4f91",
            selectforeground="#ffffff",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#243044",
            highlightcolor="#243044",
        )
        self.activity.grid(row=0, column=0, sticky="nsew")
        self._layout_base = None
        self._icon_size = 28
        self.root.bind("<Configure>", self._on_resize)

    def _on_resize(self, event) -> None:
        if event.widget is not self.root or event.width < 200 or event.height < 200:
            return
        if self._layout_base is None:
            self._layout_base = (event.width, event.height)
            return
        base_width, base_height = self._layout_base
        scale = min(event.width / base_width, event.height / base_height)
        scale = max(0.85, min(scale, 2.4))
        icon = max(22, min(72, int(round(28 * scale))))
        if icon != self._icon_size:
            self._icon_size = icon
            self.pause_button.resize(icon)
            self.stop_button.resize(icon)
        wrap = max(280, event.width - 64)
        self.calendar.apply_scale(scale, wrap)
        self.note.configure(wraplength=wrap)

    def download_sixty_days(self) -> None:
        """Download cash sessions from the last 60 calendar days. Does not scan."""
        if (self.worker is not None and self.worker.is_alive()) or self.calendar._running():
            self._append("Trading Bot is already running")
            return
        today = self._calendar_today or ny_today()
        dates = calendar_session_dates(today, CALENDAR_LOOKBACK_DAYS)
        self._mode = "sixty"
        self._stop_download.clear()
        self._pause_download.clear()
        self._log(session_activity_line("download", dates))
        self._arm_progress(self.start_button)
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_sixty, args=(dates,), name="sixty-day-download", daemon=False)
        self.worker.start()

    def delete_sixty_days(self) -> None:
        """Delete stored bars for the cash sessions in the last 60 calendar days."""
        if (self.worker is not None and self.worker.is_alive()) or self.calendar._running():
            self._append("Trading Bot is already running")
            return
        today = self._calendar_today or ny_today()
        dates = calendar_session_dates(today, CALENDAR_LOOKBACK_DAYS)
        self._mode = "delete-sixty"
        self._log(session_activity_line("delete", dates))
        self._arm_progress(self.sixty_delete_button)
        self._set_running(True, controls=False)
        self.worker = threading.Thread(target=self._run_sixty_delete, args=(dates,), name="sixty-day-delete", daemon=False)
        self.worker.start()

    def start_universe(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._append("Trading Bot is already running")
            return
        try:
            permission = resolve_buy_permission(
                enable_scheduler=self.scheduler_var.get(),
                enable_watch_engine=self.watch_var.get(),
                yes_buy=self.buy_var.get(),
            )
        except Exception as exc:
            self.buy_var.set(False)
            self._sync_buy()
            self._append(str(exc))
            return
        options = LaunchOptions(
            enable_scheduler=self.scheduler_var.get(),
            enable_watch_engine=self.watch_var.get(),
            buy_permission=permission,
        )
        self._active_options = options
        self._mode = "universe"
        self._scans_started += 1
        self._log(universe_activity_line(
            options.enable_scheduler,
            options.enable_watch_engine,
            repeat=self._scans_started > 1,
        ))
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_service, args=(options,), name="trading-bot", daemon=False)
        self.worker.start()

    def start_scheduler(self) -> None:
        """Arm the session clock. Downloads run when each scan time arrives."""
        if (self.worker is not None and self.worker.is_alive()) or self.calendar._running():
            self._log("ο scheduler τρέχει ήδη" if self._mode == "scheduler" else "τρέχει ήδη λήψη")
            return
        self._mode = "scheduler"
        self._stop_download.clear()
        self._pause_download.clear()
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_scheduler, name="bar-scheduler", daemon=True)
        self.worker.start()

    def stop_universe(self) -> None:
        """Stop the running Universe scan and its scheduler. The window stays open."""
        if self._mode in ("sixty", "sessions"):
            self._stop_download.set()
            self._pause_download.clear()
            self.stop_button.configure(state="disabled")
            self.pause_button.configure(state="disabled")
            self._log("η λήψη σταμάτησε")
            return
        if self._mode == "scheduler":
            if self.worker is None or not self.worker.is_alive():
                return
            self._stop_download.set()
            self._pause_download.clear()
            self.stop_button.configure(state="disabled")
            self.pause_button.configure(state="disabled")
            self._log("ο scheduler σταμάτησε")
            return
        service = self.service
        if service is None or self.worker is None or not self.worker.is_alive():
            self._append("Universe is not running")
            return
        self._log("universe scan stopped")
        self.stop_button.configure(state="disabled")
        self.pause_button.configure(state="disabled")
        try:
            request = getattr(service, "request_stop", None)
            if request is not None:
                request()
            else:
                service.stop()
        except Exception as exc:
            logger.exception("stop failed")
            self._append(f"Stop failed: {exc}")

    def pause_universe(self) -> None:
        """Hold the running scan, or let it continue if it is already paused."""
        if self._mode in ("sixty", "sessions", "scheduler"):
            if self._mode == "sixty" and (self.worker is None or not self.worker.is_alive()):
                return
            if self._mode == "sessions" and not self.calendar._running():
                return
            if self._mode == "scheduler" and (self.worker is None or not self.worker.is_alive()):
                return
            if self._pause_download.is_set():
                self._pause_download.clear()
                self.pause_button.set_paused(False)
                self._log("ο scheduler συνεχίζει" if self._mode == "scheduler" else "η λήψη συνεχίζει")
            else:
                self._pause_download.set()
                self.pause_button.set_paused(True)
                self._log("ο scheduler σε παύση" if self._mode == "scheduler" else "η λήψη σε παύση")
            return
        service = self.service
        if service is None or self.worker is None or not self.worker.is_alive():
            self._append("Universe is not running")
            return
        toggle = getattr(service, "toggle_pause", None)
        if toggle is None:
            self._append("Pause is not available")
            return
        try:
            paused = bool(toggle())
        except Exception as exc:
            logger.exception("pause failed")
            self._append(f"Pause failed: {exc}")
            return
        self.pause_button.set_paused(paused)
        self._log("universe paused" if paused else "universe resumed")

    def show_help(self) -> None:
        window, widget = _text_window(self.root, "Help", format_help())
        self.help_window = window
        self.help_text = widget

    def show_status(self) -> None:
        report = self.status_reader()
        body = "This view does not start or change services.\n\n" + "\n".join(report.lines())
        window, widget = _text_window(self.root, "Status", body)
        self.status_window = window
        self.status_text = widget

    def open_web_ui(self) -> None:
        if self._web_starting:
            return
        self._web_starting = True
        try:
            result = self.web_opener()
        except Exception as exc:
            self._web_starting = False
            logger.exception("web ui failed")
            self._log(f"web ui failed: {exc}")
            return
        if result == "started":
            self._log("web ui starting")
            threading.Thread(target=self._open_web_when_ready, name="web-ui", daemon=True).start()
            return
        self._web_starting = False
        self._log("web ui opened")

    def close(self) -> None:
        self._closed.set()
        self._stop_download.set()
        self._pause_download.clear()
        self._append("Stopping Trading Bot")
        session_worker = getattr(self.calendar, "_worker", None)
        service = self.service
        if service is not None:
            try:
                request = getattr(service, "request_stop", None)
                if request is not None:
                    request()
                else:
                    service.stop()
            except Exception:
                logger.exception("stop failed while closing the window")
        worker = self.worker
        try:
            self.root.destroy()
        except tk.TclError:
            return
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(5)
        if session_worker is not None and session_worker.is_alive():
            session_worker.join(5)

    def _run_service(self, options: LaunchOptions) -> None:
        service = self.service_factory(
            options,
            on_event=self._thread_event,
            on_progress=self._on_progress,
        )
        self.service = service
        try:
            if self._closed.is_set():
                return
            service.start()
            if self._closed.is_set():
                return
            if service.loops_active:
                service.wait()
        except AlreadyRunning as exc:
            self._thread_event(str(exc))
        except Exception as exc:
            logger.exception("Trading Bot stopped because startup failed")
            self._thread_event(f"Startup failed: {exc}")
        finally:
            try:
                service.stop()
            except Exception:
                logger.exception("stop failed")
            self.service = None
            self._post(lambda: self._set_running(False))

    def _run_scheduler(self) -> None:
        try:
            symbols = list(self._scheduler_symbols() or [])
            if not symbols:
                self._post(lambda: self._log("δεν υπάρχουν σύμβολα για λήψη"))
                return

            def download(slot):
                return self._slot_download(
                    slot,
                    symbols,
                    should_stop=self._download_should_stop,
                    wait_if_paused=self._wait_if_download_paused,
                )

            def log(text: str) -> None:
                def apply(text=text) -> None:
                    self._log(text)
                    if text == SCAN_FINISHED:
                        self.calendar.show_month()

                self._post(apply)

            self._scheduler(
                clock=self._scheduler_now,
                download=download,
                log=log,
                should_stop=self._download_should_stop,
                wait_if_paused=self._wait_if_download_paused,
            )
        except Exception as exc:
            logger.exception("scheduler failed")
            self._post(lambda exc=exc: self._log(f"ο scheduler σταμάτησε: {exc}"))
        finally:
            self._post(self._end_scheduler)

    def _scheduler_now(self):
        if self._scheduler_clock is not None:
            return self._scheduler_clock()
        import pandas as pd

        return pd.Timestamp.now(tz="America/New_York")

    def _end_scheduler(self) -> None:
        if self._closed.is_set():
            return
        self._set_running(False)
        if self._stop_download.is_set():
            self._log("ο scheduler σταμάτησε", once=True)

    def _run_sixty(self, dates) -> None:
        try:
            result = self._download_sixty(
                dates,
                on_progress=self._sixty_progress,
                should_stop=lambda: self._stop_download.is_set() or self._closed.is_set(),
                wait_if_paused=self._wait_if_download_paused,
            )
        except NoTrackedSymbols:
            self._post(lambda: self._end_sixty("δεν υπάρχουν σύμβολα για λήψη"))
            return
        except MarketDataUnavailable:
            logger.exception("60-day download rejected by market data")
            self._post(lambda: self._end_sixty("η λήψη από την αγορά απέτυχε"))
            return
        except Exception as exc:
            logger.exception("60-day download failed")
            self._post(lambda exc=exc: self._end_sixty(f"η λήψη δεν ολοκληρώθηκε: {exc}"))
            return
        self._post(lambda result=result: self._end_sixty_result(result))

    def _run_sixty_delete(self, dates) -> None:
        removed = 0
        total = len(dates)
        try:
            for index, day in enumerate(dates):
                removed += int(self._delete_sixty([day]))
                self._post(lambda percent=completion_percent(index + 1, total): self._set_progress(percent))
        except Exception as exc:
            logger.exception("60-day delete failed")
            self._post(lambda exc=exc: self._end_sixty_delete(f"η διαγραφή δεν ολοκληρώθηκε: {exc}"))
            return
        self._post(lambda removed=removed: self._end_sixty_delete(f"διαγράφηκαν {removed} bars", refresh=True))

    def _sixty_progress(self, done: int, total: int) -> None:
        percent = completion_percent(done, total)
        self._post(lambda percent=percent: self._set_progress(percent))

    def _wait_if_download_paused(self) -> None:
        while self._pause_download.is_set() and not self._stop_download.is_set() and not self._closed.is_set():
            self._stop_download.wait(0.2)

    def _end_sixty(self, text: str) -> None:
        self._clear_progress()
        self._set_running(False)
        self._log(text)

    def _end_sixty_result(self, result) -> None:
        self._clear_progress()
        self._set_running(False)
        if result.stopped:
            self._log("η λήψη σταμάτησε", once=True)
            return
        if result.skipped and result.bars == 0 and not result.failed_symbols:
            text = "οι μέρες είναι ήδη στη βάση"
        else:
            text = f"αποθηκεύτηκαν {result.bars} bars"
        if result.failed_symbols:
            text += f". Απέτυχαν {len(result.failed_symbols)} σύμβολα."
        self._log(text)
        self.calendar.show_month()

    def _end_sixty_delete(self, text: str, refresh: bool = False) -> None:
        self._clear_progress()
        self._set_running(False, controls=False)
        self._log(text)
        if refresh:
            self.calendar.show_month()

    def _sync_buy(self) -> None:
        checked, state = buy_checkbox_state(
            self.scheduler_var.get(),
            self.watch_var.get(),
            self.buy_var.get(),
        )
        self.buy_var.set(checked)
        self.buy_check.configure(state=state)

    def _set_running(self, running: bool, *, controls: bool = True) -> None:
        state = "disabled" if running else "normal"
        self.calendar.set_universe_running(running)
        self.start_button.configure(state=state)
        self.sixty_delete_button.configure(state=state)
        if controls or not running:
            self.stop_button.configure(state="normal" if running and controls else "disabled")
            self.pause_button.configure(state="normal" if running and controls else "disabled")
        self.scheduler_check.configure(state=state)
        self.watch_check.configure(state=state)
        self.scheduler_button.configure(state=state)
        if running:
            self.buy_check.configure(state="disabled")
        else:
            self._mode = None
            self._sync_buy()

    def _download_should_stop(self) -> bool:
        return self._stop_download.is_set() or self._closed.is_set()

    def _download_busy(self, busy: bool) -> None:
        if busy:
            self._mode = "sessions"
            self._stop_download.clear()
            self._pause_download.clear()
            self.pause_button.set_paused(False)
            self.start_button.configure(state="disabled")
            self.sixty_delete_button.configure(state="disabled")
            self.pause_button.configure(state="normal")
            self.stop_button.configure(state="normal")
            self.scheduler_button.configure(state="disabled")
            return
        if self._mode == "sessions":
            self._mode = None
            self.pause_button.set_paused(False)
            self.pause_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
        if self.worker is None or not self.worker.is_alive():
            self.start_button.configure(state="normal")
            self.sixty_delete_button.configure(state="normal")
            self.scheduler_button.configure(state="normal")

    def _open_web_when_ready(self) -> None:
        try:
            for _ in range(60):
                if web_ui_is_up() and web_ui_is_up(API_PORT):
                    webbrowser.open(WEB_UI_URL)
                    self._post(lambda: self._log("web ui opened"))
                    return
                time.sleep(0.5)
            self._post(lambda: self._log("web ui failed to open"))
        finally:
            self._web_starting = False

    def _thread_event(self, message: str) -> None:
        if message == "Universe scan stopped":
            self._post(lambda: self._log("universe scan stopped", once=True))
        elif message == "Universe scan succeeded":
            self._post(lambda: self._log("universe scan finished"))
        elif message == "Universe scan failed" or message.startswith("Universe scan failed:"):
            self._post(lambda: self._log("universe scan failed"))
        elif message == "scheduled universe scan":
            self._post(self._log_scheduled_scan)
        else:
            logger.info("%s", message)

    def _log_scheduled_scan(self) -> None:
        options = self._active_options
        if options is None:
            self._log("new scan universe")
            return
        self._log(universe_activity_line(
            options.enable_scheduler,
            options.enable_watch_engine,
            repeat=True,
        ))

    def _on_progress(self, percent: int) -> None:
        self._post(lambda percent=percent: self._set_progress(percent))

    def _arm_progress(self, button) -> None:
        self._progress_button = button
        button.set_progress(0)

    def _clear_progress(self) -> None:
        button = self._progress_button
        self._progress_button = None
        if button is not None:
            button.set_progress(0)

    def _set_progress(self, percent: int) -> None:
        button = self._progress_button
        if button is None:
            return
        button.set_progress(percent)

    def _post(self, callback) -> None:
        if self._closed.is_set():
            return
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            return

    def _append(self, message: str) -> None:
        logger.info("%s", message)

    def _log(self, text: str, *, once: bool = False) -> None:
        if once and self.activity.get("1.0", "end").rstrip().endswith(text):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.activity.configure(state="normal")
        self.activity.insert("end", f"{stamp} : {text}\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")


def _text_window(root: tk.Misc, title: str, body: str) -> tuple[tk.Toplevel, ScrolledText]:
    window = tk.Toplevel(root)
    window.title(title)
    window.configure(background=SURFACE)
    widget = ScrolledText(
        window,
        width=78,
        height=24,
        bg="#0a1016",
        fg=INK,
        insertbackground=INK,
        selectbackground="#1d4f91",
        selectforeground="#ffffff",
        relief="flat",
    )
    widget.grid(sticky="nsew", padx=8, pady=8)
    window.columnconfigure(0, weight=1)
    window.rowconfigure(0, weight=1)
    widget.insert("1.0", body)
    widget.configure(state="disabled")
    return window, widget
