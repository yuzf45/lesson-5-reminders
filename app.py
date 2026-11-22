"""Окно напоминалки и доставка уведомлений.

Проверка срока идёт в цикле Tk, поэтому напоминания всплывают и когда
главное окно свёрнуто. Закрытие окна останавливает приложение.
"""

from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
import tkinter as tk
import winsound
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from db import (
    STATUSES,
    ReminderDB,
    format_due,
)

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "reminders.db"
POLL_MS = 1000

# Идентификатор уже зарегистрированного приложения Windows.
# Неупакованный Python без своего AppUserModelID тосты не показывает.
_TOAST_APP_ID = "Microsoft.Windows.Explorer"


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _xml_escape(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch == "\n" or ord(ch) >= 32)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def show_windows_toast(title: str, message: str) -> None:
    """Системное уведомление Windows 10/11 через Windows Runtime.

    Это аналог win10toast и не требует отдельных пакетов.
    Если PowerShell не запустился, пробуем win10toast или winotify.
    Ошибка тоста не должна срывать всплывающее окно.
    """
    body = message.strip() or "Время напоминания"
    try:
        _powershell_toast(title, body)
        return
    except Exception:
        pass
    if _try_win10toast(title, body):
        return
    _try_winotify(title, body)


def _try_win10toast(title: str, message: str) -> bool:
    try:
        from win10toast import ToastNotifier
    except Exception:
        return False
    try:
        ToastNotifier().show_toast(title, message, duration=8, threaded=True)
        return True
    except Exception:
        return False


def _try_winotify(title: str, message: str) -> bool:
    try:
        from winotify import Notification, audio
    except Exception:
        return False
    try:
        toast = Notification(
            app_id=_TOAST_APP_ID,
            title=title,
            msg=message,
            duration="long",
        )
        toast.set_audio(audio.Reminder, loop=False)
        toast.show()
        return True
    except Exception:
        return False


def _powershell_toast(title: str, message: str) -> None:
    xml = (
        '<toast duration="long">'
        "<visual>"
        '<binding template="ToastGeneric">'
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(message)}</text>"
        "</binding>"
        "</visual>"
        '<audio src="ms-winsoundevent:Notification.Reminder"/>'
        "</toast>"
    )
    script = """
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$xmlText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:REMINDER_TOAST_XML))
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlText)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:REMINDER_TOAST_APP_ID)
$notifier.Show($toast)
""".strip()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    env = os.environ.copy()
    env["REMINDER_TOAST_XML"] = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    env["REMINDER_TOAST_APP_ID"] = _TOAST_APP_ID
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        env=env,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class _Foreground:
    """Вывести окно Tk поверх остальных, даже если главное окно свёрнуто."""

    _ready = False

    @classmethod
    def prepare(cls) -> None:
        if cls._ready:
            return
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        cls._set_long = setter
        cls._ready = True

    @classmethod
    def raise_window(cls, widget: tk.Misc) -> None:
        cls.prepare()
        widget.update_idletasks()
        user32 = ctypes.windll.user32
        child = widget.winfo_id()
        hwnd = user32.GetParent(child) or child
        # Уowned-окна Tk прячутся вместе со свёрнутым родителем.
        # Снимаем владельца, чтобы popup жил отдельно.
        cls._set_long(hwnd, -8, 0)  # GWL_HWNDPARENT
        hwnd_topmost = ctypes.c_void_p(-1)
        flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, flags)
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        cls._flash(hwnd)

    @staticmethod
    def _flash(hwnd: int) -> None:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        info = FLASHWINFO(
            cbSize=ctypes.sizeof(FLASHWINFO),
            hwnd=hwnd,
            dwFlags=0x00000003 | 0x0000000C,  # FLASHW_ALL | FLASHW_TIMERNOFG
            uCount=4,
            dwTimeout=0,
        )
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))


