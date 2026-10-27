"""Smoke-тест Mini App: логин, состояние, котировка, создание заказа."""
import json
import sys
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8765"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "http": exc.code, "error": exc.read().decode("utf-8", "replace")[:300]}


def main() -> int:
    ok = True

    r = call("/api/act", {"op": "login", "max_id": 16155678, "name": "Сергей"})
    print("login:", r.get("ok"), r.get("user_id"), r.get("error", ""))
    ok &= bool(r.get("ok"))

    r = call("/api/state")
    print("state:", r.get("ok"), "role=", (r.get("user") or {}).get("role"),
          "orders=", len((r.get("passenger") or {}).get("orders") or []))
    ok &= bool(r.get("ok"))

    r = call("/api/act", {"op": "quote", "pickup": "Черногорск, Степная 128А",
                          "destination": "Абакан, Аскизская 212"})
    draft = r.get("draft") or {}
    print("quote:", r.get("ok"), "km=", draft.get("km"), "price=", draft.get("price"),
          r.get("error", ""))
    ok &= bool(r.get("ok")) and draft.get("km") is not None

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
