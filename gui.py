"""Desktop reminder window, overdue checks, and Windows notifications."""

from __future__ import annotations

import ctypes
import sqlite3
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from database import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_OVERDUE,
    STATUS_PENDING,
    Reminder,
    ReminderDatabase,
    default_db_path,
)

COLOR_BG = "#EEF1F4"
COLOR_SHEET = "#FBFCFD"
COLOR_INK = "#15202B"
COLOR_MUTED = "#5E6B76"
COLOR_LINE = "#D5DDE3"
COLOR_LATE = "#A32020"
COLOR_DONE = "#2F6F4E"
COLOR_CANCEL = "#8B939A"
COLOR_SELECT = "#E4EBF1"
COLOR_BUTTON_ACTIVE = "#2A3A4A"

MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
FILTERS: tuple[tuple[str | None, str], ...] = (
    (None, "Все"),
    (STATUS_PENDING, "Ожидает"),
    (STATUS_OVERDUE, "Просрочено"),
    (STATUS_DONE, "Готово"),
    (STATUS_CANCELLED, "Отменено"),
)
STATUS_TAG = {
    STATUS_PENDING: "pending",
    STATUS_OVERDUE: "overdue",
    STATUS_DONE: "done",
    STATUS_CANCELLED: "cancelled",
}

POLL_MS = 1000
TITLE_LIMIT = 120
DESCRIPTION_LIMIT = 2000
HWND_TOPMOST = -1
SW_SHOW = 5
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
FLASHW_TRAY = 0x00000002


class FLASHWINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", ctypes.c_uint),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("uCount", ctypes.c_uint),
        ("dwTimeout", ctypes.c_uint),
    )


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            return


def _user32():
    library = ctypes.windll.user32
    library.GetParent.argtypes = [ctypes.c_void_p]
    library.GetParent.restype = ctypes.c_void_p
    library.IsIconic.argtypes = [ctypes.c_void_p]
    library.IsIconic.restype = ctypes.c_bool
    library.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.ShowWindow.restype = ctypes.c_bool
    library.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.SetWindowPos.restype = ctypes.c_bool
    library.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
    library.FlashWindowEx.restype = ctypes.c_bool
    return library


def _window_handle(widget: tk.Misc) -> int:
    widget.update_idletasks()
    child = int(widget.winfo_id())
    parent = _user32().GetParent(child)
    return int(parent or child)


