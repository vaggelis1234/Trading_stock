"""Month calendar on the desktop window for choosing cash sessions."""

from __future__ import annotations

import calendar
import logging
import queue
import threading
from datetime import date

import pandas as pd
import tkinter as tk
from tkinter import ttk

from src.data.bar_store import BarStore
from src.data.provider import AlpacaMarketDataProvider, MarketDataUnavailable
from src.data.session_download import (
    NoTrackedSymbols,
    SessionChoiceError,
    delete_sessions,
    download_sessions,
    ny_today,
    session_month,
    tracked_symbols,
)

logger = logging.getLogger(__name__)

MONTHS = [
    "Ιανουάριος",
    "Φεβρουάριος",
    "Μάρτιος",
    "Απρίλιος",
    "Μάιος",
    "Ιούνιος",
    "Ιούλιος",
    "Αύγουστος",
    "Σεπτέμβριος",
    "Οκτώβριος",
    "Νοέμβριος",
    "Δεκέμβριος",
]
WEEKDAYS = ["Δε", "Τρ", "Τε", "Πε", "Πα", "Σα", "Κυ"]
SELECTED_BG = "#1d4f91"
SELECTED_FG = "#ffffff"
STORED_BG = "#143d2c"
STORED_FG = "#b7f0cc"
NORMAL_BG = "#1b2636"
NORMAL_FG = "#d5deea"
CLOSED_BG = "#121820"
CLOSED_FG = "#6d7c8d"


def default_load_month(year: int, month: int, today: date) -> dict:
    return session_month(year, month, BarStore(), today)


def default_download(dates, on_progress=None, should_stop=None, wait_if_paused=None):
    store = BarStore()
    symbols = tracked_symbols(store)
    return download_sessions(
        list(dates),
        provider=AlpacaMarketDataProvider(),
        store=store,
        symbols=symbols,
        clock=pd.Timestamp.now(tz="UTC"),
        on_progress=on_progress,
        should_stop=should_stop,
        wait_if_paused=wait_if_paused,
    )


def default_delete(dates):
    return delete_sessions(list(dates), store=BarStore())


def completion_percent(done: int, total: int) -> int:
    """Share of the task already finished. 100 means every unit is done."""
    if total <= 0:
        return 0
    return max(0, min(100, int(done * 100 / total)))


def session_activity_line(action: str, dates) -> str:
    """One terminal line naming the cash sessions a download or delete touches."""
    listed = ", ".join(day.isoformat() for day in dates)
    if action == "delete":
        return f"διαγράφουμε τις εξής μέρες: {listed}"
    return f"γίνεται λήψη για τις εξής μέρες: {listed}"


class ProgressButton(tk.Canvas):
    """Action button whose fill is the completed share of its task."""

    def __init__(self, master, text: str, command, font_size: int = 10):
        super().__init__(
            master,
            height=font_size + 18,
            highlightthickness=1,
            highlightbackground="#243044",
            bd=0,
            bg="#1b2636",
            cursor="hand2",
        )
        self._text = text
        self._command = command
        self._state = "normal"
        self._percent = 0
        self._font_size = font_size
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    @property
    def percent(self) -> int:
        return self._percent

    def set_progress(self, percent: int) -> None:
        self._percent = max(0, min(100, int(percent)))
        self._draw()

    def apply_font(self, font_size: int) -> None:
        self._font_size = max(9, int(font_size))
        super().configure(height=self._font_size + 18)
        self._draw()

    def configure(self, cnf=None, **kwargs):
        if cnf is None and not kwargs:
            return super().configure()
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
        elif cnf is not None:
            return super().configure(cnf, **kwargs)
        text = kwargs.pop("text", None)
        state = kwargs.pop("state", None)
        if kwargs:
            super().configure(**kwargs)
        if text is not None:
            self._text = text
        if state is not None:
            self._state = state
            super().configure(cursor="hand2" if state == "normal" else "arrow")
        if text is not None or state is not None:
            self._draw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)

    def _click(self, _event) -> None:
        if self._state == "disabled":
            return
        self._command()

    def _draw(self) -> None:
        self.delete("all")
        width = max(int(self.winfo_width()), 2)
        height = max(int(self.winfo_height()), 2)
        active = self._percent > 0
        if self._state == "disabled" and not active:
            track = "#101820"
            ink = "#6d7c8d"
        else:
            track = "#1b2636"
            ink = "#d5deea"
        self.create_rectangle(0, 0, width, height, fill=track, outline="")
        span = int(width * self._percent / 100)
        if span > 0:
            self.create_rectangle(0, 0, span, height, fill="#1d4f91", outline="")
        self.create_text(
            width / 2,
            height / 2,
            text=self._text,
            fill=ink,
            font=("Segoe UI", self._font_size),
        )


