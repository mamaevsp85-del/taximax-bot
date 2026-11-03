"""Статическая проверка экранов и обработчиков Mini-App водителя.

Главный смысл: обработчик клика (data-act) должен лежать в объекте
``actions``, а экран — в ``views``. Раньше ``d-day-open`` оказался внутри
``views``, поэтому клик по столбику графика ничего не делал.
"""
import re
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"


def _block(lines, marker):
    """(начало, конец) объекта `marker = {` по балансу скобок."""
    start = None
    for i, line in enumerate(lines):
        if re.match(r"\s*" + re.escape(marker) + r"\s*=\s*\{", line):
            start = i
            break
    assert start is not None, f"не найден {marker}"
    depth = 0
    quote = ""
    escaped = False
    for i in range(start, len(lines)):
        for ch in lines[i]:
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
                continue
            if ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return start, i
    return start, len(lines)


def _keys(lines, start, end):
    keys = set()
    for line in lines[start + 1:end]:
        m = re.match(r'^    "?([A-Za-z0-9\-_]+)"?\s*:', line)
        if m:
            keys.add(m.group(1))
    return keys


class DriverStatsScreensTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.lines = cls.src.splitlines()
        v0, v1 = _block(cls.lines, "var views")
        # Первый var actions — строка `var actions = "";` (пустая заготовка),
        # настоящий объект объявлен ниже.
        a0 = a1 = None
        for i, line in enumerate(cls.lines):
            if re.match(r"\s*var actions\s*=\s*\{", line):
                a0, a1 = _block(cls.lines, "var actions")
                a0 = i
                break
        assert a0 is not None, "не найден объект actions"
        cls.views = _keys(cls.lines, v0, v1)
        cls.actions = _keys(cls.lines, a0, a1)
        cls.acts_used = set(re.findall(r'data-act="([A-Za-z0-9\-_]+)"', cls.src))

    def test_click_handlers_live_in_actions(self):
        # Клики: обработчик обязан быть в actions, иначе кнопка «мертва».
        for handler in ("d-day-open", "d-today-open", "d-period-orders",
                        "d-orders-mode", "d-period", "tab", "d-cancel"):
            self.assertIn(handler, self.actions, f"нет обработчика {handler}")
            self.assertNotIn(handler, self.views,
                             f"{handler} лежит в views — клик не сработает")

    def test_screens_live_in_views(self):
        for screen in ("driver", "d-today", "d-day-summary", "d-period-list",
                       "d-today-detail", "d-orders", "d-current"):
            self.assertIn(screen, self.views, f"нет экрана {screen}")

    def test_every_data_act_has_handler(self):
        missing = sorted(a for a in self.acts_used if a not in self.actions)
        self.assertEqual(missing, [], f"кнопки без обработчика: {missing}")

    def test_chart_opens_day_with_orders(self):
        # Столбик графика ведёт на итог дня и запрашивает driver.day.
        self.assertIn('data-act="d-day-open"', self.src)
        self.assertIn('op: "driver.day"', self.src)
        # В итогах дня сразу показываются заявки (а не только кнопка).
        summary = self.src.split('"d-day-summary": function () {', 1)[1]
        summary = summary.split('"d-period-orders": function () {', 1)[0]
        self.assertIn("Заявки за день", summary)
        self.assertIn("d-today-open", summary)

    def test_chart_count_opens_period_orders(self):
        # «Заявок» под графиком — кнопка: все заявки за период.
        self.assertIn('data-act="d-period-orders"', self.src)
        self.assertIn('op: "driver.period"', self.src)
        self.assertIn('"d-period-list": function () {', self.src)

    def test_order_card_has_money_breakdown_and_write(self):
        detail = self.src.split('"d-today-detail": function () {', 1)[1]
        # Обрезаем по следующему экрану, иначе захватим весь остальной файл.
        cut = detail.find('\n    "d-')
        if cut < 0:
            cut = detail.find('\n    "')
        detail = detail[:cut]
        self.assertIn("Комиссия", detail)
        self.assertIn("Заработок", detail)
        self.assertIn('data-act="sheet-chat"', detail)
        # Телефон пассажира в деталях больше не показываем.
        self.assertNotIn("passenger_phone", detail)


if __name__ == "__main__":
    unittest.main()