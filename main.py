
"""Запуск напоминалки для Windows.

    python main.py

База SQLite создаётся рядом с программой: reminders.db.
Окно можно свернуть — срок напоминаний проверяется, пока процесс запущен.
"""

from app import main


if __name__ == "__main__":
    main()