class SessionCalendarPanel(ttk.LabelFrame):
    """Select NYSE sessions and download only those 30-minute cash bars."""

    def __init__(self, master, today=None, load_month=None, download=None, delete=None, on_busy=None, on_sixty_download=None, on_sixty_delete=None, on_log=None):
        super().__init__(master, text="Λήψη συνεδριών", padding=8)
        self.today = today or ny_today()
        self.year = self.today.year
        self.month = self.today.month
        self.selected: set[date] = set()
        self._load_month = load_month or default_load_month
        self._download = download or default_download
        self._delete = delete or default_delete
        self._on_busy = on_busy
        self._on_sixty_download = on_sixty_download
        self._on_sixty_delete = on_sixty_delete
        self._on_log = on_log
        self._should_stop = None
        self._wait_if_paused = None
        self._worker = None
        self._active_button = None
        self._events: queue.Queue = queue.Queue()
        self._universe_running = False
        self._catalog: dict[date, dict] = {}
        self._symbols = 0
        self.day_buttons: dict[date, tk.Button] = {}
        self._build()
        self.show_month()

    def _build(self) -> None:
        nav = ttk.Frame(self)
        nav.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.prev_button = ttk.Button(nav, text="←", width=3, command=lambda: self.shift(-1))
        self.prev_button.grid(row=0, column=0)
        self.title_var = tk.StringVar()
        ttk.Label(nav, textvariable=self.title_var, width=18, anchor="center").grid(row=0, column=1, padx=8)
        self.next_button = ttk.Button(nav, text="→", width=3, command=lambda: self.shift(1))
        self.next_button.grid(row=0, column=2)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.grid_frame = ttk.Frame(self)
        self.grid_frame.grid(row=1, column=0, sticky="nsew")
        for column in range(7):
            self.grid_frame.columnconfigure(column, weight=1)
        self._scale = 1.0
        self._wraplength = 440
        self._font_size = 10
        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(self, textvariable=self.status_var, wraplength=440)
        self.status_label.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.actions = actions
        self.download_button = ProgressButton(actions, "Λήψη (0)", self.start_download, self._font_size)
        self.download_button.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 6))
        self.delete_button = ProgressButton(actions, "Διαγραφή", self.start_delete, self._font_size)
        self.delete_button.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        self.sixty_download_button = ProgressButton(actions, "Λήψη 60 Ημερών", self.start_sixty_download, self._font_size)
        self.sixty_download_button.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.sixty_delete_button = ProgressButton(actions, "Διαγραφή 60 Ημερών", self.start_sixty_delete, self._font_size)
        self.sixty_delete_button.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        self.controls = ttk.Frame(actions)
        self.controls.grid(row=2, column=0, columnspan=2, sticky="w")

    def shift(self, delta: int) -> None:
        if self._running():
            return
        index = self.year * 12 + (self.month - 1) + delta
        self.year = index // 12
        self.month = index % 12 + 1
        self.show_month()

    def show_month(self) -> None:
        self.title_var.set(f"{MONTHS[self.month - 1]} {self.year}")
        try:
            month = self._load_month(self.year, self.month, self.today)
        except SessionChoiceError:
            month = {"days": [], "symbols": 0}
        self._catalog = {date.fromisoformat(day["date"]): day for day in month["days"]}
        self._symbols = int(month.get("symbols") or 0)
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.day_buttons = {}
        for column, label in enumerate(WEEKDAYS):
            ttk.Label(self.grid_frame, text=label, anchor="center").grid(row=0, column=column, sticky="ew")
        self.grid_frame.rowconfigure(0, weight=0)
        first = date(self.year, self.month, 1)
        count = calendar.monthrange(self.year, self.month)[1]
        for offset in range(count):
            day = date(self.year, self.month, offset + 1)
            slot = first.weekday() + offset
            row, column = divmod(slot, 7)
            info = self._catalog.get(day, {})
            button = tk.Button(
                self.grid_frame,
                text=self._day_text(day, info),
                width=1,
                relief="flat",
                command=lambda chosen=day: self.toggle(chosen),
            )
            button.grid(row=row + 1, column=column, padx=1, pady=1, sticky="nsew")
            self.grid_frame.rowconfigure(row + 1, weight=1)
            self.day_buttons[day] = button
            self._paint(day)
        if not self._running():
            stored = sum(1 for info in self._catalog.values() if info.get("stored_symbols"))
            self.status_var.set(
                f"{self._symbols} σύμβολα. {stored} συνεδρίες αυτού του μήνα είναι ήδη στη βάση."
            )
        self._sync_download_button()
        self.apply_scale(self._scale, self._wraplength)

    def apply_scale(self, scale: float, wraplength: int) -> None:
        scale = max(0.85, min(float(scale), 2.4))
        font_size = max(9, int(round(10 * scale)))
        self._scale = scale
        self._wraplength = int(wraplength)
        self._font_size = font_size
        font = ("Segoe UI", font_size)
        self.status_label.configure(wraplength=self._wraplength, font=font)
        for child in self.grid_frame.winfo_children():
            if isinstance(child, ttk.Label):
                child.configure(font=font)
        for button in self.day_buttons.values():
            button.configure(font=font)
        for button in self._action_buttons():
            button.apply_font(font_size)

    def _action_buttons(self) -> tuple[ProgressButton, ...]:
        return (
            self.download_button,
            self.delete_button,
            self.sixty_download_button,
            self.sixty_delete_button,
        )

    def toggle(self, day: date) -> None:
        info = self._catalog.get(day)
        if not info or not info.get("selectable") or self._running() or self._universe_running:
            return
        if day in self.selected:
            self.selected.remove(day)
        else:
            self.selected.add(day)
        self._paint(day)
        self._sync_download_button()

    def set_download_controls(self, should_stop=None, wait_if_paused=None) -> None:
        self._should_stop = should_stop
        self._wait_if_paused = wait_if_paused

    def set_universe_running(self, running: bool) -> None:
        self._universe_running = running
        self._sync_download_button()

    def start_download(self) -> None:
        if self._running() or self._universe_running or not self.selected:
            return
        dates = sorted(self.selected)
        self._announce("download", dates)
        self._begin_task(self.download_button, f"Λήψη 0/{len(dates)}")
        self._set_nav(False)
        if self._on_busy is not None:
            self._on_busy(True)
        self._worker = threading.Thread(target=self._run, args=(dates,), name="session-download", daemon=True)
        self._worker.start()
        self._sync_download_button()
        self._poll()

    def start_sixty_download(self) -> None:
        if self._running() or self._universe_running or self._on_sixty_download is None:
            return
        self._on_sixty_download()

    def start_sixty_delete(self) -> None:
        if self._running() or self._universe_running or self._on_sixty_delete is None:
            return
        self._on_sixty_delete()

    def start_delete(self) -> None:
        if self._running() or self._universe_running or not self.selected:
            return
        dates = sorted(self.selected)
        self._announce("delete", dates)
        self._begin_task(self.delete_button, f"Διαγραφή 0/{len(dates)}")
        self._set_nav(False)
        if self._on_busy is not None:
            self._on_busy(True)
        self._worker = threading.Thread(target=self._run_delete, args=(dates,), name="session-delete", daemon=True)
        self._worker.start()
        self._sync_download_button()
        self._poll()

    def _run(self, dates) -> None:
        kwargs = {"on_progress": self._progress}
        if self._should_stop is not None:
            kwargs["should_stop"] = self._should_stop
        if self._wait_if_paused is not None:
            kwargs["wait_if_paused"] = self._wait_if_paused
        try:
            result = self._download(dates, **kwargs)
        except NoTrackedSymbols:
            self._events.put(("finish", "Δεν υπάρχουν σύμβολα στη βάση. Χρειάζεται πρώτα Universe scan.", False))
            return
        except MarketDataUnavailable:
            logger.exception("session download rejected by market data")
            self._events.put(("finish", "Η λήψη από την αγορά απέτυχε.", False))
            return
        except Exception:
            logger.exception("session download failed")
            self._events.put(("finish", "Η λήψη δεν ολοκληρώθηκε.", False))
            return
        if getattr(result, "stopped", False):
            self._events.put(("finish", "Η λήψη σταμάτησε.", False))
            return
        if result.skipped and result.bars == 0 and not result.failed_symbols:
            text = "Οι μέρες είναι ήδη στη βάση."
        else:
            text = f"Αποθηκεύτηκαν {result.bars} bars."
            if result.failed_symbols:
                text += f" Απέτυχαν {len(result.failed_symbols)} σύμβολα."
        self._events.put(("finish", text, True))

    def _run_delete(self, dates) -> None:
        removed = 0
        total = len(dates)
        try:
            for index, day in enumerate(dates):
                removed += int(self._delete([day]))
                self._events.put(("progress", index + 1, total))
        except Exception:
            logger.exception("session delete failed")
            self._events.put(("finish", "Η διαγραφή δεν ολοκληρώθηκε.", False))
            return
        self._events.put(("finish", f"Διαγράφηκαν {removed} bars.", True))

    def _progress(self, done: int, total: int) -> None:
        self._events.put(("progress", done, total))

    def _poll(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                done, total = event[1], event[2]
                if self._active_button is not None:
                    self._active_button.set_progress(completion_percent(done, total))
                label = "Διαγραφή" if self._active_button is self.delete_button else "Λήψη"
                self.status_var.set(f"{label} {done}/{total}")
            elif kind == "finish":
                self._finish(event[1], clear=event[2])
        if self._running() or not self._events.empty():
            try:
                self.after(100, self._poll)
            except RuntimeError:
                return

    def _announce(self, action: str, dates) -> None:
        if self._on_log is not None:
            self._on_log(session_activity_line(action, dates))

    def _begin_task(self, button: ProgressButton, status: str) -> None:
        self._active_button = button
        button.set_progress(0)
        self.status_var.set(status)

    def _finish(self, text: str, clear: bool = False) -> None:
        if self._active_button is not None:
            self._active_button.set_progress(0)
            self._active_button = None
        if clear:
            self.selected.clear()
        self._set_nav(True)
        self.show_month()
        self.status_var.set(text)
        if self._on_busy is not None:
            self._on_busy(False)

    def _day_text(self, day: date, info: dict) -> str:
        text = str(day.day)
        if info.get("stored_symbols"):
            text += " •"
        return text

    def _paint(self, day: date) -> None:
        button = self.day_buttons[day]
        info = self._catalog.get(day, {})
        if not info.get("selectable"):
            button.configure(state="disabled", bg=CLOSED_BG, disabledforeground=CLOSED_FG)
            return
        button.configure(state="normal")
        if day in self.selected:
            button.configure(bg=SELECTED_BG, fg=SELECTED_FG)
            return
        if info.get("stored_symbols"):
            button.configure(bg=STORED_BG, fg=STORED_FG)
            return
        button.configure(bg=NORMAL_BG, fg=NORMAL_FG)

    def _running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _sync_download_button(self) -> None:
        count = len(self.selected)
        blocked = self._running() or self._universe_running or count == 0
        state = "disabled" if blocked else "normal"
        sixty_state = "disabled" if self._running() or self._universe_running else "normal"
        self.download_button.configure(text=f"Λήψη ({count})", state=state)
        self.delete_button.configure(state=state)
        self.sixty_download_button.configure(state=sixty_state)
        self.sixty_delete_button.configure(state=sixty_state)

    def _set_nav(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.prev_button.configure(state=state)
        self.next_button.configure(state=state)
