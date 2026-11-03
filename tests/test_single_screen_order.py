"""Одноэкранный заказ: статическая проверка потока в фронтенде."""

import re
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"


class SingleScreenOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.lines = cls.src.splitlines()

    def _block(self, marker):
        """Тело функции/экрана по балансу скобок, начиная с marker."""
        start = self.src.find(marker)
        self.assertGreaterEqual(start, 0, f"не найден {marker}")
        depth = 0
        started = False
        for i in range(start, len(self.src)):
            ch = self.src[i]
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    return self.src[start:i + 1]
        return self.src[start:]

    def test_single_screen_has_both_fields(self):
        trip = self._block("trip: function () {")
        self.assertIn('field("pickup"', trip)
        self.assertIn('field("dest"', trip)
        # Никаких пошаговых кнопок «Дальше» — только «Заказать».
        self.assertNotIn("pickup-next", trip)
        self.assertNotIn("dest-next", trip)
        # Кнопка заказа берётся из общего блока итога.
        self.assertIn("tripSummaryHtml()", trip)
        summary = self._block("function tripSummaryHtml() {")
        self.assertIn('data-act="confirm"', summary)

    def test_pickup_prefilled_from_history_only(self):
        self.assertIn("function prefillPickup", self.src)
        start = self._block("start: function () {")
        self.assertIn("prefillPickup()", start)
        # Геолокация устройства не используется вовсе.
        self.assertNotIn("navigator.geolocation", self.src)
        prefill = self._block("function prefillPickup() {")
        self.assertIn("passenger.orders", prefill)
        self.assertIn("completed", prefill)

    def test_no_geo_controls_left(self):
        # Ни карты, ни кнопок «Моё место» — апп их не обрабатывает.
        for token in ("geoBtn", "my-geo", "geo-map-ok", "geo-zoom",
                      "mountGeoMap", "openGeoMap", "useMyLocation", "_geoPoints"):
            self.assertNotIn(token, self.src, f"остался гео-элемент: {token}")

    def test_price_calculated_automatically(self):
        self.assertIn("function quoteDraft", self.src)
        # Расчёт запускается сам при вводе адреса.
        self.assertIn("autoQuote", self.src)
        self.assertIn('op: "quote"', self.src)

    def test_stops_are_inline_not_modal(self):
        trip = self._block("trip: function () {")
        self.assertIn('data-act="of-add-stop"', trip)
        self.assertIn('data-act="of-del-stop"', trip)
        self.assertNotIn("sheet-stop", trip)

    def test_confirm_sends_stops_and_price(self):
        confirm = self._block("confirm: async function () {")
        self.assertIn("draftStops()", confirm)
        self.assertIn("order.create", confirm)
        self.assertIn("from_favorite", confirm)

    def test_city_hint_is_sent(self):
        quote = self._block("function quoteDraft(force, silent) {")
        self.assertIn("pickup_city", quote)
        self.assertIn("home_city", quote)


if __name__ == "__main__":
    unittest.main()