def _keep_above(widget: tk.Misc) -> None:
    user32 = _user32()
    handle = _window_handle(widget)
    user32.ShowWindow(handle, SW_RESTORE if user32.IsIconic(handle) else SW_SHOW)
    user32.SetWindowPos(
        handle,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    info = FLASHWINFO(
        cbSize=ctypes.sizeof(FLASHWINFO),
        hwnd=handle,
        dwFlags=FLASHW_TRAY,
        uCount=3,
        dwTimeout=0,
    )
    user32.FlashWindowEx(ctypes.byref(info))


def _choose_font(root: tk.Misc, family: str, size: int, weight: str = "normal") -> tuple[str, int, str]:
    available = set(tkfont.families(root))
    chosen = family if family in available else "Segoe UI"
    return (chosen, size, weight)


def format_list_time(value: datetime, today: datetime) -> str:
    if value.year == today.year:
        return value.strftime("%d.%m  %H:%M")
    return value.strftime("%d.%m.%Y  %H:%M")


def format_header_date(value: datetime, today: datetime) -> str:
    if value.date() == today.date():
        day_name = "сегодня"
    elif value.date() == today.date() + timedelta(days=1):
        day_name = "завтра"
    else:
        day_name = WEEKDAYS[value.weekday()]
    return f"{day_name}, {value.day} {MONTHS_GENITIVE[value.month - 1]}"


def show_windows_toast(title: str, message: str) -> None:
    """Show a Windows 10 toast. Failure leaves the on-screen popup in place."""
    try:
        from winotify import Notification
    except ImportError:
        return
    toast = Notification(
        app_id="Напоминания",
        title=title,
        msg=message or "Время напоминания",
        duration="long",
    )
    toast.show()


class ReminderPopup(tk.Toplevel):
    """Always-on-top reminder. Stays visible when the main window is minimized."""

    def __init__(self, master: tk.Misc, reminders: list[Reminder], stack_index: int) -> None:
        super().__init__(master)
        self.configure(bg=COLOR_SHEET)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        single = len(reminders) == 1
        self.title(reminders[0].title if single else "Напоминания")

        padding = tk.Frame(self, bg=COLOR_SHEET)
        padding.pack(fill="both", expand=True, padx=22, pady=18)

        kicker = tk.Label(
            padding,
            text="Напоминание" if single else f"Напоминания: {len(reminders)}",
            bg=COLOR_SHEET,
            fg=COLOR_LATE,
            font=("Segoe UI", 10),
            anchor="w",
        )
        kicker.pack(fill="x")

        if single:
            reminder = reminders[0]
            time_font = _choose_font(self, "Bahnschrift", 36)
            tk.Label(
                padding,
                text=reminder.trigger_at.strftime("%H:%M"),
                bg=COLOR_SHEET,
                fg=COLOR_INK,
                font=time_font,
                anchor="w",
            ).pack(fill="x", pady=(6, 0))
            tk.Label(
                padding,
                text=reminder.title,
                bg=COLOR_SHEET,
                fg=COLOR_INK,
                font=("Segoe UI", 14),
                anchor="w",
                justify="left",
                wraplength=340,
            ).pack(fill="x", pady=(2, 8))
            if reminder.description:
                tk.Label(
                    padding,
                    text=reminder.description,
                    bg=COLOR_SHEET,
                    fg=COLOR_MUTED,
                    font=("Segoe UI", 11),
                    anchor="w",
                    justify="left",
                    wraplength=340,
                ).pack(fill="x")
        else:
            lines = [
                f"{item.trigger_at.strftime('%H:%M')}  {item.title}"
                + (f"\n{item.description}" if item.description else "")
                for item in reminders
            ]
            text = tk.Text(
                padding,
                width=42,
                height=min(12, 2 + len(reminders) * 2),
                bg=COLOR_SHEET,
                fg=COLOR_INK,
                font=("Segoe UI", 11),
                relief="flat",
                wrap="word",
                highlightthickness=0,
            )
            text.insert("1.0", "\n\n".join(lines))
            text.configure(state="disabled")
            text.pack(fill="both", expand=True, pady=(10, 0))

        close = tk.Button(
            padding,
            text="Закрыть",
            command=self.destroy,
            bg=COLOR_INK,
            fg=COLOR_SHEET,
            activebackground=COLOR_BUTTON_ACTIVE,
            activeforeground=COLOR_SHEET,
            relief="flat",
            bd=0,
            padx=16,
            pady=8,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        close.pack(anchor="e", pady=(16, 0))

        self.update_idletasks()
        width = 400
        height = max(self.winfo_reqheight(), 220)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, screen_w - width - 28)
        y = max(0, screen_h - height - 80 - stack_index * 36)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.after(50, self._raise)

    def _raise(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            _keep_above(self)
        except (tk.TclError, OSError):
            return


class Notifier:
    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._popups: list[ReminderPopup] = []

    def notify(self, reminders: list[Reminder]) -> None:
        if not reminders:
            return
        self._sound()
        self._popups = [popup for popup in self._popups if popup.winfo_exists()]
        popup = ReminderPopup(self.root, reminders, len(self._popups))
        self._popups.append(popup)
        threading.Thread(target=self._toast_all, args=(tuple(reminders),), daemon=True).start()

    def close(self) -> None:
        for popup in list(self._popups):
            if popup.winfo_exists():
                popup.destroy()
        self._popups.clear()

    def _toast_all(self, reminders: tuple[Reminder, ...]) -> None:
        for reminder in reminders:
            when = format_header_date(reminder.trigger_at, datetime.now())
            message = reminder.description or f"{when}, {reminder.trigger_at.strftime('%H:%M')}"
            try:
                show_windows_toast(reminder.title, message)
            except Exception:
                continue

    @staticmethod
    def _sound() -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except (ImportError, RuntimeError):
            return


class ReminderApp(tk.Tk):
    def __init__(self, db_path: Path | None = None) -> None:
        super().__init__()
        self.db = ReminderDatabase(db_path)
        self.filter_status: str | None = None
        self._filter_buttons: dict[str | None, tk.Button] = {}
        self.time_font = _choose_font(self, "Bahnschrift", 52)
        self.body_font = ("Segoe UI", 11)
        self.notifier = Notifier(self)

        self.title("Напоминания")
        self.configure(bg=COLOR_BG)
        self.minsize(860, 560)
        self._center(980, 660)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_styles()
        self._build()
        self.refresh()
        self.after(200, self._tick)

    def _center(self, width: int, height: int) -> None:
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Reminders.Treeview",
            background=COLOR_SHEET,
            fieldbackground=COLOR_SHEET,
            foreground=COLOR_INK,
            borderwidth=0,
            relief="flat",
            rowheight=34,
            font=self.body_font,
        )
        style.configure(
            "Reminders.Treeview.Heading",
            background=COLOR_BG,
            foreground=COLOR_MUTED,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.map(
            "Reminders.Treeview",
            background=[("selected", COLOR_SELECT)],
            foreground=[("selected", COLOR_INK)],
        )
        style.layout("Reminders.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure(
            "Reminders.Vertical.TScrollbar",
            background=COLOR_BG,
            troughcolor=COLOR_SHEET,
            borderwidth=0,
            arrowsize=12,
        )
        style.configure(
            "Reminders.TCombobox",
            fieldbackground=COLOR_SHEET,
            background=COLOR_BG,
            foreground=COLOR_INK,
            arrowcolor=COLOR_INK,
            borderwidth=0,
            relief="flat",
            padding=4,
        )

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = tk.Frame(self, bg=COLOR_SHEET)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        self.time_label = tk.Label(
            header,
            text="—",
            bg=COLOR_SHEET,
            fg=COLOR_INK,
            font=self.time_font,
            anchor="w",
        )
        self.time_label.grid(row=0, column=0, rowspan=2, padx=(28, 18), pady=(16, 14), sticky="w")
        self.next_title = tk.Label(
            header,
            text="Нет ожидающих",
            bg=COLOR_SHEET,
            fg=COLOR_INK,
            font=("Segoe UI", 16),
            anchor="sw",
            justify="left",
        )
        self.next_title.grid(row=0, column=1, sticky="swe", padx=(0, 28), pady=(22, 0))
        self.next_when = tk.Label(
            header,
            text="",
            bg=COLOR_SHEET,
            fg=COLOR_MUTED,
            font=("Segoe UI", 11),
            anchor="nw",
            justify="left",
        )
        self.next_when.grid(row=1, column=1, sticky="nwe", padx=(0, 28), pady=(2, 16))
        self.next_title.configure(wraplength=560)
        rule = tk.Frame(header, bg=COLOR_LINE, height=1, bd=0, highlightthickness=0)
        rule.grid(row=2, column=0, columnspan=2, sticky="ew")
        rule.grid_propagate(False)

        body = tk.Frame(self, bg=COLOR_BG)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=320)
        body.rowconfigure(1, weight=1)

        filters = tk.Frame(body, bg=COLOR_BG)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(14, 8))
        for status, label in FILTERS:
            button = tk.Button(
                filters,
                text=label,
                command=lambda chosen=status: self._set_filter(chosen),
                relief="flat",
                bd=0,
                padx=14,
                pady=6,
                font=("Segoe UI", 11),
                cursor="hand2",
                highlightthickness=0,
            )
            button.pack(side="left", padx=(0, 6))
            self._filter_buttons[status] = button
        self._paint_filters()

        list_frame = tk.Frame(body, bg=COLOR_SHEET)
        self.list_frame = list_frame
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(20, 0), pady=(0, 8))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        list_frame.bind("<Configure>", self._wrap_detail)

        self.tree = ttk.Treeview(
            list_frame,
            columns=("when", "title", "status"),
            show="headings",
            style="Reminders.Treeview",
            selectmode="browse",
        )
        self.tree.heading("when", text="Время", anchor="w")
        self.tree.heading("title", text="Заголовок", anchor="w")
        self.tree.heading("status", text="Статус", anchor="w")
        self.tree.column("when", width=130, minwidth=110, stretch=False, anchor="w")
        self.tree.column("title", width=280, minwidth=140, stretch=True, anchor="w")
        self.tree.column("status", width=120, minwidth=100, stretch=False, anchor="w")
        self.tree.tag_configure("pending", foreground=COLOR_INK)
        self.tree.tag_configure("overdue", foreground=COLOR_LATE)
        self.tree.tag_configure("done", foreground=COLOR_DONE)
        self.tree.tag_configure("cancelled", foreground=COLOR_CANCEL, font=("Segoe UI", 11, "overstrike"))
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda _event: self._delete_selected())

        scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.tree.yview,
            style="Reminders.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns", pady=(8, 0), padx=(0, 4))

        self.empty_label = tk.Label(
            list_frame,
            text="",
            bg=COLOR_SHEET,
            fg=COLOR_MUTED,
            font=("Segoe UI", 12),
            anchor="w",
            justify="left",
        )

        self.detail = tk.Label(
            list_frame,
            text="Выберите напоминание, чтобы прочитать описание.",
            bg=COLOR_SHEET,
            fg=COLOR_MUTED,
            font=self.body_font,
            anchor="nw",
            justify="left",
            wraplength=460,
        )
        self.detail.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(12, 4))

        actions = tk.Frame(list_frame, bg=COLOR_SHEET)
        actions.grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 12))
        self.done_button = self._make_button(actions, "Готово", self._mark_done)
        self.cancel_button = self._make_button(actions, "Отменено", self._mark_cancelled)
        self.delete_button = self._make_button(actions, "Удалить", self._delete_selected, danger=True)
        for button in (self.done_button, self.cancel_button, self.delete_button):
            button.pack(side="left", padx=(0, 8))

        form = tk.Frame(body, bg=COLOR_BG)
        form.grid(row=1, column=1, sticky="nsew", padx=(20, 24), pady=(0, 8))
        tk.Label(
            form,
            text="Новое напоминание",
            bg=COLOR_BG,
            fg=COLOR_INK,
            font=("Segoe UI", 14),
            anchor="w",
        ).pack(fill="x", pady=(4, 14))

        self.title_entry = self._labeled_entry(form, "Заголовок")
        tk.Label(form, text="Описание", bg=COLOR_BG, fg=COLOR_MUTED, font=("Segoe UI", 10), anchor="w").pack(
            fill="x", pady=(12, 4)
        )
        self.description_box = tk.Text(
            form,
            width=28,
            height=4,
            bg=COLOR_SHEET,
            fg=COLOR_INK,
            insertbackground=COLOR_INK,
            relief="flat",
            font=self.body_font,
            wrap="word",
            highlightthickness=1,
            highlightbackground=COLOR_LINE,
            highlightcolor=COLOR_INK,
            padx=8,
            pady=6,
        )
        self.description_box.pack(fill="x")

        when = tk.Frame(form, bg=COLOR_BG)
        when.pack(fill="x", pady=(12, 0))

        tk.Label(when, text="Дата", bg=COLOR_BG, fg=COLOR_MUTED, font=("Segoe UI", 10), anchor="w").pack(fill="x")
        date_row = tk.Frame(when, bg=COLOR_BG)
        date_row.pack(fill="x", pady=(4, 0))
        self.day_spin = self._spinbox(date_row, 1, 31, 3)
        self.day_spin.pack(side="left")
        now = datetime.now()
        years = [str(year) for year in range(now.year, now.year + 6)]
        self.year_box = ttk.Combobox(
            date_row,
            values=years,
            state="readonly",
            width=6,
            font=self.body_font,
            style="Reminders.TCombobox",
        )
        self.year_box.pack(side="right")
        self.month_box = ttk.Combobox(
            date_row,
            values=MONTHS,
            state="readonly",
            width=12,
            font=self.body_font,
            style="Reminders.TCombobox",
        )
        self.month_box.pack(side="left", padx=6, fill="x", expand=True)

        tk.Label(when, text="Время", bg=COLOR_BG, fg=COLOR_MUTED, font=("Segoe UI", 10), anchor="w").pack(
            fill="x", pady=(12, 0)
        )
        time_row = tk.Frame(when, bg=COLOR_BG)
        time_row.pack(fill="x", pady=(4, 0))
        self.hour_spin = self._spinbox(time_row, 0, 23, 3)
        self.hour_spin.pack(side="left")
        tk.Label(time_row, text=":", bg=COLOR_BG, fg=COLOR_INK, font=("Segoe UI", 14)).pack(side="left", padx=4)
        self.minute_spin = self._spinbox(time_row, 0, 59, 3)
        self.minute_spin.pack(side="left")

        self.form_error = tk.Label(
            form,
            text="",
            bg=COLOR_BG,
            fg=COLOR_LATE,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=280,
        )
        self.form_error.pack(fill="x", pady=(10, 0))

        add = tk.Button(
            form,
            text="Добавить",
            command=self._add,
            bg=COLOR_INK,
            fg=COLOR_SHEET,
            activebackground=COLOR_BUTTON_ACTIVE,
            activeforeground=COLOR_SHEET,
            relief="flat",
            bd=0,
            padx=16,
            pady=10,
            font=("Segoe UI", 11),
            cursor="hand2",
        )
        add.pack(fill="x", pady=(12, 0))
        self._set_default_when(datetime.now() + timedelta(hours=1))

        footer = tk.Frame(self, bg=COLOR_BG)
        footer.grid(row=2, column=0, sticky="ew")
        tk.Label(
            footer,
            text="Сверните окно — напоминание всё равно откроется.",
            bg=COLOR_BG,
            fg=COLOR_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(side="left", padx=24, pady=10)
        self.error_label = tk.Label(
            footer,
            text="",
            bg=COLOR_BG,
            fg=COLOR_LATE,
            font=("Segoe UI", 10),
            anchor="e",
        )
        self.error_label.pack(side="right", padx=24, pady=10)

    def _labeled_entry(self, parent: tk.Widget, caption: str) -> tk.Entry:
        tk.Label(parent, text=caption, bg=COLOR_BG, fg=COLOR_MUTED, font=("Segoe UI", 10), anchor="w").pack(
            fill="x", pady=(0, 4)
        )
        entry = tk.Entry(
            parent,
            bg=COLOR_SHEET,
            fg=COLOR_INK,
            insertbackground=COLOR_INK,
            relief="flat",
            font=self.body_font,
            highlightthickness=1,
            highlightbackground=COLOR_LINE,
            highlightcolor=COLOR_INK,
        )
        entry.pack(fill="x", ipady=6, ipadx=8)
        return entry

    def _spinbox(self, parent: tk.Widget, start: int, end: int, width: int) -> tk.Spinbox:
        return tk.Spinbox(
            parent,
            from_=start,
            to=end,
            wrap=True,
            width=width,
            format="%02.0f",
            justify="center",
            bg=COLOR_SHEET,
            fg=COLOR_INK,
            buttonbackground=COLOR_BG,
            relief="flat",
            font=self.body_font,
            highlightthickness=1,
            highlightbackground=COLOR_LINE,
            highlightcolor=COLOR_INK,
        )

    def _make_button(self, parent: tk.Widget, text: str, command, danger: bool = False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=COLOR_SHEET,
            fg=COLOR_LATE if danger else COLOR_INK,
            activebackground=COLOR_SELECT,
            activeforeground=COLOR_LATE if danger else COLOR_INK,
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            font=("Segoe UI", 11),
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=COLOR_LINE,
            highlightcolor=COLOR_INK,
            disabledforeground=COLOR_CANCEL,
        )

    def _set_default_when(self, moment: datetime) -> None:
        self.day_spin.delete(0, "end")
        self.day_spin.insert(0, f"{moment.day:02d}")
        self.month_box.set(MONTHS[moment.month - 1])
        year = str(moment.year)
        if year not in self.year_box["values"]:
            self.year_box["values"] = tuple(self.year_box["values"]) + (year,)
        self.year_box.set(year)
        self.hour_spin.delete(0, "end")
        self.hour_spin.insert(0, f"{moment.hour:02d}")
        self.minute_spin.delete(0, "end")
        self.minute_spin.insert(0, f"{moment.minute:02d}")

    def _paint_filters(self) -> None:
        for status, button in self._filter_buttons.items():
            selected = status == self.filter_status
            button.configure(
                bg=COLOR_INK if selected else COLOR_BG,
                fg=COLOR_SHEET if selected else COLOR_INK,
                activebackground=COLOR_INK if selected else COLOR_SELECT,
                activeforeground=COLOR_SHEET if selected else COLOR_INK,
            )

    def _set_filter(self, status: str | None) -> None:
        self.filter_status = status
        self._paint_filters()
        self.refresh()

    def _wrap_detail(self, event: tk.Event) -> None:
        if event.widget is not self.list_frame:
            return
        self.detail.configure(wraplength=max(240, event.width - 36))
        self.next_title.configure(wraplength=max(240, self.winfo_width() - 280))

    def refresh(self) -> None:
        selected = self._selected_id()
        self.tree.delete(*self.tree.get_children())
        now = datetime.now()
        reminders = self.db.list_reminders(self.filter_status)
        for reminder in reminders:
            self.tree.insert(
                "",
                "end",
                iid=str(reminder.id),
                values=(
                    format_list_time(reminder.trigger_at, now),
                    reminder.title,
                    reminder.status,
                ),
                tags=(STATUS_TAG[reminder.status],),
            )
        if reminders:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text=self._empty_text())
            self.empty_label.place(x=16, y=48, anchor="nw")
            self.empty_label.lift()
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
            self.tree.focus(str(selected))
            self._show_detail(self.db.get(selected))
        else:
            self._show_detail(None)
        self._show_next(self.db.next_pending(), now)

    def _empty_text(self) -> str:
        if self.filter_status is None:
            return "Пока пусто. Новое напоминание — справа."
        return f"Нет напоминаний со статусом «{self.filter_status}»."

    def _show_next(self, reminder: Reminder | None, now: datetime) -> None:
        if reminder is None:
            self.time_label.configure(text="—")
            self.next_title.configure(text="Нет ожидающих")
            self.next_when.configure(text="")
            return
        self.time_label.configure(text=reminder.trigger_at.strftime("%H:%M"))
        self.next_title.configure(text=reminder.title)
        self.next_when.configure(text=format_header_date(reminder.trigger_at, now))

    def _on_select(self, _event: tk.Event | None = None) -> None:
        reminder_id = self._selected_id()
        self._show_detail(self.db.get(reminder_id) if reminder_id is not None else None)

    def _show_detail(self, reminder: Reminder | None) -> None:
        if reminder is None:
            self.detail.configure(
                text="Выберите напоминание, чтобы прочитать описание.",
                fg=COLOR_MUTED,
            )
        elif reminder.description:
            self.detail.configure(text=reminder.description, fg=COLOR_INK)
        else:
            self.detail.configure(text="Без описания.", fg=COLOR_MUTED)
        state = "normal" if reminder is not None else "disabled"
        for button in (self.done_button, self.cancel_button, self.delete_button):
            button.configure(state=state)

    def _selected_id(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _read_trigger(self) -> datetime:
        month_name = self.month_box.get()
        if month_name not in MONTHS:
            raise ValueError("Выберите дату.")
        try:
            return datetime(
                year=int(self.year_box.get()),
                month=MONTHS.index(month_name) + 1,
                day=int(float(self.day_spin.get().replace(",", "."))),
                hour=int(float(self.hour_spin.get().replace(",", "."))),
                minute=int(float(self.minute_spin.get().replace(",", "."))),
            )
        except ValueError as exc:
            raise ValueError("Такой даты нет.") from exc

    def _add(self) -> None:
        title = " ".join(self.title_entry.get().split())
        description = self.description_box.get("1.0", "end").strip()
        if not title:
            self._form_message("Напишите заголовок.")
            return
        if len(title) > TITLE_LIMIT:
            self._form_message(f"Заголовок длиннее {TITLE_LIMIT} символов.")
            return
        if len(description) > DESCRIPTION_LIMIT:
            self._form_message(f"Описание длиннее {DESCRIPTION_LIMIT} символов.")
            return
        try:
            trigger_at = self._read_trigger()
        except ValueError as exc:
            self._form_message(str(exc))
            return
        if trigger_at <= datetime.now().replace(second=0, microsecond=0):
            self._form_message("Это время уже прошло. Укажите будущее.")
            return
        try:
            reminder = self.db.add(title, description, trigger_at)
        except sqlite3.Error as exc:
            self._form_message(f"Не удалось сохранить: {exc}")
            return
        self.title_entry.delete(0, "end")
        self.description_box.delete("1.0", "end")
        self._form_message("")
        self.filter_status = None
        self._paint_filters()
        self.refresh()
        self.tree.selection_set(str(reminder.id))
        self.tree.see(str(reminder.id))
        self._show_detail(reminder)
        self.title_entry.focus_set()

    def _mark_done(self) -> None:
        self._update_status(STATUS_DONE)

    def _mark_cancelled(self) -> None:
        self._update_status(STATUS_CANCELLED)

    def _update_status(self, status: str) -> None:
        reminder_id = self._selected_id()
        if reminder_id is None:
            return
        try:
            self.db.set_status(reminder_id, status)
        except KeyError:
            self.refresh()
            return
        except sqlite3.Error as exc:
            self._set_error(f"Не удалось обновить статус: {exc}")
            return
        self._set_error("")
        self.refresh()

    def _delete_selected(self) -> None:
        reminder_id = self._selected_id()
        if reminder_id is None:
            return
        reminder = self.db.get(reminder_id)
        if reminder is None:
            self.refresh()
            return
        confirmed = messagebox.askyesno(
            "Удалить напоминание",
            f"Удалить «{reminder.title}»? Восстановить его будет нельзя.",
            parent=self,
        )
        if not confirmed:
            return
        try:
            self.db.delete(reminder_id)
        except KeyError:
            pass
        except sqlite3.Error as exc:
            self._set_error(f"Не удалось удалить: {exc}")
            return
        self._set_error("")
        self.refresh()

    def _form_message(self, text: str) -> None:
        self.form_error.configure(text=text)

    def _set_error(self, text: str) -> None:
        self.error_label.configure(text=text)

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            due = self.db.claim_due(datetime.now())
            if due:
                self.notifier.notify(due)
                self.refresh()
                self._set_error("")
        except tk.TclError:
            return
        except Exception as exc:
            self._set_error(f"Не удалось проверить сроки: {exc}")
        try:
            self.after(POLL_MS, self._tick)
        except tk.TclError:
            return

    def _on_close(self) -> None:
        self.notifier.close()
        self.destroy()


def main() -> None:
    _enable_dpi_awareness()
    app = ReminderApp(default_db_path())
    app.mainloop()


if __name__ == "__main__":
    main()