class ReminderApp:
    def __init__(self, db_path: Path | None = None) -> None:
        _enable_dpi_awareness()
        self.db = ReminderDB(db_path or DB_PATH)
        self.root = tk.Tk()
        self.root.title("Напоминалка")
        self.root.geometry("920x620")
        self.root.minsize(760, 520)
        self._apply_style()

        self.title_var = tk.StringVar()
        self.date_var = tk.StringVar()
        self.time_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="Все")
        self.status_var = tk.StringVar(
            value="Окно можно свернуть — проверка напоминаний продолжится."
        )
        self._rows_signature: tuple | None = None
        self._popup_index = 0
        self._reset_due_fields()
        self._build()
        self.refresh(force=True)
        self.root.after(400, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self) -> None:
        self.root.mainloop()

    def _apply_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 9), foreground="#5c5c5c")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 4))
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _reset_due_fields(self) -> None:
        soon = datetime.now() + timedelta(minutes=5)
        self.date_var.set(soon.strftime("%Y-%m-%d"))
        self.time_var.set(soon.strftime("%H:%M"))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Напоминалка", style="Header.TLabel").pack(side="left")
        filter_box = ttk.Frame(header)
        filter_box.pack(side="right")
        ttk.Label(filter_box, text="Статус").pack(side="left", padx=(0, 6))
        self.filter_combo = ttk.Combobox(
            filter_box,
            textvariable=self.filter_var,
            values=("Все", *STATUSES),
            state="readonly",
            width=16,
        )
        self.filter_combo.pack(side="left")
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh(force=True))

        form = ttk.LabelFrame(outer, text="Новое напоминание", padding=10)
        form.pack(fill="x", pady=(14, 10))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Заголовок").grid(row=0, column=0, sticky="w", pady=3)
        title_entry = ttk.Entry(form, textvariable=self.title_var)
        title_entry.grid(row=0, column=1, columnspan=4, sticky="ew", padx=(8, 0), pady=3)
        title_entry.bind("<Return>", lambda _event: self._add())

        ttk.Label(form, text="Описание").grid(row=1, column=0, sticky="nw", pady=3)
        self.description_text = tk.Text(form, height=3, font=("Segoe UI", 10), wrap="word")
        self.description_text.grid(row=1, column=1, columnspan=4, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(form, text="Дата").grid(row=2, column=0, sticky="w", pady=3)
        date_entry = ttk.Entry(form, textvariable=self.date_var, width=16)
        date_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
        ttk.Label(form, text="Время").grid(row=2, column=2, sticky="e", padx=(12, 0))
        time_entry = ttk.Entry(form, textvariable=self.time_var, width=10)
        time_entry.grid(row=2, column=3, sticky="w", padx=(8, 0))
        ttk.Button(form, text="Добавить", command=self._add).grid(row=2, column=4, sticky="e")
        ttk.Label(
            form,
            text="Дата в формате ГГГГ-ММ-ДД, время — ЧЧ:ММ",
            style="Hint.TLabel",
        ).grid(row=3, column=1, columnspan=4, sticky="w", padx=(8, 0))
        date_entry.bind("<Return>", lambda _event: self._add())
        time_entry.bind("<Return>", lambda _event: self._add())

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True, pady=(4, 8))
        columns = ("title", "due", "status")
        self.tree = ttk.Treeview(
            list_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("title", text="Заголовок")
        self.tree.heading("due", text="Срабатывание")
        self.tree.heading("status", text="Статус")
        self.tree.column("title", width=420, anchor="w")
        self.tree.column("due", width=160, anchor="w", stretch=False)
        self.tree.column("status", width=120, anchor="w", stretch=False)
        self.tree.tag_configure("Ожидает", foreground="#1a1a1a")
        self.tree.tag_configure("Готово", foreground="#0b6a0b")
        self.tree.tag_configure("Просрочено", foreground="#a1260d")
        self.tree.tag_configure("Отменено", foreground="#6b6b6b")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda _event: self._delete())

        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        ttk.Button(actions, text="Готово", command=lambda: self._set_status("Готово")).pack(side="left")
        ttk.Button(actions, text="Отменено", command=lambda: self._set_status("Отменено")).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="Удалить", command=self._delete).pack(side="left")

        details = ttk.LabelFrame(outer, text="Описание", padding=8)
        details.pack(fill="x", pady=(10, 6))
        self.details = tk.Text(
            details,
            height=5,
            font=("Segoe UI", 10),
            wrap="word",
            relief="flat",
            background=self.root.cget("background") if sys.platform != "win32" else "#f5f5f5",
        )
        self.details.pack(fill="x")
        self.details.configure(state="disabled")

        ttk.Label(outer, textvariable=self.status_var, style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

    def _tick(self) -> None:
        try:
            changed = self.db.mark_overdue()
            due = self.db.due_unnotified()
            for item in due:
                self._notify(item)
                self.db.mark_notified(item["id"])
            if changed or due:
                self.refresh(force=True)
            else:
                self.refresh()
        except Exception as exc:
            self.status_var.set(f"Ошибка проверки: {exc}")
        finally:
            self.root.after(POLL_MS, self._tick)

    def _notify(self, item: dict) -> None:
        self._show_popup(item)
        try:
            show_windows_toast(item["title"], item["description"])
        except Exception:
            self.status_var.set("Не удалось показать системное уведомление. Открыто окно напоминания.")
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except RuntimeError:
            pass

    def _show_popup(self, item: dict) -> None:
        win = tk.Toplevel(self.root)
        win.title("Напоминание")
        win.resizable(False, False)
        win.configure(bg="white")
        win.attributes("-topmost", True)

        width, height = 440, 280
        offset = (self._popup_index % 5) * 26
        self._popup_index += 1
        x = max(0, (win.winfo_screenwidth() - width) // 2 + offset)
        y = max(0, (win.winfo_screenheight() - height) // 2 + offset)
        win.geometry(f"{width}x{height}+{x}+{y}")

        tk.Frame(win, bg="#0067c0", height=8).pack(fill="x")
        body = tk.Frame(win, bg="white", padx=16, pady=12)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text=item["title"],
            bg="white",
            fg="#1a1a1a",
            font=("Segoe UI", 14, "bold"),
            anchor="w",
            justify="left",
            wraplength=390,
        ).pack(fill="x")
        tk.Label(
            body,
            text=f"{format_due(item['due_at'])}  ·  {item['status']}",
            bg="white",
            fg="#5c5c5c",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(4, 8))
        tk.Message(
            body,
            text=item["description"].strip() or "Без описания",
            bg="white",
            fg="#1a1a1a",
            font=("Segoe UI", 10),
            width=390,
            anchor="w",
        ).pack(fill="both", expand=True)

        buttons = tk.Frame(body, bg="white")
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Готово", command=lambda: self._popup_status(item["id"], "Готово", win)).pack(
            side="left"
        )
        ttk.Button(
            buttons,
            text="Отменено",
            command=lambda: self._popup_status(item["id"], "Отменено", win),
        ).pack(side="left", padx=8)
        ttk.Button(buttons, text="Закрыть", command=win.destroy).pack(side="right")

        win.update_idletasks()
        win.lift()
        win.attributes("-topmost", True)
        try:
            _Foreground.raise_window(win)
        except Exception:
            win.lift()

    def _popup_status(self, reminder_id: int, status: str, window: tk.Toplevel) -> None:
        self.db.update_status(reminder_id, status)
        window.destroy()
        self.refresh(force=True)
        self.status_var.set(f"Напоминание отмечено: {status}.")

    def _parse_due(self) -> datetime:
        raw = f"{self.date_var.get().strip()} {self.time_var.get().strip()}"
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise ValueError("Укажите дату как ГГГГ-ММ-ДД и время как ЧЧ:ММ.")

    def _add(self) -> None:
        title = self.title_var.get().strip()
        if not title:
            messagebox.showwarning("Напоминание", "Введите заголовок.", parent=self.root)
            return
        try:
            due = self._parse_due()
        except ValueError as exc:
            messagebox.showwarning("Напоминание", str(exc), parent=self.root)
            return
        description = self.description_text.get("1.0", "end").strip()
        self.db.add(title, description, due)
        self.title_var.set("")
        self.description_text.delete("1.0", "end")
        self._reset_due_fields()
        self.refresh(force=True)
        self.status_var.set("Напоминание добавлено.")

    def _selected_id(self) -> int | None:
        selected = self.tree.selection()
        if not selected:
            return None
        return int(selected[0])

    def _require_id(self) -> int | None:
        reminder_id = self._selected_id()
        if reminder_id is None:
            messagebox.showinfo("Напоминание", "Выберите напоминание в списке.", parent=self.root)
        return reminder_id

    def _set_status(self, status: str) -> None:
        reminder_id = self._require_id()
        if reminder_id is None:
            return
        if not self.db.update_status(reminder_id, status):
            messagebox.showwarning("Напоминание", "Запись уже удалена.", parent=self.root)
            self.refresh(force=True)
            return
        self.refresh(force=True)
        self.status_var.set(f"Напоминание отмечено: {status}.")

    def _delete(self) -> None:
        reminder_id = self._require_id()
        if reminder_id is None:
            return
        row = self.db.get(reminder_id)
        title = row["title"] if row else "напоминание"
        if not messagebox.askyesno("Удаление", f"Удалить «{title}»?", parent=self.root):
            return
        self.db.delete(reminder_id)
        self.refresh(force=True)
        self.status_var.set("Напоминание удалено.")

    def _filter_status(self) -> str | None:
        value = self.filter_var.get()
        if value == "Все":
            return None
        return value

    def refresh(self, force: bool = False) -> None:
        rows = self.db.list_reminders(self._filter_status())
        signature = tuple(
            (row["id"], row["title"], row["description"], row["due_at"], row["status"])
            for row in rows
        )
        if not force and signature == self._rows_signature:
            return
        selected = self._selected_id()
        self._rows_signature = signature
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(row["title"], format_due(row["due_at"]), row["status"]),
                tags=(row["status"],),
            )
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
            self.tree.focus(str(selected))
        self._on_select()

    def _on_select(self, _event: object = None) -> None:
        reminder_id = self._selected_id()
        row = self.db.get(reminder_id) if reminder_id is not None else None
        if row is None:
            text = "Выберите напоминание, чтобы увидеть описание."
        else:
            description = row["description"].strip() or "Без описания"
            text = f"{row['title']}\n{format_due(row['due_at'])} · {row['status']}\n\n{description}"
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _on_close(self) -> None:
        try:
            self.db.close()
        finally:
            self.root.destroy()


def main() -> None:
    ReminderApp().run()


if __name__ == "__main__":
    main()
