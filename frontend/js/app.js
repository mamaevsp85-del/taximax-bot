(function () {
  var S = null;
  var screen = "home";
  var stack = [];
  var draft = null;
  var picked = null;
  var period = "today";
  var driverPeriod = "day";

  // ==================== ЗАСТАВКА ====================
  var splashEl = document.getElementById("splash");
  var splashSub = document.getElementById("splash-sub");
  var SPLASH_TEXT = {
    passenger: ["Готовим машину…", "Строим маршрут…", "Ищем водителя поблизости…"],
    driver: ["Выходим на линию…", "Загружаем заказы…", "Проверяем доступ…"],
    admin: ["Загружаем панель…", "Считаем статистику…", "Подключаем водителей…"]
  };
  var splashTimer = null;

  function setSplashRole(role) {
    if (!splashEl) return;
    var safe = SPLASH_TEXT[role] ? role : "passenger";
    if (splashEl.dataset.role === safe) return;
    splashEl.dataset.role = safe;
    document.body.dataset.role = safe;
    var lines = SPLASH_TEXT[safe];
    var i = 0;
    if (splashSub) splashSub.textContent = lines[0];
    clearInterval(splashTimer);
    splashTimer = setInterval(function () {
      i = (i + 1) % lines.length;
      if (splashSub) {
        splashSub.style.opacity = "0";
        setTimeout(function () {
          splashSub.textContent = lines[i];
          splashSub.style.opacity = "1";
        }, 160);
      }
    }, 1300);
  }

  function hideSplash() {
    if (!splashEl || splashEl.classList.contains("hide")) return;
    clearInterval(splashTimer);
    splashEl.classList.add("hide");
    setTimeout(function () {
      if (splashEl && splashEl.parentNode) splashEl.parentNode.removeChild(splashEl);
      splashEl = null;
    }, 620);
  }

  // Стартовая роль из диплинка — до получения состояния
  (function () {
    var params = new URLSearchParams(window.location.search);
    var sp = (window.Maximus && window.Maximus.startParam && window.Maximus.startParam())
             || params.get("startapp") || "";
    if (SPLASH_TEXT[sp]) setSplashRole(sp);
  })();

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function pickupLine(d) {
    if (!d) return "";
    var text = d.pickup || "";
    var entrance = String(d.entrance || "").trim();
    if (!entrance || text.toLowerCase().indexOf("подъезд") >= 0) return text;
    return text + ", подъезд " + entrance;
  }
  function money(n) {
    if (n == null || n === "") return "…";
    return Math.round(Number(n) || 0) + " ₽";
  }
  function toast(text) {
    var el = $("toast");
    el.hidden = false;
    el.textContent = text;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.hidden = true; }, 2400);
  }
  function val(id) {
    var el = document.getElementById(id);
    return el ? el.value.trim() : "";
  }
  var STATUS = {
    pending: "Ищем водителя", accepted: "Водитель назначен", driver_arrived: "На месте",
    in_progress: "В пути", completed: "Завершён", cancelled: "Отменён"
  };
  function badge(status) {
    var cls = status === "completed" ? "ok" : status === "in_progress" ? "live" : status === "cancelled" ? "off" : "wait";
    return '<span class="badge ' + cls + '">' + esc(STATUS[status] || status) + "</span>";
  }
  function row(title, sub, act, data, icon) {
    var attrs = ' data-act="' + act + '"';
    if (data) Object.keys(data).forEach(function (k) { attrs += " data-" + k + '="' + esc(data[k]) + '"'; });
    return '<button class="menu-row"' + attrs + '><i>' + (icon || "›") + '</i><span class="txt"><b>' +
      esc(title) + "</b>" + (sub ? "<small>" + esc(sub) + "</small>" : "") + '</span><em>›</em></button>';
  }
  function group(title) {
    return '<p class="section-label">' + esc(title) + "</p>";
  }

  var inflight = 0;
  async function api(path, body) {
    inflight++;
    try {
      // Auto-add uid for /api/act calls
      if (body && path === "/api/act" && maxUser && maxUser.id && !body.uid && !body.user_id && !body.max_id) {
        body.uid = maxUser.id;
      }
      var res = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        cache: "no-store"
      });
      var data = await res.json();
      if (!data.ok) throw new Error(data.error || "Ошибка");
      return data;
    } finally {
      inflight--;
    }
  }
  function normalizeState(data) {
    if (!data || typeof data !== "object") return data;
    if (!data.user) data.user = {};
    data.user.roles = Array.isArray(data.user.roles) ? data.user.roles : [];
    if (!data.user.role) data.user.role = data.user.roles[0] || "passenger";
    data.passenger = data.passenger || {};
    data.driver = data.driver || {};
    data.admin = data.admin || {};
    if (!Array.isArray(data.passenger.orders)) data.passenger.orders = [];
    if (!Array.isArray(data.passenger.favorites)) data.passenger.favorites = [];
    if (!Array.isArray(data.driver.available)) data.driver.available = [];
    if (!Array.isArray(data.driver.history)) data.driver.history = [];
    if (!Array.isArray(data.driver.transactions)) data.driver.transactions = [];
    if (!data.driver.stats) data.driver.stats = {};
    if (!data.driver.tariffs) data.driver.tariffs = {};
    if (!data.admin.drivers) data.admin.drivers = [];
    if (!data.admin.passengers) data.admin.passengers = [];
    if (!data.admin.orders) data.admin.orders = [];
    if (!data.admin.pending) data.admin.pending = [];
    if (!data.admin.managers) data.admin.managers = [];
    if (!data.admin.admins) data.admin.admins = [];
    if (!data.admin.cities) data.admin.cities = [];
    if (!data.admin.commission) data.admin.commission = {};
    if (!data.user.docs) data.user.docs = {};
    return data;
  }

  async function refresh() {
    S = normalizeState(await api("/api/state"));
    render();
  }

  function predict(body) {
    if (!S || !S.user) return;
    var op = body.op;
    if (op === "driver.line") S.user.line = body.status;
    if (op === "order.cancel" && S.passenger && S.passenger.active && S.passenger.active.id === body.id) {
      S.passenger.active.status = "cancelled";
    }
    if (op === "order.price" && S.passenger && S.passenger.active && S.passenger.active.id === body.id) {
      S.passenger.active.price = body.price;
    }
    if (op === "driver.accept" && S.driver) {
      var found = (S.driver.available || []).filter(function (x) { return x.id === body.id; })[0];
      if (found) { found.status = "accepted"; S.driver.current = found; }
    }
    if (op === "driver.arrived" && S.driver && S.driver.current) S.driver.current.status = "driver_arrived";
    // driver.wait НЕ меняет статус заказа — только таймер ожидания.
    if (op === "driver.wait" && S.driver && S.driver.current) {
      S.driver.current.waiting = S.driver.current.waiting || {};
    }
    if (op === "driver.start" && S.driver && S.driver.current) S.driver.current.status = "in_progress";
    if (op === "driver.complete" && S.driver && S.driver.current) S.driver.current.status = "completed";
    if (op === "driver.cancel" && S.driver && S.driver.current && S.driver.current.id === body.id) {
      S.driver.current.status = "cancelled";
    }
  }
  async function run(body) {
    predict(body);
    render();
    try {
      // Pass user id for authentication
      if (maxUser && maxUser.id && !body.uid && !body.user_id) {
        body.uid = maxUser.id;
      }
      var data = await api("/api/act", body);
      if (data.state) S = normalizeState(data.state);
      render();
      return true;
    } catch (e) {
      toast(e.message);
      refresh().catch(function () {});
      return false;
    }
  }

  function go(name) {
    if (screen !== name) stack.push(screen);
    screen = name;
    render();
  }
  function homeOf() {
    if (!S) return "login";
    if (S.user.role === "driver") return "driver";
    if (S.user.role === "admin") return "admin";
    return "home";
  }

  // Знакомство — ровно один раз при первом входе
  function needOnboarding() {
    return !!(S && S.user && S.user.needs_onboarding === true);
  }

  function entryScreen() {
    if (needOnboarding()) {
      if (!window._onbStep) window._onbStep = 1;
      return "welcome";
    }
    return homeOf();
  }

  function header() {
    if (!S) return '<div class="brand"><img src="assets/logo.jpg" alt=""><div class="word">Maximus<small>TAXI</small></div></div>';
    var title = S.user.role === "driver" ? "Водитель" : S.user.role === "admin" ? "Админ" : "Пассажир";
    return '<div class="brand"><img src="assets/logo.jpg" alt=""><div class="word">Maximus<small>TAXI</small></div></div>' +
      '<div class="top-side"><button class="chip" data-act="roles">' + title + "</button></div>";
  }
  function tabs() {
    if (!S) return "";
    var items = S.user.role === "driver"
      ? [["driver", "◉", "Линия"], ["d-orders", "☰", "Заказы"], ["d-current", "🚕", "Рейс"], ["d-profile", "☺", "Профиль"]]
      : S.user.role === "admin"
        ? [["admin", "▣", "Меню"], ["a-drivers", "👥", "Водители"], ["a-orders", "☰", "Заказы"], ["a-more", "•••", "Ещё"]]
        : [["home", "⌂", "Главная"], ["trip", "🚕", "Поездка"], ["orders", "☰", "Заказы"], ["profile", "☺", "Профиль"]];
    return items.map(function (it) {
      return '<button class="' + (screen === it[0] ? "on" : "") + '" data-act="tab" data-screen="' + it[0] + '"><b>' + it[1] + "</b>" + it[2] + "</button>";
    }).join("");
  }
  function orderCard(o, act) {
    return '<button class="order" data-act="' + act + '" data-id="' + o.id + '"><div class="row">' + badge(o.status) +
      "<b>" + money(o.price) + '</b></div><div class="route" style="margin-top:8px">' + esc(o.pickup) +
      "<small>" + esc(o.destination) + "</small></div><div class=\"muted\" style=\"margin-top:6px\">#" + o.id + " · " +
      Number(o.distance || 0).toFixed(1) + " км</div></button>";
  }
  function orderById(id) {
    var pool = [].concat(
      (S && S.passenger && S.passenger.orders) || [],
      (S && S.passenger && S.passenger.active) ? [S.passenger.active] : [],
      (S && S.driver && S.driver.current) ? [S.driver.current] : [],
      (S && S.driver && S.driver.history) || []
    );
    for (var i = 0; i < pool.length; i++) {
      if (pool[i] && String(pool[i].id) === String(id)) return pool[i];
    }
    return null;
  }
  function starsText(n) {
    var count = Math.max(0, Math.min(5, Number(n) || 0));
    return new Array(count + 1).join("★");
  }

  // ==================== ДЕТАЛИ ПОЕЗДКИ ====================
  // Итоговая стоимость: после завершения цена пересчитывается
  // (final_price = поездка + ожидание).
  function orderPrice(o) {
    if (!o) return 0;
    var total = o.final_price;
    return total != null && total !== "" ? total : (o.price || 0);
  }
  function favoriteIndex(orderId) {
    var favs = (S && S.passenger && S.passenger.favorites) || [];
    for (var i = 0; i < favs.length; i++) {
      if (favs[i] && String(favs[i].order_id) === String(orderId)) return i;
    }
    return -1;
  }
  function isFavorite(orderId) {
    return favoriteIndex(orderId) >= 0;
  }
  function fmtDateTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    function two(n) { return (n < 10 ? "0" : "") + n; }
    return two(d.getDate()) + "." + two(d.getMonth() + 1) + "." + d.getFullYear() +
      " " + two(d.getHours()) + ":" + two(d.getMinutes());
  }
  function detailLine(label, value) {
    return '<div class="doc"><span>' + esc(label) + "</span><b>" + esc(value) + "</b></div>";
  }
  // Километры, время в пути, ожидание и итог
  function orderDetails(o) {
    var base = o.base_price != null && o.base_price !== "" ? o.base_price : o.price;
    var waiting = Number(o.waiting_cost || 0);
    var rows = detailLine("Расстояние", Number(o.distance || 0).toFixed(1) + " км") +
      detailLine("Время в пути", Number(o.time_minutes || 0) + " мин");
    if (waiting > 0) {
      rows += detailLine("Поездка", money(base)) + detailLine("Ожидание", money(waiting));
    }
    rows += detailLine("Итого", money(orderPrice(o)));
    if (o.completed_at) rows += detailLine("Завершена", fmtDateTime(o.completed_at));
    return rows;
  }
  function driverCard(o) {
    if (!o.driver_name) return "";
    var car = [o.car_model, o.car_number, o.car_color].filter(Boolean).join(" · ");
    return '<div class="card"><b>' + esc(o.driver_name) + '</b><p class="muted">' + esc(car) +
      (o.driver_phone ? "<br>" + esc(o.driver_phone) : "") + "</p></div>";
  }
  // Предложение сохранить маршрут в избранное
  function favoriteOffer(o) {
    var idx = favoriteIndex(o.id);
    if (idx >= 0) {
      return '<div class="card fav-offer"><div class="row"><b>⭐ Маршрут в избранном</b></div>' +
        '<p class="muted">В следующий раз закажете в один клик — без ввода адресов.</p>' +
        '<button class="btn ghost" data-act="use-fav" data-index="' + idx + '">🚕 Заказать снова</button></div>';
    }
    return '<div class="card fav-offer"><div class="row"><b>⭐ Сохранить маршрут?</b></div>' +
      '<p class="muted">Добавим адреса в избранное — заказ в один клик в следующий раз.</p>' +
      '<button class="btn yellow" data-act="fav-add" data-id="' + o.id + '">⭐ В избранное</button>' +
      '<button class="btn ghost" data-act="tab" data-screen="home">Не сейчас</button></div>';
  }
  function field(id, label, value, placeholder) {
    return '<label class="field"><span>' + label + '</span><input id="' + id + '" value="' + esc(value || "") + '" placeholder="' + esc(placeholder || "") + '"></label>';
  }
  function numField(id, label, value, placeholder) {
    var shown = value == null || value === "" ? "" : value;
    return '<label class="field"><span>' + label + '</span><input id="' + id + '" inputmode="decimal" value="' + esc(shown) + '" placeholder="' + esc(placeholder || "") + '"></label>';
  }
  function tariffInputs(t) {
    t = t || {};
    return numField("base_price", "Посадка, ₽", t.base_price, "100") +
      numField("included_km", "Включено км", t.included_km, "2") +
      numField("price_per_km", "Цена за км", t.price_per_km, "20") +
      numField("price_per_minute", "За минуту пути", t.price_per_minute, "2") +
      numField("min_price", "Минимальная цена", t.min_price, "50") +
      numField("day_surcharge_percent", "Дневная наценка, %", t.day_surcharge_percent, "0") +
      numField("night_surcharge_percent", "Ночная наценка, %", t.night_surcharge_percent, "0") +
      numField("night_start_hour", "Ночь с часа", t.night_start_hour != null ? t.night_start_hour : 22, "22") +
      numField("night_end_hour", "Ночь до часа", t.night_end_hour != null ? t.night_end_hour : 6, "6") +
      numField("waypoint_surcharge", "Точка по пути, ₽", t.waypoint_surcharge, "0");
  }
  function tariffBody(extra) {
    var body = extra || {};
    ["base_price", "included_km", "price_per_km", "price_per_minute", "min_price",
     "day_surcharge_percent", "night_surcharge_percent", "night_start_hour",
     "night_end_hour", "waypoint_surcharge"].forEach(function (k) {
      var raw = val(k);
      if (raw !== "") body[k] = Number(raw);
    });
    return body;
  }

  // ==================== ФОТО АВТОМОБИЛЯ (обязательно при регистрации) ====================
  // Фото можно приложить прямо здесь: выбираем файл, сжимаем на клиенте и
  // отправляем на /api/photo. Оно сохраняется в базе водителя и уходит
  // администратору вместе с заявкой.
  var _carPhoto = "";   // data-url выбранного файла (ещё не загруженного)

  function photoBlock(locked) {
    var has = !!(S && S.user && S.user.car_photo);
    if (has) {
      var src = (S.user.car_photo_url || "") + "?t=" + Date.now();
      return '<div class="card"><b>📷 Фото автомобиля</b>' +
        '<img src="' + src + '" alt="Авто" style="width:100%;border-radius:12px;margin-top:8px">' +
        '<p class="muted">Фото прикреплено' + (locked ? " и отправлено на проверку" : "") + '.</p>' +
        (locked ? "" : '<button class="btn ghost" data-act="pick-photo">Заменить фото</button>') +
        '</div>';
    }
    if (locked) {
      return '<div class="card"><b>📷 Фото автомобиля</b>' +
        '<p class="note">Фото не прикреплено — админ не сможет одобрить заявку.</p>' +
        '<button class="btn yellow" data-act="pick-photo">Прикрепить фото</button></div>';
    }
    var preview = _carPhoto ? '<img src="' + _carPhoto + '" alt="Авто" style="width:100%;border-radius:12px;margin-top:8px">' : "";
    return '<div class="card"><b>📷 Фото автомобиля *</b>' +
      '<p class="muted">Виден весь автомобиль и госномер. Без фото заявка не отправляется.</p>' +
      preview +
      '<input type="file" id="car_photo_file" accept="image/*" style="display:none">' +
      '<button class="btn ghost" data-act="pick-photo">' + (preview ? "Выбрать другое" : "Выбрать фото") + '</button>' +
      '</div>';
  }

  // Уменьшаем фото до 1280px по длинной стороне, чтобы не гнать мегабайты.
  function readPhoto(file, cb) {
    var reader = new FileReader();
    reader.onload = function (e) {
      var img = new Image();
      img.onload = function () {
        var max = 1280;
        var w = img.width, h = img.height;
        if (w > max || h > max) {
          var k = Math.min(max / w, max / h);
          w = Math.round(w * k); h = Math.round(h * k);
        }
        var canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        try { cb(canvas.toDataURL("image/jpeg", 0.82)); }
        catch (err) { cb(e.target.result); }
      };
      img.onerror = function () { cb(e.target.result); };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  }

  async function uploadCarPhoto(dataUrl) {
    var res = await fetch("/api/photo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: dataUrl, uid: (maxUser && maxUser.id) || 0 })
    });
    var data = await res.json();
    if (!data.ok) throw new Error(data.error || "Не удалось загрузить фото");
    return data;
  }

  // ==================== ТАЙМЕР ОЖИДАНИЯ (у водителя и пассажира) ====================
  // Показывается, когда водитель нажал «Я прибыл». Отсчёт идёт локально
  // каждую секунду, чтобы цифры «тикали» между опросами состояния.
  var _waitTimer = null;

  function waitingCard(o, role) {
    var w = (o && o.waiting) || {};
    var running = !!w.running;
    var secs = Number(w.seconds || 0);
    if (!running && !secs) return "";
    var startedAt = w.started_at ? new Date(w.started_at).getTime() : 0;
    var freeMin = Number(w.free_minutes || 3);
    var perMin = Number(w.per_minute || 5);
    var isFree = secs <= freeMin * 60;
    var cost = Number(w.cost || 0);
    var title = role === "driver"
      ? "Пассажир садится — идёт ожидание"
      : "Водитель ждёт вас";
    var note = isFree
      ? "Бесплатно ещё " + Math.max(0, Math.ceil(freeMin - secs / 60)) + " мин"
      : "Платно: " + perMin + " ₽/мин · начислено " + Math.round(cost) + " ₽";
    var mm = Math.floor(secs / 60);
    return '<div class="wait-card" id="wait-card" data-started="' + (startedAt || "") +
      '" data-free="' + freeMin + '" data-per="' + perMin + '" data-base="' + secs + '">' +
      '<div class="wait-card-top"><b>⏳ ' + esc(title) + '</b></div>' +
      '<div class="wait-timer" id="wait-timer">' + mm + ":" +
      ("0" + (Math.floor(secs) % 60)).slice(-2) + '</div>' +
      '<div class="wait-note" id="wait-note">' + esc(note) + '</div>' +
      '</div>';
  }

  function tickWaiting() {
    var card = document.getElementById("wait-timer");
    var box = document.getElementById("wait-card");
    if (!card || !box) return;
    var started = Number(box.dataset.started || 0);
    var base = Number(box.dataset.base || 0);
    var freeMin = Number(box.dataset.free || 3);
    var perMin = Number(box.dataset.per || 5);
    var secs = started ? Math.max(0, (Date.now() - started) / 1000) : base;
    card.textContent = Math.floor(secs / 60) + ":" +
      ("0" + (Math.floor(secs) % 60)).slice(-2);
    var note = document.getElementById("wait-note");
    if (note) {
      if (secs <= freeMin * 60) {
        note.textContent = "Бесплатно ещё " +
          Math.max(0, Math.ceil(freeMin - secs / 60)) + " мин";
      } else {
        var cost = Math.round(((secs - freeMin * 60) / 60) * perMin);
        note.textContent = "Платно: " + perMin + " ₽/мин · начислено " + cost + " ₽";
      }
    }
  }

  function startWaitingTicker() {
    if (_waitTimer) return;
    _waitTimer = setInterval(tickWaiting, 1000);
  }

  // ==================== СВОЙ ЗВУК УВЕДОМЛЕНИЯ ====================
  // Системный звук push в MAX не настраивается. Поэтому, пока приложение
  // открыто, играем собственный двухтональный сигнал «такси ждёт»
  // (Web Audio API) — он отличается от обычной вибрации.
  // Плюс дублируем вибрацией, если она доступна.
  var _audioCtx = null;
  var _lastAlertKey = "";

  function taxiBeep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      _audioCtx = _audioCtx || new Ctx();
      if (_audioCtx.state === "suspended") _audioCtx.resume();
      var t = _audioCtx.currentTime;
      // Длинная громкая трель такси — 2 цикла, ~3.5 сек, громкость 90%.
      // [частота, длительность, задержка от старта]
      [[880, 0.30, 0], [1175, 0.35, 0.31], [1568, 0.50, 0.75],
       [880, 0.30, 1.45], [1175, 0.35, 1.76], [1568, 0.60, 2.20]].forEach(function (n) {
        var osc = _audioCtx.createOscillator();
        var gain = _audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = n[0];
        var start = t + n[2];
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.9, start + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + n[1]);
        osc.connect(gain); gain.connect(_audioCtx.destination);
        osc.start(start); osc.stop(start + n[1] + 0.03);
      });
    } catch (e) { return false; }
    try {
      if (navigator.vibrate) navigator.vibrate([250, 120, 250, 120, 350, 120, 250]);
    } catch (e) {}
    return true;
  }

  // Один и тот же алерт не повторяем на каждом автообновлении.
  function alertOnce(key, text) {
    if (_lastAlertKey === key) return;
    _lastAlertKey = key;
    taxiBeep();
    showBanner(text, "err");
  }

  // Сколько минут заказ ищет водителя.
  function pendingMinutes(o) {
    if (!o || !o.created_at) return 0;
    var t = new Date(o.created_at).getTime();
    if (isNaN(t)) return 0;
    return (Date.now() - t) / 60000;
  }

  var views = {
    login: function (people) {
      return "<h2>Кто вы</h2><p class=\"muted\">Это живые профили из базы бота. В MAX откроется ваш аккаунт сам.</p>" +
        people.map(function (p) {
          return row(p.name, p.roles.join(" · "), "login", { id: p.id });
        }).join("");
    },
    roles: function () {
      var names = { passenger: ["Пассажир", "Заказ, история, избранное"], driver: ["Водитель", "Линия, рейсы, заработок"], admin: ["Админ", "Водители, заказы, настройки"] };
      return "<h2>Выберите роль</h2>" + S.user.roles.map(function (r) {
        var n = names[r] || [r, ""];
        return '<button class="role-card' + (S.user.role === r ? " on" : "") + '" data-act="role" data-role="' + r + '"><span class="avatar">' +
          esc(n[0][0]) + "</span><span><b>" + n[0] + '</b><br><span class="muted">' + n[1] + "</span></span></button>";
      }).join("");
    },
    home: function () {
      var p = S.passenger;
      var active = p.active;
      var chips = (p.favorites || []).slice(0, 4).map(function (f, i) {
        return '<button data-act="use-fav" data-index="' + i + '">' + esc((f.label || "Маршрут").slice(0, 18)) + "</button>";
      }).join("");
      return '<section class="hero"><p class="eyebrow">Куда поедем</p>' +
        '<button class="where" data-act="start"><span class="pin"></span><span>Создать поездку</span><span class="go">→</span></button>' +
        '<div class="chips">' + chips + "</div></section>" +
        (active ? '<div class="card"><div class="row"><b>Заказ #' + active.id + "</b>" + badge(active.status) + "</div>" +
          '<button class="btn yellow" data-act="open-order" data-id="' + active.id + '">Открыть</button></div>' : "") +
        group("Меню") +
        '<div class="grid">' +
        tile("orders", "☰", "Мои заказы", "что сейчас в работе") +
        tile("history", "↺", "История", "прошлые поездки") +
        tile("favorites", "★", "Избранное", (p.favorites || []).length + " адреса") +
        tile("stats", "▦", "Статистика", "день, неделя, месяц") +
        tile("profile", "☺", "Профиль", S.user.name || "имя и телефон") +
        (S.user.roles.indexOf("driver") < 0 ? tile("become", "✚", "Стать водителем", "анкета на проверку") : "") +
        "</div>";
    },
    trip: function () {
      var step = (draft && draft.step) || 1;
      var bars = [1, 2, 3].map(function (n) { return '<i class="' + (n <= step ? "on" : "") + '"></i>'; }).join("");
      if (step === 1) return "<h2>Адрес подачи</h2><div class=\"steps\">" + bars + "</div>" + field("pickup", "Откуда", draft && draft.pickup, "Город, улица, дом") +
        geoBtn("pickup") +
        field("entrance", "Подъезд", draft && draft.entrance, "номер, если есть") +
        '<button class="btn" data-act="pickup-next">Дальше</button>';
      if (step === 2) return "<h2>Куда</h2><div class=\"steps\">" + bars + "</div><p class=\"muted\">Подача: " + esc(pickupLine(draft)) + "</p>" +
        field("dest", "Назначение", draft.destination, "Город, улица, дом") + geoBtn("dest") +
        '<button class="btn" data-act="dest-next">Посчитать</button>';
      if (draft.pending) return "<h2>Подтверждение</h2><div class=\"steps\">" + bars + "</div><div class=\"card\" style=\"margin-top:0\"><div class=\"route\">" +
        esc(draft.pickup) + "<small>" + esc(draft.destination) + "</small></div><p class=\"muted\">Считаю маршрут…</p></div>";
      var fromFav = draft.favoriteIndex != null
        ? '<button class="btn ghost" data-act="edit-fav" data-index="' + draft.favoriteIndex + '">Изменить адреса</button>'
        : "";
      return "<h2>Подтверждение</h2><div class=\"steps\">" + bars + "</div><div class=\"card\" style=\"margin-top:0\"><div class=\"route\">" +
        esc(pickupLine(draft)) + (draft.stop ? "<small>через " + esc(draft.stop) + "</small>" : "") + "<small>" + esc(draft.destination) +
        '</small></div><div class="row" style="margin-top:12px"><span class="muted">' + draft.km + " км · ~" + draft.minutes +
        ' мин</span><div class="price">' + money(draft.price) + "</div></div></div>" +
        '<button class="btn" data-act="confirm">Подтвердить заказ</button>' +
        '<button class="btn ghost" data-act="edit-route">Изменить маршрут</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="sheet-price">Своя цена</button><button class="btn ghost" data-act="sheet-stop">Остановка</button></div>' +
        fromFav;
    },
    live: function () {
      var o = (S.passenger.orders || []).filter(function (x) { return String(x.id) === String(picked); })[0] || S.passenger.active;
      if (!o) return "<h2>Заказ</h2><p class=\"muted\">Заказ не найден.</p>";
      var canCancel = ["pending", "accepted", "driver_arrived"].indexOf(o.status) >= 0;
      var canEditRoute = ["pending", "accepted", "driver_arrived", "in_progress"].indexOf(o.status) >= 0;
      var isDone = ["completed", "cancelled"].indexOf(o.status) >= 0;
      var driver = o.driver_name ? '<div class="card"><b>' + esc(o.driver_name) + '</b><p class="muted">' +
        esc([o.car_model, o.car_number, o.car_color].filter(Boolean).join(" · ")) + "<br>" + esc(o.driver_phone) + "</p>" +
        '<div class="btn-row"><button class="btn ghost" data-act="sheet-chat" data-id="' + o.id + '">Написать</button>' +
        '<button class="btn ghost" data-act="max-call" data-id="' + o.id + '" data-peer="' + esc(o.driver_id || "") + '" data-phone="' + esc(o.driver_phone) + '">📞 Позвонить через MAX</button></div></div>' : "";
      var waitBoxP = (o.status === "driver_arrived" || o.status === "in_progress")
        ? waitingCard(o, "passenger") : "";
      if (waitBoxP) setTimeout(startWaitingTicker, 0);
      return '<div class="row"><h2 style="margin:0">Заказ #' + o.id + "</h2>" + badge(o.status) + "</div>" +
        waitBoxP +
        '<div class="card"><div class="route">' + esc(o.pickup) + "<small>" + esc(o.destination) + "</small></div>" +
        '<div class="row" style="margin-top:10px"><span class="muted">' + Number(o.distance || 0).toFixed(1) +
        " км</span><b>" + money(orderPrice(o)) + "</b></div></div>" +
        (isDone ? orderDetails(o) : "") +
        driver +
        (canEditRoute ? '<div class="btn-row"><button class="btn ghost" data-act="sheet-live-dest" data-id="' + o.id + '">Изменить адрес</button>' +
          '<button class="btn ghost" data-act="sheet-live-stop" data-id="' + o.id + '">Добавить остановку</button></div>' : "") +
        (canCancel ? '<button class="btn ghost" data-act="sheet-price-live" data-id="' + o.id + '">Предложить свою цену</button>' +
          '<button class="btn ghost" data-act="sheet-complaint" data-id="' + o.id + '">Жалоба на авто</button>' +
          '<button class="btn danger" data-act="cancel" data-id="' + o.id + '">Отменить заказ</button>' : "") +
        (o.status === "completed" ? favoriteOffer(o) : "") +
        (o.status === "completed" && !o.rating ? '<button class="btn yellow" data-act="to-rate" data-id="' + o.id + '">Оценить поездку</button>' : "") +
        (o.rating ? '<p class="note">Ваша оценка: ' + esc(starsText(o.rating)) + "</p>" : "") +
        (o.complaint ? '<p class="note">Жалоба: ' + esc(o.complaint) + "</p>" : "");
    },
    receipt: function () {
      var o = orderById(picked) || (S.passenger && S.passenger.active);
      if (!o) return "<h2>Детали поездки</h2><p class=\"list-empty\">Заказ не найден</p>" +
        '<button class="btn ghost" data-act="tab" data-screen="home">На главную</button>';
      var done = o.status === "completed";
      var head = done
        ? '<div class="receipt-hero"><div class="receipt-check">✓</div><h1>Поездка завершена</h1>' +
          '<p class="muted">Заказ #' + o.id + (o.completed_at ? " · " + esc(fmtDateTime(o.completed_at)) : "") + "</p></div>"
        : '<div class="row"><h2 style="margin:0">Заказ #' + o.id + "</h2>" + badge(o.status) + "</div>";
      return head +
        '<div class="card"><div class="route">' + esc(o.pickup) + "<small>" + esc(o.destination) + "</small></div></div>" +
        orderDetails(o) +
        driverCard(o) +
        (done ? favoriteOffer(o) : "") +
        (done && !o.rating
          ? '<button class="btn yellow" data-act="to-rate" data-id="' + o.id + '">Оценить поездку</button>'
          : "") +
        (o.rating ? '<p class="note">Ваша оценка: ' + esc(starsText(o.rating)) + "</p>" : "") +
        (o.complaint ? '<p class="note">Жалоба: ' + esc(o.complaint) + "</p>" : "") +
        '<button class="btn ghost" data-act="tab" data-screen="home">Готово</button>';
    },
    orders: function () {
      var list = (S.passenger.orders || []).filter(function (o) { return ["pending", "accepted", "driver_arrived", "in_progress"].indexOf(o.status) >= 0; });
      return "<h2>Мои заказы</h2>" + (list.length ? list.map(function (o) { return orderCard(o, "open-order"); }).join("") : '<p class="list-empty">Активных заказов нет</p>');
    },
    // Вопрос, когда водителя нет дольше 10 минут — ждать или отменить
    "no-driver": function () {
      var o = orderById(picked) || (S.passenger && S.passenger.active) || {};
      var mins = Math.round(pendingMinutes(o));
      var waiting = !!o.passenger_waiting;
      return '<div class="onb-hero"><div class="onb-logo">⏳</div>' +
        "<h1>Водитель пока не найден</h1>" +
        '<p class="muted">Заказ #' + esc(o.id) + " ищет водителя уже " + mins + " мин. Как поступим?</p></div>" +
        (waiting
          ? '<p class="note">Вы выбрали «ждать» — продолжаем поиск. Цена может вырасти.</p>'
          : '<div class="card"><b>🕐 Ждать ещё</b><p class="muted">Продолжим искать водителя. Мы предупредим вас.</p></div>' +
            '<button class="btn yellow" data-act="no-driver-wait" data-id="' + esc(o.id) + '">Ждать ещё</button>') +
        '<button class="btn danger" data-act="no-driver-cancel" data-id="' + esc(o.id) + '">Отменить заказ</button>' +
        '<button class="btn ghost" data-act="tab" data-screen="home">На главную</button>';
    },
    history: function () {
      var list = (S.passenger.orders || []).filter(function (o) { return ["completed", "cancelled"].indexOf(o.status) >= 0; });
      return "<h2>История заказов</h2>" + (list.length ? list.map(function (o) { return orderCard(o, "open-order"); }).join("") : '<p class="list-empty">Поездок пока нет</p>');
    },
    stats: function () {
      var st = S.passenger.stats || {};
      var box = st[period] || { count: 0, sum: 0, km: 0 };
      return "<h2>Статистика</h2><div class=\"periods\">" +
        [["today", "Сегодня"], ["week", "Неделя"], ["month", "Месяц"]].map(function (p) {
          return '<button class="' + (period === p[0] ? "on" : "") + '" data-act="period" data-p="' + p[0] + '">' + p[1] + "</button>";
        }).join("") + '</div><div class="stat-grid"><div class="stat"><span class="muted">Поездок</span><b>' + box.count +
        '</b></div><div class="stat"><span class="muted">Потрачено</span><b>' + money(box.sum) +
        '</b></div><div class="stat"><span class="muted">Километры</span><b>' + box.km + "</b></div></div>";
    },
    favorites: function () {
      var favs = S.passenger.favorites || [];
      return "<h2>Избранное</h2>" + (favs.length ? favs.map(function (f, i) {
        var sub = [f.pickup, f.destination].filter(Boolean).join(" → ");
        if (f.entrance) sub = (f.pickup || "") + ", подъезд " + f.entrance + (f.destination ? " → " + f.destination : "");
        return '<div class="card"><b>' + esc(f.label || "Маршрут") + "</b>" +
          (sub ? '<p class="muted">' + esc(sub) + "</p>" : "") +
          '<div class="btn-row"><button class="btn" data-act="use-fav" data-index="' + i + '">Заказать</button>' +
          '<button class="btn ghost" data-act="edit-fav" data-index="' + i + '">Изменить</button>' +
          '<button class="btn danger" data-act="del-fav" data-index="' + i + '">Удалить</button></div></div>';
      }).join("") : '<p class="list-empty">Сохраните маршрут из завершённой поездки</p>');
    },
    welcome: function () {
      var u = S.user || {};
      var step = (window._onbStep || 1);
      var bars = [1, 2, 3].map(function (n) {
        return '<i class="' + (n <= step ? "on" : "") + '"></i>';
      }).join("");

      var head = '<div class="onb-hero">' +
        '<div class="onb-logo"><img src="assets/logo.jpg" alt=""></div>' +
        '<h1>Добро пожаловать в Maximus Taxi</h1>' +
        '<p class="muted">Давайте познакомимся — это займёт меньше минуты.</p>' +
        '</div><div class="steps">' + bars + '</div>';

      if (step === 1) {
        return head +
          '<h2>Как вас зовут?</h2>' +
          field("onb_name", "Имя", u.name || "", "Например, Александр") +
          '<button class="btn" data-act="onb-next">Далее</button>';
      }
      if (step === 2) {
        return head +
          '<h2>Ваш телефон</h2>' +
          '<p class="muted">Нужен, чтобы водитель мог связаться с вами.</p>' +
          field("onb_phone", "Телефон", u.phone || "", "+7 900 000-00-00") +
          '<button class="btn" data-act="onb-next">Далее</button>' +
          '<button class="btn ghost" data-act="onb-back">Назад</button>';
      }
      return head +
        '<h2>Ваш город</h2>' +
        '<p class="muted">Откуда вы обычно заказываете такси. Можно пропустить.</p>' +
        field("onb_city", "Город", u.home_city || "", "Например, Абакан") +
        '<button class="btn" data-act="onb-finish">Готово, поехали</button>' +
        '<button class="btn ghost" data-act="onb-back">Назад</button>';
    },
    profile: function () {
      return "<h2>Профиль</h2><div class=\"card\"><b>" + esc(S.user.name || "Без имени") + '</b><p class="muted">Телефон ' +
        esc(S.user.phone || "—") + "<br>Рейтинг " + S.user.rating + "</p>" +
        '<button class="btn ghost" data-act="sheet-profile">Изменить имя и телефон</button></div>' +
        (S.user.roles.indexOf("driver") < 0 ? '<button class="btn yellow" data-act="tab" data-screen="become">Стать водителем</button>' : "") +
        '<p class="note">Поддержка: ' + esc(S.passenger.support || "—") + "</p>";
    },
    become: function () {
      if (S.user.driver_status === "pending") {
        return "<h2>Заявка на проверке</h2><p class=\"muted\">Админ увидит её в модерации.</p>" +
          photoBlock(true);
      }
      if (S.user.driver_status === "approved") return "<h2>Вы уже водитель</h2><button class=\"btn\" data-act=\"role\" data-role=\"driver\">Открыть меню водителя</button>";
      var note = S.user.driver_status === "rejected" ? '<p class="note">Прошлая заявка отклонена: ' + esc(S.user.reject_reason || "без причины") + "</p>" : "";
      return "<h2>Анкета водителя</h2>" + note + field("car", "Марка и модель", "", "Kia Rio") +
        field("plate", "Госномер", "", "А123ВС 19") + field("color", "Цвет", "", "белый") +
        photoBlock(false) +
        '<button class="btn" data-act="apply">Отправить на проверку</button>';
    },
    rate: function () {
      var stars = [1, 2, 3, 4, 5].map(function (n) {
        return '<button data-act="star" data-n="' + n + '">' + (n <= (window._star || 0) ? "★" : "☆") + "</button>";
      }).join("");
      return "<h2>Оценка поездки #" + picked + '</h2><div class="stars">' + stars + '</div><button class="btn" data-act="save-rate">Сохранить</button>';
    },
    "fav-offer": function () {
      var oid = window._favOrder;
      var pool = (S.passenger && S.passenger.orders) || [];
      var o = pool.filter(function (x) { return x && x.id === oid; })[0] || {};
      var saved = !!(S.passenger.favorites || []).filter(function (f) {
        return Number(f.order_id) === Number(oid);
      }).length;
      return '<div class="onb-hero">' +
        '<div class="onb-logo">⭐</div>' +
        '<h1>Поездка завершена</h1>' +
        '<p class="muted">Сохраните маршрут, чтобы в следующий раз заказать его в один клик.</p>' +
        '</div>' +
        '<div class="card order-details">' +
          '<div class="row"><b>Заказ #' + oid + '</b>' + badge("completed") + '</div>' +
          '<div class="route" style="margin-top:14px">' + esc(o.pickup || "—") +
            '<small>' + esc(o.destination || "—") + '</small></div>' +
          '<div class="row" style="margin-top:14px"><span class="muted">Расстояние</span><b>' + Number(o.distance || 0).toFixed(1) + ' км</b></div>' +
          '<div class="row"><span class="muted">Время</span><b>' + (o.time_minutes || 0) + ' мин</b></div>' +
          '<div class="row"><span class="muted">Стоимость</span><b>' + money(o.final_price || o.price) + '</b></div>' +
          (o.completed_at ? '<div class="row"><span class="muted">Завершена</span><b>' + esc(fmtDateTime(o.completed_at)) + '</b></div>' : '') +
        '</div>' +
        driverCard(o) +
        (saved ? '<div class="note">⭐ Этот маршрут уже в избранном</div>' :
          '<button class="btn" data-act="fav-save">⭐ Сохранить маршрут в избранное</button>') +
        '<button class="btn ghost" data-act="go-receipt">📋 Детали поездки</button>' +
        '<button class="btn ghost" data-act="fav-skip">Продолжить</button>';
    },
    driver: function () {
      var u = S.user;
      var line = u.line === "online" ? "На линии" : u.line === "break" ? "Перерыв" : "Оффлайн";
      var today = (S.driver && S.driver.today) || {};
      var todayCount = today.count || 0;
      var todayEarn = today.earnings || 0;
      return '<div class="card" style="margin-top:0"><div class="row"><div><b>' + esc(u.driver_name || u.name) +
        '</b><div class="muted">' + esc([u.car_model, u.car_number].filter(Boolean).join(" · ")) + "</div></div>" +
        '<span class="badge ' + (u.line === "online" ? "ok" : "off") + '">' + line + "</span></div>" +
        '<div class="seg" style="margin-top:12px">' +
        seg("online", "На линии") + seg("break", "Перерыв") + seg("offline", "Оффлайн") + "</div></div>" +
        // Одна длинная кнопка «Сегодня»: и заработок, и количество заявок.
        // (Раньше было две отдельные колонки — убрали по просьбе.)
        '<button class="today-row" data-act="tab" data-screen="d-today">' +
        '<span class="today-left"><span class="muted">Сегодня</span>' +
        '<b class="today-sum">' + money(todayEarn) + "</b></span>" +
        '<span class="today-right"><span class="muted">Заявок</span>' +
        '<b class="today-count">' + todayCount + "</b></span>" +
        "</button>" +
        (S.driver.current ? orderCard(S.driver.current, "d-open") : "") +
        group("На линии") +
        '<div class="grid">' +
        tile("d-orders", "☰", "Список заказов", "свободные рядом") +
        tile("d-current", "▶", "Текущий рейс", S.driver.current ? "заказ #" + S.driver.current.id : "нет рейса") +
        tile("d-profile", "☺", "Профиль", "авто и документы") +
        "</div>";
    },
    "d-today": function () {
      var daily = (S.driver && S.driver.daily) || {};
      var periodKey = window._statPeriod || "week";
      var list = daily[periodKey] || [];
      var totalSum = 0, totalCount = 0;
      for (var i = 0; i < list.length; i++) {
        totalSum += Number(list[i].sum || 0);
        totalCount += Number(list[i].count || 0);
      }
      var labels = { day: "День", week: "Неделя", month: "Месяц" };
      var tabs = ["day", "week", "month"].map(function (k) {
        return '<button class="' + (periodKey === k ? "on" : "") +
          '" data-act="d-period" data-p="' + k + '">' + labels[k] + "</button>";
      }).join("");
      var maxSum = 0;
      for (var m = 0; m < list.length; m++) {
        maxSum = Math.max(maxSum, Number(list[m].sum || 0));
      }
      function dayNum(iso) {
        var parts = String(iso || "").split("-");
        return parts.length === 3 ? parts[2] : "";
      }
      var bars = list.map(function (b) {
        var sum = Number(b.sum || 0);
        var h = maxSum > 0 ? Math.max(4, Math.round((sum / maxSum) * 100)) : 4;
        return '<button class="bar-col" data-act="d-day-open" data-date="' + esc(b.date) + '">' +
          '<span class="bar-sum">' + (sum ? Math.round(sum) : "") + "</span>" +
          '<i class="bar" style="height:' + h + '%"></i>' +
          '<span class="bar-count">' + (b.count || 0) + "</span>" +
          '<span class="bar-day">' + esc(dayNum(b.date)) + "</span></button>";
      }).join("");
      return "<h2>Заработок</h2>" +
        '<div class="periods">' + tabs + "</div>" +
        (list.length
          ? '<div class="bar-chart">' + bars + "</div>" +
            '<div class="stat-grid chart-total">' +
              '<div class="stat"><span class="muted">За ' +
                labels[periodKey].toLowerCase() + '</span><b>' + money(totalSum) + "</b></div>" +
              '<div class="stat"><span class="muted">Заявок</span><b>' + totalCount + "</b></div></div>" +
            '<p class="muted" style="text-align:center">Нажмите на столбик — покажутся все заявки за этот день</p>'
          : '<div class="stat-grid"><div class="stat"><span class="muted">Сумма</span><b>' + money(totalSum) +
            "</b></div><div class=\"stat\"><span class=\"muted\">Заявок</span><b>" + totalCount +
            "</b></div></div><p class='list-empty'>Пока нет завершённых заявок</p>");
    },
    "d-day-open": function (el) {
      var day = el.dataset.date || "";
      window._statDay = day;
      window._statDayOrders = null;
      go("d-day-orders");
      api("/api/act", { op: "driver.day", date: day }).then(function (data) {
        if (screen !== "d-day-orders" || window._statDay !== day) return;
        window._statDayOrders = (data && data.orders) || [];
        render();
      }).catch(function (e) {
        if (screen !== "d-day-orders" || window._statDay !== day) return;
        window._statDayOrders = [];
        toast(e.message || "Не удалось загрузить заявки дня");
        render();
      });
    },
    "d-day-orders": function () {
      var day = window._statDay || "";
      var list = window._statDayOrders;
      var loading = !Array.isArray(list);
      if (loading) {
        // Подстраховка: если запрос ещё не вернулся, показываем то,
        // что уже есть в истории.
        var all = (S.driver && S.driver.history) || [];
        list = all.filter(function (o) {
          var ts = o.completed_at || o.created_at || "";
          return ts.slice(0, 10) === day;
        });
        if (!list.length) {
          return "<h2>" + (day || "Заявки") + "</h2>" +
            '<p class="list-empty">Загружаем заявки дня…</p>';
        }
      }
      var sum = list.reduce(function (acc, o) {
        return acc + Number(o.final_price || o.price || 0);
      }, 0);
      return "<h2>" + (day || "Заявки") + "</h2>" +
        '<div class="stat-grid"><div class="stat"><span class="muted">Заработано</span><b>' +
        money(sum) + '</b></div><div class="stat"><span class="muted">Заявок</span><b>' +
        list.length + "</b></div></div>" +
        (list.length ? list.map(function (o) {
          return orderCard(o, "d-today-open");
        }).join("") : '<p class="list-empty">За этот день заявок нет</p>') +
        '<button class="btn ghost" data-act="tab" data-screen="d-today">Назад к графику</button>';
    },
    "d-today-detail": function () {
      var pool = [].concat(window._statDayOrders || []);
      var o = orderById(picked) || pool.filter(function (x) { return x.id === picked; })[0] ||
        ((S.driver.today_orders || []).filter(function (x) { return x.id === picked; })[0]);
      if (!o) return "<h2>Заявка</h2><p class=\"list-empty\">Не найдена</p>" +
        '<button class="btn ghost" data-act="tab" data-screen="d-today">Назад</button>';
      var stops = (o.waypoints || []).map(function (w) {
        return typeof w === "string" ? w : (w.text || w.address || "");
      }).filter(Boolean);
      return '<div class="row"><h2 style="margin:0">Заявка #' + o.id + "</h2>" + badge(o.status) + "</div>" +
        '<div class="card"><div class="route">' + esc(o.pickup) +
        (stops.length ? stops.map(function (s) { return "<small>⏸ " + esc(s) + "</small>"; }).join("") : "") +
        "<small>" + esc(o.destination) + "</small></div>" +
        '<div class="row" style="margin-top:12px"><span class="muted">Расстояние</span><b>' + Number(o.distance || 0).toFixed(1) + " км</b></div>" +
        '<div class="row"><span class="muted">Время</span><b>' + (o.time_minutes || 0) + " мин</b></div>" +
        '<div class="row"><span class="muted">Стоимость</span><b>' + money(o.final_price || o.price) + "</b></div>" +
        (Number(o.waiting_cost || 0) > 0 ? '<div class="row"><span class="muted">Ожидание</span><b>' + money(o.waiting_cost) + "</b></div>" : "") +
        (o.completed_at ? '<div class="row"><span class="muted">Завершена</span><b>' + esc(fmtDateTime(o.completed_at)) + "</b></div>" : "") +
        "</div>" +
        '<button class="btn ghost" data-act="tab" data-screen="d-today">К списку</button>';
    },
    "d-orders": function () {
      var list = (S.driver && S.driver.available) || [];
      var waiting = Number(S.driver.pending_waiting || 0);
      var head = '<div class="card"><b>Ждут водителя сейчас: ' + waiting + "</b>" +
        '<p class="muted">' + (waiting
          ? "Эти заказы ищут водителя. Ваших доступных: " + list.length
          : "Новых заказов нет — как только появятся, они будут здесь") +
        "</p></div>";
      if (S.user.line !== "online") {
        return "<h2>Список заказов</h2>" + head +
          '<p class="list-empty">Сначала выйдите на линию, чтобы видеть заказы и принимать их</p>';
      }
      return "<h2>Список заказов</h2>" + head +
        (list.length ? list.map(function (o) {
          return '<div class="card">' + orderCard(o, "noop") + '<button class="btn" data-act="accept" data-id="' + o.id + '">Принять</button></div>';
        }).join("") : '<p class="list-empty">Свободных заказов нет</p>');
    },
    "d-current": function () {
      var o = S.driver.current;
      if (!o) return "<h2>Текущие заказы</h2><p class=\"list-empty\">Нет активного рейса</p>";
      var actions = "";
      if (o.status === "accepted") actions = '<button class="btn" data-act="arrived" data-id="' + o.id + '">Я прибыл</button>';
      if (o.status === "driver_arrived") actions = '<button class="btn" data-act="start-trip" data-id="' + o.id + '">Начать движение</button>';
      // В ПУТИ: нажали «Ожидание» — кнопки «Завершить поездку» и «Ожидание»
      // исчезают, появляется «Продолжить поездку». По ней возвращаются
      // «Ожидание» и «Завершить поездку».
      if (o.status === "in_progress") {
        var wNow = o.waiting || {};
        if (wNow.running) {
          actions = '<button class="btn" data-act="resume-trip" data-id="' + o.id + '">▶ Продолжить поездку</button>';
        } else {
          actions = '<button class="btn" data-act="wait" data-id="' + o.id + '">⏳ Ожидание</button>' +
            '<button class="btn" data-act="complete" data-id="' + o.id + '">Завершить поездку</button>';
        }
      }
      var waitBox = (o.status === "driver_arrived" || o.status === "in_progress")
        ? waitingCard(o, "driver") : "";
      if (waitBox) setTimeout(startWaitingTicker, 0);
      // Водитель маршрут не меняет: адрес/остановки правит только пассажир.
      var routeEdit = "";
      return '<div class="row"><h2 style="margin:0">Рейс #' + o.id + "</h2>" + badge(o.status) + "</div>" +
        waitBox +
        '<div class="card"><div class="route">' + esc(o.pickup) + "<small>" + esc(o.destination) + "</small></div>" +
        '<p class="muted">Пассажир ' + esc(o.passenger_name) + " · " + esc(o.passenger_phone) + "</p><b>" + money(o.price) + "</b></div>" +
        routeEdit +
        actions +
        '<div class="btn-row">' +
          '<button class="btn ghost" data-act="nav" data-q="' + esc(o.status === "in_progress" ? o.destination : o.pickup) + '">Навигация</button>' +
          '<button class="btn ghost" data-act="sheet-chat" data-id="' + o.id + '">Написать</button>' +
        '</div>' +
        '<button class="btn ghost" data-act="max-call" data-id="' + o.id + '" data-peer="' + esc(o.passenger_id || "") + '" data-phone="' + esc(o.passenger_phone || "") + '">📞 Позвонить через MAX</button>' +
        (["accepted", "driver_arrived", "in_progress", "pending"].indexOf(o.status) >= 0 ? '<button class="btn danger" data-act="d-cancel" data-id="' + o.id + '">Отменить заказ</button>' : "");
    },
    "d-profile": function () {
      var today = (S.driver && S.driver.today) || {};
      // Баланс показываем только при включённой комиссии (платные смены).
      // Сейчас работаем без комиссии — баланс не нужен.
      var showBalance = !!(S.user.commission_enabled && !S.user.balance_hidden);
      return "<h2>Профиль</h2><div class=\"card\"><b>" + esc(S.user.driver_name) + '</b><p class="muted">' +
        esc(S.user.driver_phone) + "<br>" + esc(S.user.car_model) + " · " + esc(S.user.car_number) + " · " + esc(S.user.car_color) +
        (showBalance ? "<br>Баланс " + money(S.user.balance) : "") +
        "<br>Сегодня " + money(today.earnings || 0) + " · заявок " + (today.count || 0) + "</p></div>" +
        group("Машина") +
        '<div class="grid">' +
        tile("d-car", "▣", "Автомобиль", "марка, номер, цвет") +
        tile("d-docs", "▣", "Документы", "фото для проверки") +
        tile("d-today", "💵", "Сегодня", (today.count || 0) + " заявок") +
        tile("d-stats", "▦", "Статистика", "день и месяц") +
        tile("d-history", "↺", "История", "закрытые рейсы") +
        "</div>" +
        group("Аккаунт") +
        row("Имя", S.user.driver_name, "sheet-dname", null, "☺") +
        row("Телефон", S.user.driver_phone, "sheet-dphone", null, "#") +
        (showBalance ? row("Баланс", money(S.user.balance), "tab", { screen: "d-balance" }, "₽") : "") +
        row("Помощь", "как устроена линия", "help", null, "?");
    },
    "d-car": function () {
      return "<h2>Автомобиль</h2>" + row("Марка и модель", S.user.car_model, "sheet-car", { field: "car" }) +
        row("Госномер", S.user.car_number, "sheet-car", { field: "plate" }) +
        row("Цвет", S.user.car_color, "sheet-car", { field: "color" });
    },
    "d-docs": function () {
      var names = { selfie: "Селфи", left: "Авто слева", right: "Авто справа", front: "Авто спереди", back: "Авто сзади", salon: "Салон" };
      return "<h2>Документы</h2>" + Object.keys(names).map(function (k) {
        return row(names[k], S.user.docs[k] ? "загружено" : "добавить", "doc", { key: k });
      }).join("") + '<p class="muted">Отметка сохраняется в базе бота. Само фото по-прежнему присылается в чат.</p>';
    },
    "d-stats": function () {
      var map = { day: "day", week: "week", month: "month", all: "all" };
      var box = (S.driver.stats || {})[driverPeriod] || {};
      return "<h2>Статистика</h2><div class=\"periods\">" +
        [["day", "Сегодня"], ["week", "Неделя"], ["month", "Месяц"], ["all", "Всё"]].map(function (p) {
          return '<button class="' + (driverPeriod === p[0] ? "on" : "") + '" data-act="dperiod" data-p="' + p[0] + '">' + p[1] + "</button>";
        }).join("") + '</div><div class="stat-grid"><div class="stat"><span class="muted">Рейсов</span><b>' +
        (box.orders_completed || 0) + '</b></div><div class="stat"><span class="muted">Сумма</span><b>' + money(box.revenue) + "</b></div></div>";
    },
    "d-history": function () {
      var list = S.driver.history || [];
      return "<h2>История заказов</h2>" + (list.length ? list.map(function (o) { return orderCard(o, "noop"); }).join("") : '<p class="list-empty">Пусто</p>');
    },
    "d-balance": function () {
      // Баланс нужен только при включённой комиссии (платные смены).
      if (!S.user.commission_enabled) {
        return "<h2>Баланс</h2><p class=\"muted\">Сейчас работаем без комиссии — " +
          "баланс и платные смены не используются.</p>";
      }
      var tariffs = S.driver.tariffs || {};
      var keys = Object.keys(tariffs);
      return "<h2>Баланс</h2><div class=\"price\">" + money(S.user.balance) + "</div>" +
        '<button class="btn" data-act="sheet-topup">Пополнить</button>' +
        keys.map(function (h) { return '<button class="btn ghost" data-act="buy" data-hours="' + h + '">' + h + " ч — " + money(tariffs[h]) + "</button>"; }).join("") +
        "<h2>История</h2>" + (S.driver.transactions || []).map(function (t) {
          return '<div class="doc"><span>' + esc(t.description || t.type) + "</span><b>" + money(t.amount) + "</b></div>";
        }).join("");
    },
    admin: function () {
      var a = S.admin;
      return "<h2>Админ</h2>" + group("Каждый день") +
        '<div class="grid">' +
        tile("a-orders", "☰", "Заказы", "лента и статусы") +
        tile("a-drivers", "▣", "Водители", a.drivers.length + " в базе") +
        tile("a-passengers", "☺", "Пассажиры", a.passengers.length + " профиля") +
        tile("a-moderation", "✓", "Модерация", a.pending.length ? a.pending.length + " ждут" : "заявок нет") +
        "</div>" +
        group("Деньги") +
        '<div class="grid">' +
        tile("a-tariffs", "₽", "Тарифы", "цена за километр") +
        tile("a-commission", "%", "Комиссия", a.commission.enabled ? "включена" : "выключена") +
        tile("a-stats", "▦", "Статистика", "сводка бота") +
        tile("a-content", "✎", "Тексты", "приветствия и поддержка") +
        "</div>" +
        group("Доступ") +
        row("Менеджеры", String(a.managers.length), "tab", { screen: "a-managers" }, "★") +
        row("Админы", String(a.admins.length), "tab", { screen: "a-admins" }, "⚑") +
        row("Настройки", a.balances_disabled ? "балансы скрыты" : "балансы видны", "tab", { screen: "a-settings" }, "⚙");
    },
    "a-more": function () {
      var a = S.admin;
      return "<h2>Ещё</h2>" + group("Деньги и тексты") +
        row("Тарифы", "цена за километр", "tab", { screen: "a-tariffs" }, "₽") +
        row("Комиссия", a.commission.enabled ? "включена" : "выключена", "tab", { screen: "a-commission" }, "%") +
        row("Статистика", "сводка", "tab", { screen: "a-stats" }, "▦") +
        row("Тексты", "приветствия", "tab", { screen: "a-content" }, "✎") +
        group("Доступ") +
        row("Менеджеры", String(a.managers.length), "tab", { screen: "a-managers" }, "★") +
        row("Админы", String(a.admins.length), "tab", { screen: "a-admins" }, "⚑") +
        row("Настройки", "балансы и система", "tab", { screen: "a-settings" }, "⚙");
    },
    "a-drivers": function () {
      return "<h2>Водители</h2>" + S.admin.drivers.map(function (d) {
        return row(d.name, (d.verification_status || "") + " · " + (d.car_number || ""), "a-open", { id: d.user_id });
      }).join("");
    },
    "a-driver": function () {
      var d = (S.admin.drivers || []).filter(function (x) { return String(x.user_id) === String(picked); })[0];
      if (!d) return "<h2>Водитель</h2><p class=\"muted\">Не найден.</p>";
      var pending = d.verification_status === "pending";
      var photo = d.car_photo
        ? '<img src="/api/photo/' + d.user_id + '?t=' + Date.now() + '" alt="Авто" style="width:100%;border-radius:12px;margin:10px 0">'
        : '<p class="note">Фото автомобиля не прикреплено</p>';
      // Кнопки решения показываем ТОЛЬКО пока заявка ждёт проверки.
      var decide = pending
        ? '<div class="btn-row"><button class="btn" data-act="adm" data-op="admin.approve" data-id="' + d.user_id + '">Одобрить</button>' +
          '<button class="btn danger" data-act="adm-reject" data-id="' + d.user_id + '">Отклонить</button></div>'
        : '';
      return "<h2>" + esc(d.name) + "</h2><p class=\"muted\">" + esc(d.phone) + "<br>" + esc(d.car_model) + " " + esc(d.car_number) +
        " " + esc(d.car_color) + "<br>Баланс " + money(d.balance) + " · " + esc(d.verification_status) + "</p>" +
        photo + decide +
        '<button class="btn ghost" data-act="adm" data-op="admin.unblock" data-id="' + d.user_id + '">Разблокировать</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="adm" data-op="admin.block" data-id="' + d.user_id + '" data-minutes="60">Блок 1 ч</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.block" data-id="' + d.user_id + '">Блок навсегда</button></div>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.priority" data-id="' + d.user_id + '" data-seconds="3600">Приоритет 1 ч</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="adm" data-op="admin.grant" data-id="' + d.user_id + '" data-hours="8">Смена 8 ч</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.unlim" data-id="' + d.user_id + '">Безлимит</button></div>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.resetacc" data-id="' + d.user_id + '">Обнулить доступ</button>' +
        '<button class="btn ghost" data-act="sheet-admin-balance" data-id="' + d.user_id + '">Пополнить баланс</button>' +
        '<button class="btn ghost" data-act="edit-person" data-kind="driver" data-id="' + d.user_id + '">Изменить</button>' +
        '<button class="btn danger" data-act="adm-delete" data-kind="driver" data-id="' + d.user_id + '" data-name="' + esc(d.name) + '">Удалить</button>';
    },
    "a-moderation": function () {
      var list = S.admin.pending || [];
      return "<h2>Модерация</h2>" + (list.length ? list.map(function (d) {
        return row(d.name, (d.car_number || "без номера") + (d.car_photo ? " · 📷" : " · без фото"), "a-open", { id: d.user_id });
      }).join("") : '<p class="list-empty">Заявок нет</p>');
    },
    "a-orders": function () {
      return "<h2>Заказы</h2><p class=\"muted\">Города: " + esc((S.admin.cities || []).join(", ")) + "</p>" +
        (S.admin.orders || []).map(function (o) { return orderCard(o, "a-order"); }).join("");
    },
    "a-order": function () {
      var o = (S.admin.orders || []).filter(function (x) { return String(x.id) === String(picked); })[0];
      if (!o) return "<h2>Заказ</h2>";
      return "<h2>Заказ #" + o.id + "</h2>" + badge(o.status) + '<div class="card"><div class="route">' + esc(o.pickup) +
        "<small>" + esc(o.destination) + "</small></div><p class=\"muted\">" + esc(o.passenger_name) + " → " + esc(o.driver_name || "без водителя") +
        "</p></div>" + '<button class="btn ghost" data-act="sheet-admin-price" data-id="' + o.id + '">Изменить цену</button>' +
        '<button class="btn danger" data-act="adm" data-op="admin.order.cancel" data-id="' + o.id + '">Отменить</button>' +
        (o.complaint ? '<p class="note">Жалоба: ' + esc(o.complaint) + "</p>" : "");
    },
    "a-passengers": function () {
      var list = S.admin.passengers || [];
      return "<h2>Пассажиры</h2>" + (list.length ? list.map(function (p) {
        return row(p.name, (p.phone || "без телефона") + " · заказов " + p.orders_count, "a-open-pass", { id: p.user_id });
      }).join("") : '<p class="list-empty">Пассажиров нет</p>');
    },
    "a-passenger": function () {
      var p = (S.admin.passengers || []).filter(function (x) { return String(x.user_id) === String(picked); })[0];
      if (!p) return "<h2>Пассажир</h2><p class=\"muted\">Не найден.</p>";
      return "<h2>" + esc(p.name) + "</h2><p class=\"muted\">" + esc(p.phone || "телефон не указан") +
        "<br>Заказов " + p.orders_count + " · потрачено " + money(p.total_spent) +
        (p.is_blocked ? "<br>Заблокирован" : "") + "</p>" +
        '<button class="btn ghost" data-act="edit-person" data-kind="passenger" data-id="' + p.user_id + '">Изменить</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="adm" data-op="admin.passenger.block" data-id="' + p.user_id + '" data-minutes="60">Блок 1 ч</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.passenger.unblock" data-id="' + p.user_id + '">Разблокировать</button></div>' +
        '<button class="btn danger" data-act="adm-delete" data-kind="passenger" data-id="' + p.user_id + '" data-name="' + esc(p.name) + '">Удалить</button>' +
        '<button class="btn ghost" data-act="tab" data-screen="a-passengers">К списку</button>';
    },
    "a-tariffs": function () {
      var cities = S.admin.tariffs || {};
      var ic = S.admin.intercity || {};
      var list = Object.keys(cities).map(function (c) {
        var t = cities[c] || {};
        return '<div class="card"><b>' + esc(c) + '</b><p class="muted">посадка ' +
          esc(t.base_price != null ? t.base_price : "—") + " ₽ · " +
          esc(t.price_per_km != null ? t.price_per_km : "—") + " ₽/км · мин. " +
          esc(t.min_price != null ? t.min_price : "—") + ' ₽</p><div class="btn-row">' +
          '<button class="btn ghost" data-act="edit-tariff" data-city="' + esc(c) + '">Изменить</button>' +
          '<button class="btn danger" data-act="delete-tariff" data-city="' + esc(c) + '">Удалить</button></div></div>';
      }).join("");
      return "<h2>Тарифы</h2>" +
        '<div class="card"><b>Межгород</b><p class="muted">Город подачи и город назначения разные<br>посадка ' +
        esc(ic.base_price != null ? ic.base_price : "—") + " ₽ · " +
        esc(ic.price_per_km != null ? ic.price_per_km : "—") + " ₽/км · мин. " +
        esc(ic.min_price != null ? ic.min_price : "—") + ' ₽</p>' +
        '<button class="btn ghost" data-act="edit-intercity">Изменить межгород</button></div>' +
        (list || '<p class="list-empty">Городских тарифов пока нет</p>') +
        '<button class="btn" data-act="edit-tariff">Добавить город</button>';
    },
    "a-stats": function () {
      var done = (S.admin.orders || []).filter(function (o) { return o.status === "completed"; }).length;
      var live = (S.admin.orders || []).filter(function (o) { return ["pending", "accepted", "driver_arrived", "in_progress"].indexOf(o.status) >= 0; }).length;
      return "<h2>Статистика</h2><div class=\"stat-grid\"><div class=\"stat\"><span class=\"muted\">В ленте завершено</span><b>" +
        done + '</b></div><div class="stat"><span class="muted">Живые</span><b>' + live + "</b></div></div>";
    },
    "a-managers": function () {
      return "<h2>Менеджеры</h2>" + (S.admin.managers || []).map(function (id) {
        return row("id " + id, "убрать", "adm", { op: "admin.manager.remove", id: id });
      }).join("") + field("mid", "ID пользователя", "", "например, 123456789") + '<button class="btn" data-act="add-manager">Добавить</button>';
    },
    "a-admins": function () {
      return "<h2>Админы</h2>" + (S.admin.admins || []).map(function (id) {
        return row("id " + id, "убрать", "adm", { op: "admin.admin.remove", id: id });
      }).join("") + field("aphone", "Телефон", "", "+7 900 000-00-00") + '<button class="btn" data-act="add-admin">Добавить по телефону</button>';
    },
    "a-settings": function () {
      var ic = S.admin.intercity || {};
      var freeMin = ic.free_waiting_minutes != null ? ic.free_waiting_minutes : 3;
      var perMin = ic.waiting_price_per_minute != null ? ic.waiting_price_per_minute : 5;
      return "<h2>Настройки</h2>" +
        "<p class=\"muted\">Балансы у водителей: " +
        (S.admin.balances_disabled ? "скрыты" : "показаны") + "</p>" +
        '<button class="btn" data-act="adm" data-op="admin.balances" data-hidden="' +
        (S.admin.balances_disabled ? "0" : "1") + '">' +
        (S.admin.balances_disabled ? "Показать балансы" : "Скрыть балансы") + "</button>" +
        group("Ожидание") +
        '<div class="card"><b>⏳ Тарификация ожидания</b>' +
        '<p class="muted">Бесплатно ' + esc(freeMin) + " мин, далее " +
        esc(perMin) + " ₽/мин</p>" +
        field("wfree", "Бесплатные минуты", freeMin, "3") +
        field("wper", "Цена за минуту, ₽", perMin, "5") +
        '<button class="btn" data-act="save-waiting">Сохранить</button></div>';
    },
    "a-commission": function () {
      var c = S.admin.commission || {};
      return "<h2>Комиссия</h2><p class=\"muted\">Сейчас " + (c.enabled ? "включена" : "выключена") + ", режим " + esc(c.mode || "") +
        ", " + (c.percent || 0) + "%</p>" +
        '<button class="btn" data-act="adm" data-op="admin.commission" data-enabled="1" data-mode="percent" data-percent="10">Включить 10%</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.commission" data-enabled="1" data-mode="access" data-percent="10">Режим смен</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.commission" data-enabled="0" data-mode="percent" data-percent="0">Выключить</button>';
    },
    "a-content": function () {
      return "<h2>Контент</h2>" + field("wp", "Приветствие пассажира", S.admin.welcome_passenger, "") +
        field("wd", "Приветствие водителя", S.admin.welcome_driver, "") +
        field("ws", "Поддержка", S.admin.support, "") +
        '<button class="btn" data-act="save-content">Сохранить</button>';
    }
  };

  function tile(screenName, icon, title, sub) {
    return '<button class="tile" data-act="tab" data-screen="' + screenName + '"><em>' + icon + "</em><span><b>" + title + "</b><br>" + sub + "</span></button>";
  }
  function seg(status, label) {
    return '<button class="' + (S.user.line === status ? "on" : "") + '" data-act="line" data-status="' + status + '">' + label + "</button>";
  }
  function sheet(html) {
    $("sheet").innerHTML = '<div class="handle"></div>' + html;
    $("sheet-back").hidden = false;
  }
  function closeSheet() { $("sheet-back").hidden = true; }

  // ==================== ТОКЕН ПОЛЬЗОВАТЕЛЯ ====================
  // Персональный токен изолирует данные: чужой токен не даёт доступа
  // к заказам другого пользователя.
  var TOKEN_KEY = "maximus_token";

  function savedToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function rememberToken(token) {
    if (!token) return "";
    try { localStorage.setItem(TOKEN_KEY, token); } catch (e) {}
    try {
      var secure = window.isSecureContext ? "; Secure" : "";
      document.cookie = TOKEN_KEY + "=" + encodeURIComponent(token) +
        "; path=/; max-age=31536000; SameSite=None" + secure;
    } catch (e) {}
    return token;
  }

  function savedTokenCookie() {
    try {
      var m = document.cookie.match(new RegExp("(?:^|; )" + TOKEN_KEY + "=([^;]*)"));
      return m ? decodeURIComponent(m[1]) : "";
    } catch (e) { return ""; }
  }

  // ==================== ЗАПОМИНАНИЕ ПОЛЬЗОВАТЕЛЯ ====================
  var UID_KEY = "maximus_uid";

  function savedUid() {
    try {
      var v = localStorage.getItem(UID_KEY);
      return v ? Number(v) : 0;
    } catch (e) { return 0; }
  }

  function rememberUid(id) {
    var n = Number(id) || 0;
    if (n > 0) {
      try { localStorage.setItem(UID_KEY, String(n)); } catch (e) {}
    }
    return n;
  }

  function savedUidCookie() {
    try {
      var m = document.cookie.match(new RegExp("(?:^|; )" + UID_KEY + "=([^;]*)"));
      return m ? Number(m[1]) : 0;
    } catch (e) { return 0; }
  }

  function rememberUidCookie(id) {
    var n = Number(id) || 0;
    if (n > 0) {
      try {
        // SameSite=None обязателен: мини-апп открывается в iframe,
        // а Lax-cookie там блокируются как сторонние.
        var secure = window.isSecureContext ? "; Secure" : "";
        document.cookie = UID_KEY + "=" + n +
          "; path=/; max-age=31536000; SameSite=None" + secure;
        // Дублируем без Secure — для локальной отладки по http
        if (!secure) {
          document.cookie = UID_KEY + "=" + n + "; path=/; max-age=31536000; SameSite=Lax";
        }
      } catch (e) {}
    }
    return n;
  }

  // ==================== АВТОДОПОЛНЕНИЕ АДРЕСА ====================
  var _suggestTimer = null;
  var _suggestSeq = 0;

  function geoBtn(fieldId) {
    return '<button type="button" class="btn ghost geo-btn" data-act="my-geo" data-field="' + fieldId + '">Моё место</button>';
  }

  var _geoPoints = {};

  function clearGeoPoint(inputId) {
    delete _geoPoints[inputId];
    if (!draft) return;
    if (inputId === "pickup") draft.pickup_coords = null;
    if (inputId === "dest") {
      draft.dest_coords = null;
      draft.destination_coords = null;
    }
  }

  function putGeoAddress(inputId, res) {
    var address = (res && res.address) || "";
    if (!address) { toast("Адрес по точке не найден"); return; }
    var point = { lat: res.lat, lon: res.lon };
    _geoPoints[inputId] = point;
    if (!draft) draft = { step: 1 };
    if (inputId === "dest" || inputId === "newdest" || inputId === "fdest") {
      draft.destination = address;
      draft.dest_coords = point;
      draft.destination_coords = point;
      if (res.city) draft.destination_city = res.city;
    } else if (inputId === "stop" || inputId === "newstop") {
      draft.stop = address;
    } else {
      draft.pickup = address;
      draft.pickup_coords = point;
      if (res.city) draft.pickup_city = res.city;
      if (!draft.step) draft.step = 1;
    }
    if (screen !== "trip" && (inputId === "pickup" || inputId === "dest")) {
      screen = "trip";
    }
    render();
    var input = document.getElementById(inputId);
    if (input) input.value = address;
    toast("Адрес подставлен");
  }

  function mapStart() {
    var u = (S && S.user) || {};
    var lat = Number(u.lat);
    var lon = Number(u.lon);
    if (lat && lon) return { lat: lat, lon: lon };
    var city = String(u.home_city || "").toLowerCase();
    var known = {
      "черногорск": [53.8236, 91.2836],
      "абакан": [53.7212, 91.4424],
      "минусинск": [53.7103, 91.6872],
      "саяногорск": [53.0896, 91.4122],
      "красноярск": [56.0153, 92.8932]
    };
    var point = known[city] || known["черногорск"];
    return { lat: point[0], lon: point[1] };
  }

  function mountGeoMap(root, lat, lon) {
    var z = 16;
    var worldX = 0;
    var worldY = 0;
    var moved = false;

    function lonToX(value, zoom) {
      return (value + 180) / 360 * Math.pow(2, zoom);
    }
    function latToY(value, zoom) {
      var rad = value * Math.PI / 180;
      var n = Math.log(Math.tan(rad) + 1 / Math.cos(rad));
      return (1 - n / Math.PI) / 2 * Math.pow(2, zoom);
    }
    function xToLon(value, zoom) {
      return value / Math.pow(2, zoom) * 360 - 180;
    }
    function yToLat(value, zoom) {
      var n = Math.PI - 2 * Math.PI * value / Math.pow(2, zoom);
      return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    }
    function setCenter(nextLat, nextLon) {
      worldX = lonToX(nextLon, z) * 256;
      worldY = latToY(nextLat, z) * 256;
      draw();
    }
    function draw() {
      var old = root.querySelectorAll("img");
      for (var i = 0; i < old.length; i++) old[i].remove();
      var rect = root.getBoundingClientRect();
      var w = rect.width || 300;
      var h = rect.height || 280;
      var n = Math.pow(2, z);
      var leftTile = Math.floor((worldX - w / 2) / 256) - 1;
      var rightTile = Math.floor((worldX + w / 2) / 256) + 1;
      var topTile = Math.floor((worldY - h / 2) / 256) - 1;
      var bottomTile = Math.floor((worldY + h / 2) / 256) + 1;
      for (var tx = leftTile; tx <= rightTile; tx++) {
        for (var ty = topTile; ty <= bottomTile; ty++) {
          if (ty < 0 || ty >= n) continue;
          var img = document.createElement("img");
          var wrapped = ((tx % n) + n) % n;
          img.alt = "";
          img.src = "https://tile.openstreetmap.org/" + z + "/" + wrapped + "/" + ty + ".png";
          img.style.left = (tx * 256 - worldX + w / 2) + "px";
          img.style.top = (ty * 256 - worldY + h / 2) + "px";
          root.appendChild(img);
        }
      }
      window._mapCenter = {
        lat: yToLat(worldY / 256, z),
        lon: xToLon(worldX / 256, z)
      };
    }
    setCenter(lat, lon);
    var drag = null;
    root.addEventListener("pointerdown", function (e) {
      if (e.target.closest("button")) return;
      drag = { x: e.clientX, y: e.clientY, wx: worldX, wy: worldY };
      root.setPointerCapture(e.pointerId);
    });
    root.addEventListener("pointermove", function (e) {
      if (!drag) return;
      worldX = drag.wx - (e.clientX - drag.x);
      worldY = drag.wy - (e.clientY - drag.y);
      moved = true;
      draw();
    });
    root.addEventListener("pointerup", function () { drag = null; });
    root.addEventListener("pointercancel", function () { drag = null; });
    window._geoMap = {
      zoom: function (delta) {
        var center = window._mapCenter;
        z = Math.max(13, Math.min(18, z + delta));
        setCenter(center.lat, center.lon);
      }
    };
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(function (pos) {
        if (moved) return;
        setCenter(pos.coords.latitude, pos.coords.longitude);
      }, function () {}, { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
    }
  }

  function openGeoMap(inputId) {
    window._geoField = inputId;
    var start = mapStart();
    sheet("<h2>Где вы</h2><p class=\"muted\">Сдвиньте карту, чтобы метка стояла на доме.</p>" +
      '<div class="geo-map" id="geomap"><i class="geo-pin"></i>' +
      '<div class="geo-zoom"><button type="button" data-act="geo-zoom" data-z="1">+</button>' +
      '<button type="button" data-act="geo-zoom" data-z="-1">−</button></div></div>' +
      '<p class="geo-copy">© OpenStreetMap</p>' +
      '<button class="btn" data-act="geo-map-ok">Подставить адрес</button>');
    mountGeoMap(document.getElementById("geomap"), start.lat, start.lon);
  }

  function useMyLocation(inputId) {
    window._geoField = inputId;
    if (!navigator.geolocation) {
      openGeoMap(inputId);
      return;
    }
    navigator.geolocation.getCurrentPosition(function (pos) {
      api("/api/act", { op: "geo.resolve", lat: pos.coords.latitude, lon: pos.coords.longitude }).then(function (res) {
        putGeoAddress(inputId, res);
      }).catch(function () { openGeoMap(inputId); });
    }, function () {
      openGeoMap(inputId);
    }, { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
  }

  function fullAddress(it) {
    if (!it) return "";
    if (it.address) return String(it.address).trim();
    return [it.title, it.subtitle].filter(Boolean).join(", ");
  }

  function attachSuggest(inputId, onPick, opts) {
    var input = document.getElementById(inputId);
    if (!input) return;
    var options = opts || {};

    var box = document.createElement("div");
    box.className = "suggest-box";
    box.hidden = true;
    input.parentNode.appendChild(box);

    function hide() { box.hidden = true; box.innerHTML = ""; }

    function show(items) {
      if (!items || !items.length) { hide(); return; }
      var html = "";
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        var icon = it.kind === "house" ? "\uD83C\uDFE0"
                 : it.kind === "street" ? "\uD83D\uDEE3"
                 : "\uD83D\uDCCD";
        html += '<button type="button" class="suggest-item" data-i="' + i + '">' +
          "<i>" + icon + "</i><span>" + esc(fullAddress(it)) + "</span></button>";
      }
      box.innerHTML = html;
      box.hidden = false;

      var btns = box.querySelectorAll(".suggest-item");
      for (var k = 0; k < btns.length; k++) {
        (function (btn) {
          btn.addEventListener("click", function (e) {
            e.preventDefault();
            var it = items[Number(btn.dataset.i)];
            input.value = fullAddress(it);
            clearGeoPoint(inputId);
            hide();
            if (onPick) onPick(it);
          });
        })(btns[k]);
      }
    }

    function fetchSuggest(query) {
      var seq = ++_suggestSeq;
      api("/api/act", {
        op: "geo.suggest",
        query: query,
        city: options.city || (S && S.user && S.user.home_city) || "",
        lat: null,
        lon: null
      }).then(function (res) {
        if (seq !== _suggestSeq) return;
        show(res.items || []);
      }).catch(function () { hide(); });
    }

    input.addEventListener("input", function () {
      clearGeoPoint(inputId);
      var q = input.value.trim();
      clearTimeout(_suggestTimer);
      if (q.length < 2) { hide(); return; }
      _suggestTimer = setTimeout(function () { fetchSuggest(q); }, 320);
    });

    input.addEventListener("blur", function () {
      setTimeout(hide, 180);
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") hide();
    });
  }

  function render() {
    if (S && S.user && S.user.role) {
      document.body.dataset.role = S.user.role;
    }
    // Сохраняем то, что пользователь уже ввёл в поля
    var focused = document.activeElement;
    var focusedId = focused && focused.id ? focused.id : "";
    var typed = {};
    var inputs = document.querySelectorAll("input");
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].id) typed[inputs[i].id] = inputs[i].value;
    }
    var caret = 0;
    if (focused && typeof focused.selectionStart === "number") {
      caret = focused.selectionStart;
    }

    $("top").innerHTML = header();
    $("tabs").innerHTML = tabs();
    var fn = views[screen];
    $("view").innerHTML = fn ? fn() : "<p>Экран не найден</p>";

    // Возвращаем введённый текст
    var after = document.querySelectorAll("input");
    for (var j = 0; j < after.length; j++) {
      var id = after[j].id;
      if (id && typed[id]) after[j].value = typed[id];
    }
    // Возвращаем фокус и курсор
    if (focusedId) {
      var again = document.getElementById(focusedId);
      if (again) {
        try {
          again.focus();
          if (typeof again.setSelectionRange === "function") {
            again.setSelectionRange(caret, caret);
          }
        } catch (e) {}
      }
    }

    // Подключаем автодополнение адреса на нужных экранах
    if (screen === "trip") {
      var step = (draft && draft.step) || 1;
      if (step === 1) {
        attachSuggest("pickup", function (it) { draft.pickup = fullAddress(it); });
      } else if (step === 2) {
        attachSuggest("dest", function (it) { draft.destination = fullAddress(it); });
      }
    }
    if (screen === "d-current") {
      attachSuggest("geoaddr", null);
    }

    var deep = stack.length > 0 && screen !== homeOf();
    window.Maximus.back(deep, function () {
      screen = stack.pop() || homeOf();
      render();
    });
  }

  var actions = {
    tab: function (el) {
      stack = [];
      var next = el.dataset.screen;
      if (next === "trip") {
        if (needOnboarding()) {
          window._onbStep = 1;
          screen = "welcome";
          render();
          toast("Сначала заполните профиль");
          return;
        }
        if (S.passenger && S.passenger.active) { picked = S.passenger.active.id; next = "live"; }
        else draft = { step: 1 };
      }
      screen = next;
      render();
    },
    roles: function () { go("roles"); },
    role: function (el) {
      var next = el.dataset.role;
      if (!S.user.roles || S.user.roles.indexOf(next) < 0) { toast("Роль недоступна"); return; }
      stack = [];
      run({ op: "role", role: next }).then(function (ok) {
        if (!ok) return;
        setSplashRole(next);
        screen = homeOf();
        render();
      });
    },
    login: function (el) {
      api("/api/act", { op: "login", user_id: Number(el.dataset.id) }).then(function (data) {
        if (data.state) { S = normalizeState(data.state); screen = entryScreen(); render(); }
      }).catch(function (e) { toast(e.message); });
    },
    "my-geo": function (el) {
      useMyLocation(el.dataset.field || "pickup");
    },
    "geo-zoom": function (el) {
      if (window._geoMap) window._geoMap.zoom(Number(el.dataset.z) || 1);
    },
    "geo-map-ok": function () {
      var center = window._mapCenter;
      var field = window._geoField || "pickup";
      if (!center) return;
      api("/api/act", { op: "geo.resolve", lat: center.lat, lon: center.lon }).then(function (res) {
        closeSheet();
        putGeoAddress(field, res);
      }).catch(function (e) { toast(e.message || "Адрес не найден"); });
    },
    start: function () {
      if (needOnboarding()) {
        window._onbStep = 1;
        go("welcome");
        toast("Сначала заполните профиль");
        return;
      }
      draft = { step: 1 };
      go("trip");
    },
    "pickup-next": function () {
      var pickup = val("pickup");
      if (pickup.length < 3) { toast("Укажите адрес"); return; }
      draft.pickup = pickup;
      draft.entrance = val("entrance");
      draft.step = 2;
      render();
    },
    "dest-next": function () {
      var dest = val("dest");
      if (dest.length < 3) { toast("Укажите, куда ехать"); return; }
      if (draft.pending) return;
      draft.destination = dest;
      draft.step = 3;
      draft.pending = true;
      var entrance = draft.entrance || "";
      var favIndex = draft.favoriteIndex;
      render();
      api("/api/act", {
        op: "quote",
        pickup: draft.pickup,
        destination: dest,
        stop: draft.stop || "",
        pickup_coords: draft.pickup_coords || null,
        dest_coords: draft.dest_coords || draft.destination_coords || null
      }).then(function (data) {
        draft = data.draft; draft.step = 3; draft.pending = false;
        draft.entrance = entrance;
        if (favIndex != null) draft.favoriteIndex = favIndex;
        render();
      }).catch(function (e) { draft.step = 2; draft.pending = false; toast(e.message); render(); });
    },
    "sheet-price": function () {
      var base = (draft && draft.calc_price) ? Number(draft.calc_price) : (draft ? Number(draft.price) || 0 : 0);
      sheet("<h2>Своя цена</h2><p class=\"muted\">Только больше рассчитанной: " + money(base) + "</p>" +
        field("own", "Сумма", "", (base ? base + 1 : 220)) + '<button class="btn" data-act="apply-price">Поставить</button>');
    },
    "apply-price": function () {
      var n = Number(val("own"));
      if (!n || isNaN(n) || n <= 0) { toast("Введите сумму"); return; }
      // Запоминаем рассчитанную стоимость при первом изменении цены
      if (draft && !draft.calc_price) draft.calc_price = Number(draft.price) || 0;
      var base = (draft && draft.calc_price) ? Number(draft.calc_price) : 0;
      if (base > 0 && n <= base) { toast("Своя цена должна быть больше рассчитанной: " + base + " ₽"); return; }
      if (base <= 0 && n < 50) { toast("Минимум 50 ₽"); return; }
      draft.price = n; closeSheet(); render();
    },
    "sheet-stop": function () {
      sheet("<h2>Остановка</h2>" + field("stop", "Адрес", draft.stop || "", "Город, улица, дом") +
        geoBtn("stop") + '<button class="btn" data-act="apply-stop">Пересчитать</button>');
      attachSuggest("stop", function (it) {
        var input = document.getElementById("stop");
        if (input) input.value = fullAddress(it);
      });
    },
    "apply-stop": function () {
      var stop = val("stop");
      closeSheet();
      draft.stop = stop;
      draft.pending = true;
      var entrance = draft.entrance || "";
      var favIndex = draft.favoriteIndex;
      render();
      api("/api/act", {
        op: "quote", pickup: draft.pickup, destination: draft.destination, stop: stop,
        pickup_coords: draft.pickup_coords || null,
        dest_coords: draft.dest_coords || draft.destination_coords || null
      }).then(function (data) {
        draft = data.draft; draft.step = 3; draft.pending = false;
        draft.entrance = entrance;
        if (favIndex != null) draft.favoriteIndex = favIndex;
        render();
      }).catch(function (e) { draft.pending = false; toast(e.message); render(); });
    },
    "edit-route": function () {
      if (!draft) return;
      draft.step = 1;
      draft.pending = false;
      render();
    },
    confirm: async function () {
      var ok = await run({ op: "order.create", draft: draft, price: draft.price });
      if (ok && S.passenger.active) { draft = null; picked = S.passenger.active.id; screen = "live"; render(); }
    },
    "open-order": function (el) { picked = Number(el.dataset.id); go("live"); },
    cancel: function (el) { run({ op: "order.cancel", id: Number(el.dataset.id) }); },
    "no-driver-wait": function (el) {
      // Пассажир выбрал ждать: помечаем в локальном состоянии и на сервере,
      // чтобы авто-отмена не сработала.
      if (S.passenger && S.passenger.active) S.passenger.active.passenger_waiting = true;
      run({ op: "order.wait", id: Number(el.dataset.id) }).then(function () {
        toast("Продолжаем искать водителя");
        screen = "home";
        render();
      });
    },
    "no-driver-cancel": function (el) {
      run({ op: "order.cancel", id: Number(el.dataset.id) }).then(function () {
        _noDriverPrompted = {};
        screen = "home";
        render();
      });
    },
    "sheet-live-dest": function (el) {
      var o = orderById(Number(el.dataset.id)) || (S.driver && S.driver.current) || {};
      sheet("<h2>Новый адрес</h2><p class=\"muted\">Куда ехать вместо текущего адреса.</p>" +
        field("newdest", "Куда", o.destination || "", "Город, улица, дом") +
        geoBtn("newdest") +
        '<button class="btn" data-act="save-live-dest" data-id="' + el.dataset.id + '">Пересчитать</button>');
      attachSuggest("newdest", function (it) {
        var input = document.getElementById("newdest");
        if (input) input.value = fullAddress(it);
      });
    },
    "save-live-dest": function (el) {
      var dest = val("newdest");
      if (dest.length < 3) { toast("Укажите адрес"); return; }
      var point = _geoPoints.newdest || null;
      closeSheet();
      run({ op: "order.route", id: Number(el.dataset.id), destination: dest, destination_coords: point });
    },
    "sheet-live-stop": function (el) {
      sheet("<h2>Остановка</h2>" + field("newstop", "Адрес остановки", "", "Город, улица, дом") +
        geoBtn("newstop") +
        '<button class="btn" data-act="save-live-stop" data-id="' + el.dataset.id + '">Добавить</button>');
      attachSuggest("newstop", function (it) {
        var input = document.getElementById("newstop");
        if (input) input.value = fullAddress(it);
      });
    },
    "save-live-stop": function (el) {
      var stop = val("newstop");
      if (stop.length < 3) { toast("Укажите остановку"); return; }
      var o = orderById(Number(el.dataset.id)) || (S.driver && S.driver.current) || {};
      closeSheet();
      run({ op: "order.route", id: Number(el.dataset.id), destination: o.destination || "", stop: stop });
    },
    "sheet-price-live": function (el) {
      var oid = Number(el.dataset.id);
      var o = orderById(oid);
      var base = (o && o.base_price) ? Number(o.base_price) : (o ? Number(o.price) || 0 : 0);
      sheet("<h2>Своя цена</h2><p class=\"muted\">Только больше рассчитанной: " + money(base) + "</p>" +
        field("own", "Сумма", "", (base ? base + 1 : 220)) + '<button class="btn" data-act="save-live-price" data-id="' + el.dataset.id + '">Сохранить</button>');
    },
    "save-live-price": function (el) {
      var oid = Number(el.dataset.id);
      var n = Number(val("own"));
      if (!n || isNaN(n) || n <= 0) { toast("Введите сумму"); return; }
      var o = orderById(oid);
      var base = (o && o.base_price) ? Number(o.base_price) : (o ? Number(o.price) || 0 : 0);
      if (base > 0 && n <= base) { toast("Своя цена должна быть больше рассчитанной: " + base + " ₽"); return; }
      closeSheet();
      run({ op: "order.price", id: oid, price: n });
    },
    "sheet-complaint": function (el) { sheet("<h2>Жалоба</h2>" + field("complaint", "Что не совпало", "", "номер, цвет, водитель") + '<button class="btn" data-act="save-complaint" data-id="' + el.dataset.id + '">Отправить</button>'); },
    "save-complaint": function (el) { closeSheet(); run({ op: "order.complaint", id: Number(el.dataset.id), text: val("complaint") }); },
    "fav-add": function (el) { run({ op: "favorite.add", id: Number(el.dataset.id) }).then(function () { toast("В избранном"); }); },
    "del-fav": function (el) {
      var index = Number(el.dataset.index);
      var fav = ((S.passenger && S.passenger.favorites) || [])[index] || {};
      // Без window.confirm: в WebView MAX нативные confirm/prompt могут
      // блокироваться, и кнопка «Удалить» не срабатывала вообще.
      run({ op: "favorite.remove", index: index }).then(function (ok) {
        if (ok) { toast("Маршрут удалён"); screen = "favorites"; render(); }
      });
    },
    "d-period": function (el) {
      window._statPeriod = el.dataset.p || "week";
      render();
    },
    "d-today-open": function (el) {
      picked = Number(el.dataset.id);
      go("d-today-detail");
    },
    "use-fav": function (el) {
      var index = Number(el.dataset.index);
      api("/api/act", { op: "favorite.preview", index: index }).then(function (data) {
        if (!data || !data.draft || !data.draft.pickup) {
          toast("Маршрут не найден");
          return;
        }
        draft = data.draft;
        draft.step = 3;
        draft.pending = false;
        draft.favoriteIndex = index;
        draft.calc_price = Number(draft.price) || 0;
        screen = "trip";
        render();
      }).catch(function (e) { toast(e.message || "Не удалось открыть маршрут"); });
    },
    "to-rate": function (el) { picked = Number(el.dataset.id); window._star = 0; go("rate"); },
    star: function (el) { window._star = Number(el.dataset.n); render(); },
    "save-rate": function () {
      var oid = picked;
      run({ op: "order.rate", id: oid, stars: window._star || 0 }).then(function (ok) {
        if (!ok) { screen = "home"; render(); return; }
        // Предлагаем сохранить маршрут в избранное
        window._favOrder = oid;
        go("fav-offer");
      });
    },
    "fav-save": function () {
      var oid = window._favOrder;
      if (!oid) { screen = "home"; render(); return; }
      var o = orderById(oid) || {};
      // Предлагаем дать маршруту понятное название: «На работу», «В школу»…
      var presets = ["🏠 Домой", "💼 На работу", "🏫 В школу", "🏋️ Тренировка", "🛒 Магазин"];
      sheet("<h2>Название маршрута</h2>" +
        '<p class="muted">' + esc(o.pickup || "") + " → " + esc(o.destination || "") + "</p>" +
        '<div class="chips">' + presets.map(function (p) {
          return '<button type="button" data-act="fav-name" data-name="' + esc(p) + '">' + esc(p) + "</button>";
        }).join("") + "</div>" +
        field("favname", "Название", "", "Например, На работу") +
        '<button class="btn" data-act="fav-save-do">Сохранить в избранное</button>' +
        '<button class="btn ghost" data-act="fav-skip">Не сохранять</button>');
    },
    "fav-name": function (el) {
      var input = document.getElementById("favname");
      if (input) input.value = el.dataset.name || "";
    },
    "fav-save-do": function () {
      var oid = window._favOrder;
      if (!oid) { closeSheet(); screen = "home"; render(); return; }
      var label = val("favname");
      closeSheet();
      run({ op: "favorite.add", id: oid, label: label }).then(function (ok) {
        if (ok) toast(label ? "Сохранено: " + label : "Маршрут сохранён в избранное");
        window._favOrder = null;
        screen = "home";
        render();
      });
    },
    "fav-skip": function () {
      window._favOrder = null;
      screen = "home";
      render();
    },
    "go-receipt": function () { go("receipt"); },
    "edit-fav": function (el) {
      var index = Number(el.dataset.index);
      api("/api/act", { op: "favorite.preview", index: index }).then(function (data) {
        var d = (data && data.draft) || {};
        var fav = ((S.passenger && S.passenger.favorites) || [])[index] || {};
        sheet("<h2>Избранный заказ</h2>" +
          field("fpickup", "Откуда", d.pickup || fav.pickup || "", "Город, улица, дом") +
          geoBtn("fpickup") +
          field("fentrance", "Подъезд", d.entrance || fav.entrance || "", "номер") +
          field("fdest", "Куда", d.destination || fav.destination || "", "Город, улица, дом") +
          geoBtn("fdest") +
          field("flabel", "Название", d.label || fav.label || "", "Дом → Работа") +
          '<button class="btn" data-act="save-fav" data-index="' + index + '">Сохранить</button>');
        attachSuggest("fpickup", null);
        attachSuggest("fdest", null);
      }).catch(function (e) { toast(e.message || "Не удалось открыть"); });
    },
    "save-fav": function (el) {
      var index = Number(el.dataset.index);
      var pickup = val("fpickup");
      var destination = val("fdest");
      if (pickup.length < 3 || destination.length < 3) { toast("Укажите оба адреса"); return; }
      closeSheet();
      run({
        op: "favorite.edit", index: index, pickup: pickup, destination: destination,
        entrance: val("fentrance"), label: val("flabel")
      }).then(function (ok) {
        if (!ok || screen !== "trip") return;
        api("/api/act", { op: "favorite.preview", index: index }).then(function (data) {
          draft = data.draft;
          draft.step = 3;
          draft.pending = false;
          draft.favoriteIndex = index;
          draft.calc_price = Number(draft.price) || 0;
          render();
        }).catch(function (e) { toast(e.message); });
      });
    },
    "sheet-chat": function (el) {
      picked = Number(el.dataset.id);
      var pool = [].concat((S.passenger && S.passenger.orders) || [], (S.driver && S.driver.current) ? [S.driver.current] : [], (S.admin && S.admin.orders) || []);
      var o = pool.filter(function (x) { return x && String(x.id) === String(picked); })[0] || {};
      var mine = S && S.user ? String(S.user.id) : "";
      var title = (S && S.user && S.user.role === "driver") ? "Диалог с пассажиром" : "Диалог с водителем";
      var bubbles = (o.chat || []).map(function (m) {
        return '<div class="bubble' + (String(m.from) === mine ? " me" : "") + '">' + esc(m.text) + "</div>";
      }).join("");
      sheet("<h2>" + title + "</h2><div class=\"chat\">" + (bubbles || '<p class="muted">Пока пусто</p>') + "</div>" +
        field("msg", "Текст", "", "") + '<button class="btn" data-act="send-msg" data-id="' + picked + '">Отправить</button>');
    },
    "send-msg": function (el) {
      var oid = Number(el.dataset.id);
      var text = val("msg");
      if (!text) { toast("Введите текст"); return; }
      closeSheet();
      api("/api/act", { op: "order.chat", id: oid, text: text }).then(function (data) {
        // Запоминаем СВОЁ сообщение: всплывающее окно показываем только
        // на входящие, а свои помечаем здесь (в т.ч. когда пассажир и
        // водитель — один и тот же аккаунт при тесте).
        noteMyMessage(oid, (data && data.chat) || []);
        if (data && data.state) S = normalizeState(data.state);
        render();
        toast("Отправлено");
      }).catch(function (e) { toast(e.message); });
    },
    "max-chat": function (el) {
      // Связь через приложение: открываем переписку внутри заказа.
      // Внешняя ссылка на профиль MAX (https://max.ru/id<uid>) устарела —
      // MAX её не открывает («ссылка устарела»).
      actions["sheet-chat"] && actions["sheet-chat"](el);
    },
    "max-call": function (el) {
      // max.ru/id… MAX открывает как «ссылка устарела». Карточка контакта
      // уходит в чат с ботом: по ней звонок идёт уже внутри MAX.
      var id = Number(el.dataset.id || picked || 0);
      var phone = el.dataset.phone || "";
      run({ op: "order.call", id: id }).then(function (ok) {
        if (ok) {
          toast("Карточка в чате с ботом — нажмите её и позвоните");
          var link = (S.user && S.user.bot_link) || "";
          if (link && window.Maximus.openBotChat) window.Maximus.openBotChat(link);
          return;
        }
        var clean = String(phone).replace(/[^\d+]/g, "");
        if (!clean) { toast("Номер телефона не указан"); return; }
        var a = document.createElement("a");
        a.href = "tel:" + clean;
        document.body.appendChild(a);
        a.click();
        a.remove();
      });
    },
    call: function (el) {
      var phone = el.dataset.phone || "";
      if (!phone) { toast("Номер не указан"); return; }
      var clean = String(phone).replace(/[^\d+]/g, "");
      window.Maximus.openLink("tel:" + clean);
    },
    "onb-next": function () {
      var step = window._onbStep || 1;
      if (step === 1) {
        var name = val("onb_name");
        if (name.length < 2) { toast("Введите имя"); return; }
        window._onbName = name;
        window._onbStep = 2;
        render();
        return;
      }
      if (step === 2) {
        var phone = val("onb_phone");
        var digits = phone.replace(/\D/g, "");
        if (digits.length < 10) { toast("Введите телефон полностью"); return; }
        window._onbPhone = phone;
        window._onbStep = 3;
        render();
        return;
      }
    },
    "onb-back": function () {
      window._onbStep = Math.max(1, (window._onbStep || 3) - 1);
      render();
    },
    "onb-finish": function () {
      var city = val("onb_city");
      var name = window._onbName || val("onb_name");
      var phone = window._onbPhone || val("onb_phone");
      if (!name || name.length < 2) { window._onbStep = 1; render(); toast("Введите имя"); return; }
      if (String(phone).replace(/\D/g, "").length < 10) {
        window._onbStep = 2; render(); toast("Введите телефон полностью"); return;
      }
      run({
        op: "profile",
        name: name,
        phone: phone,
        home_city: city
      }).then(function (ok) {
        if (!ok) return;
        window._onbStep = null;
        window._onbName = null;
        window._onbPhone = null;
        toast("Профиль сохранён");
        stack = [];
        screen = homeOf();
        render();
        // Сразу подтягиваем состояние: сервер проставил onboarded,
        // и знакомство больше не покажется при следующих входах.
        refresh().catch(function () {});
      });
    },
    "sheet-profile": function () {
      sheet("<h2>Профиль</h2>" +
        field("pname", "Имя", S.user.name, "") +
        field("pphone", "Телефон", S.user.phone, "") +
        field("pcity", "Город", S.user.home_city, "Например, Абакан") +
        '<button class="btn" data-act="save-profile">Сохранить</button>');
    },
    "save-profile": function () {
      closeSheet();
      run({
        op: "profile",
        name: val("pname"),
        phone: val("pphone"),
        home_city: val("pcity")
      });
    },
    apply: function () {
      var car = val("car"), plate = val("plate"), color = val("color");
      if (!car || !plate || !color) { toast("Заполните марку, номер и цвет"); return; }
      var hasPhoto = !!(S.user && S.user.car_photo);
      if (!hasPhoto && !_carPhoto) { toast("Прикрепите фото автомобиля"); return; }
      var send = function () {
        run({ op: "driver.apply", car: car, plate: plate, color: color });
      };
      if (_carPhoto) {
        uploadCarPhoto(_carPhoto).then(function () {
          _carPhoto = "";
          send();
        }).catch(function (e) { toast(e.message); });
        return;
      }
      send();
    },
    "pick-photo": function () {
      var input = document.getElementById("car_photo_file");
      if (!input) {
        // На экране «заявка на проверке» поля нет — создаём временное
        input = document.createElement("input");
        input.type = "file";
        input.accept = "image/*";
        input.style.display = "none";
        document.body.appendChild(input);
      }
      input.onchange = function () {
        var f = input.files && input.files[0];
        if (!f) return;
        readPhoto(f, function (dataUrl) {
          _carPhoto = dataUrl;
          if (S.user.driver_status === "pending" || S.user.driver_status === "rejected") {
            // Дослать фото в уже поданную заявку
            uploadCarPhoto(dataUrl).then(function () {
              _carPhoto = "";
              toast("Фото прикреплено");
              refresh().catch(function () {});
              render();
            }).catch(function (e) { toast(e.message); });
            return;
          }
          render();
          toast("Фото выбрано — отправьте анкету");
        });
      };
      input.click();
    },
    period: function (el) { period = el.dataset.p; render(); },
    dperiod: function (el) { driverPeriod = el.dataset.p; render(); },
    line: function (el) { run({ op: "driver.line", status: el.dataset.status }); },
    accept: function (el) { screen = "d-current"; run({ op: "driver.accept", id: Number(el.dataset.id) }); },
    "d-open": function (el) { screen = "d-current"; render(); },
    arrived: function (el) { run({ op: "driver.arrived", id: Number(el.dataset.id) }); },
    wait: function (el) {
      var oid = Number(el.dataset.id);
      var cur = S && S.driver && S.driver.current;
      var statusBefore = cur ? cur.status : "";
      run({ op: "driver.wait", id: oid }).then(function (ok) {
        if (!ok) return;
        // Защита: «Ожидание» НЕ должно менять статус заказа. Если сервер
        // (или устаревший кэш) вернул другой статус — возвращаем прежний,
        // чтобы «Завершить поездку» не превратилось в «Начать движение».
        if (cur && statusBefore && cur.status !== statusBefore) {
          cur.status = statusBefore;
          render();
        }
      });
    },
    "start-trip": function (el) { run({ op: "driver.start", id: Number(el.dataset.id) }); },
    "resume-trip": function (el) {
      // «Продолжить поездку» — останавливает ожидание и возвращает
      // кнопки «Ожидание» и «Завершить поездку».
      run({ op: "driver.wait", id: Number(el.dataset.id) });
    },
    complete: function (el) {
      run({ op: "driver.complete", id: Number(el.dataset.id) });
    },
    "sheet-dname": function () { sheet("<h2>Имя</h2>" + field("dval", "Новое имя", S.user.driver_name, "") + '<button class="btn" data-act="save-d" data-field="name">Сохранить</button>'); },
    "sheet-dphone": function () { sheet("<h2>Телефон</h2>" + field("dval", "Телефон", S.user.driver_phone, "") + '<button class="btn" data-act="save-d" data-field="phone">Сохранить</button>'); },
    "sheet-car": function (el) { sheet("<h2>Авто</h2>" + field("dval", "Значение", "", "") + '<button class="btn" data-act="save-d" data-field="' + el.dataset.field + '">Сохранить</button>'); },
    "save-d": function (el) { closeSheet(); var body = { op: "driver.profile" }; body[el.dataset.field] = val("dval"); run(body); },
    doc: function (el) { run({ op: "driver.doc", key: el.dataset.key }); },
    "sheet-topup": function () { sheet("<h2>Пополнение</h2>" + field("amount", "Сумма", "500", "") + '<button class="btn" data-act="topup">Зачислить</button>'); },
    topup: function () { closeSheet(); run({ op: "driver.topup", amount: Number(val("amount")) }); },
    buy: function (el) { run({ op: "driver.buy", hours: Number(el.dataset.hours) }); },
    help: function () { toast("Линия, список, текущий рейс и профиль — как в боте"); },
    nav: function (el) { window.Maximus.openLink("https://yandex.ru/maps/?text=" + encodeURIComponent(el.dataset.q || "")); },
    "a-open": function (el) { picked = el.dataset.id; go("a-driver"); },
    "a-open-pass": function (el) { picked = el.dataset.id; go("a-passenger"); },
    "edit-person": function (el) {
      var id = el.dataset.id;
      var kind = el.dataset.kind;
      var list = kind === "driver" ? (S.admin.drivers || []) : (S.admin.passengers || []);
      var person = list.filter(function (x) { return String(x.user_id) === String(id); })[0];
      if (!person) { toast("Не найден"); return; }
      var html = "<h2>Изменить</h2>" +
        field("pname", "Имя", person.name, "") +
        field("pphone", "Телефон", person.phone, "");
      if (kind === "driver") {
        html += field("pmodel", "Модель", person.car_model, "") +
          field("pnumber", "Номер", person.car_number, "") +
          field("pcolor", "Цвет", person.car_color, "");
      }
      html += '<button class="btn" data-act="save-person" data-id="' + id + '" data-kind="' + kind + '">Сохранить</button>';
      sheet(html);
    },
    "save-person": function (el) {
      var name = val("pname");
      if (!name) { toast("Укажите имя"); return; }
      var body = { op: "admin.user.edit", id: Number(el.dataset.id), name: name, phone: val("pphone") };
      if (el.dataset.kind === "driver") {
        body.car_model = val("pmodel");
        body.car_number = val("pnumber");
        body.car_color = val("pcolor");
      }
      closeSheet();
      run(body);
    },
    "adm-delete": function (el) {
      var back = el.dataset.kind === "passenger" ? "a-passengers" : "a-drivers";
      run({ op: "admin.delete", id: Number(el.dataset.id) }).then(function (ok) {
        if (!ok) return;
        toast("Удалено");
        screen = back;
        render();
      });
    },
    "a-order": function (el) { picked = el.dataset.id; go("a-order"); },
    adm: function (el) {
      var body = { op: el.dataset.op, id: Number(el.dataset.id) };
      if (el.dataset.minutes) body.minutes = el.dataset.minutes === "" ? null : Number(el.dataset.minutes);
      if (el.dataset.hours) body.hours = Number(el.dataset.hours);
      if (el.dataset.seconds) body.seconds = Number(el.dataset.seconds);
      if (el.dataset.enabled != null) body.enabled = el.dataset.enabled === "1";
      if (el.dataset.mode) body.mode = el.dataset.mode;
      if (el.dataset.percent != null) body.percent = Number(el.dataset.percent);
      if (el.dataset.hidden != null) body.hidden = el.dataset.hidden === "1";
      run(body);
    },
    "adm-reject": function (el) {
      // Без window.prompt (в WebView MAX блокируется): форма в шторке.
      picked = Number(el.dataset.id);
      sheet("<h2>Отклонить заявку</h2>" +
        field("reason", "Причина отклонения", "", "не хватает данных") +
        '<button class="btn danger" data-act="adm-reject-do">Отклонить</button>');
    },
    "adm-reject-do": function () {
      var reason = val("reason") || "не хватает данных";
      closeSheet();
      run({ op: "admin.reject", id: picked, reason: reason });
    },
    "sheet-admin-balance": function (el) { sheet("<h2>Баланс</h2>" + field("amount", "Сумма", "100", "") + '<button class="btn" data-act="admin-balance" data-id="' + el.dataset.id + '">Начислить</button>'); },
    "admin-balance": function (el) { closeSheet(); run({ op: "admin.balance", id: Number(el.dataset.id), amount: Number(val("amount")) }); },
    "sheet-admin-price": function (el) { sheet("<h2>Цена заказа</h2>" + field("amount", "Новая цена", "", "") + '<button class="btn" data-act="admin-price" data-id="' + el.dataset.id + '">Сохранить</button>'); },
    "admin-price": function (el) { closeSheet(); run({ op: "admin.order.price", id: Number(el.dataset.id), price: Number(val("amount")) }); },
    "edit-tariff": function (el) {
      var city = (el && el.dataset && el.dataset.city) || "";
      var t = city && S.admin && S.admin.tariffs ? S.admin.tariffs[city] : null;
      sheet("<h2>" + (city ? "Тариф: " + esc(city) : "Новый город") + "</h2>" +
        (city ? '<input id="city" type="hidden" value="' + esc(city) + '">' : field("city", "Город", "", "Абакан")) +
        tariffInputs(t) +
        '<button class="btn" data-act="save-tariff">Сохранить</button>');
    },
    "save-tariff": function () {
      var city = val("city");
      if (!city) { toast("Укажите город"); return; }
      closeSheet();
      run(tariffBody({ op: "admin.tariff", city: city }));
    },
    "delete-tariff": function (el) {
      var city = el.dataset.city || "";
      if (!city) return;
      run({ op: "admin.tariff.delete", city: city }).then(function (ok) {
        if (ok) toast("Тариф удалён");
      });
    },
    "edit-intercity": function () {
      var t = (S.admin && S.admin.intercity) || {};
      sheet("<h2>Межгород</h2><p class=\"muted\">Считается, когда город подачи и город назначения разные.</p>" +
        tariffInputs(t) +
        '<button class="btn" data-act="save-intercity">Сохранить</button>');
    },
    "save-intercity": function () {
      closeSheet();
      run(tariffBody({ op: "admin.tariff.intercity" }));
    },
    "add-manager": function () { run({ op: "admin.manager.add", id: Number(val("mid")) }); },
    "add-admin": function () { run({ op: "admin.admin.add", phone: val("aphone") }); },
    "save-content": function () { run({ op: "admin.content", passenger: val("wp"), driver: val("wd"), support: val("ws") }); },
    "save-waiting": function () {
      var free = Number(val("wfree"));
      var per = Number(val("wper"));
      if (isNaN(free) || free < 0) { toast("Бесплатные минуты: число ≥ 0"); return; }
      if (isNaN(per) || per < 0) { toast("Цена за минуту: число ≥ 0"); return; }
      run({ op: "admin.tariff.waiting", free_minutes: free, per_minute: per })
        .then(function (ok) { if (ok) toast("Настройки ожидания сохранены"); });
    },
    noop: function () {}
  };

  document.body.addEventListener("click", function (e) {
    if (e.target.id === "sheet-back") { closeSheet(); return; }
    var el = e.target.closest("[data-act]");
    if (!el || !actions[el.dataset.act]) return;
    window.Maximus.haptic("light");
    actions[el.dataset.act](el);
  });

  window.Maximus.ready();
  var maxUser = window.Maximus.user();
  var start = window.Maximus.startParam();
  // Страховка: заставка не должна висеть дольше 6 секунд
  setTimeout(hideSplash, 6000);
  var urlParams = new URLSearchParams(window.location.search);
  var urlUid = urlParams.get('uid') || urlParams.get('user_id') || '';
  var urlToken = urlParams.get('token') || '';

  // Токен из ссылки бота — самый надёжный способ идентификации
  var activeToken = urlToken || savedToken() || savedTokenCookie();
  if (urlToken) rememberToken(urlToken);

  // Порядок определения пользователя:
  // 1) токен, 2) данные из MAX, 3) uid в URL, 4) сохранённый uid
  if (activeToken) {
    api("/api/act", { op: "login", token: activeToken }).then(function (data) {
      if (data.state) {
        S = normalizeState(data.state);
        rememberUid(S.user.id);
        rememberUidCookie(S.user.id);
        setSplashRole(S.user.role);
        screen = entryScreen();
        render();
        startKeepAlive();
        setTimeout(hideSplash, 420);
        return;
      }
      showPeople();
      setTimeout(hideSplash, 300);
    }).catch(function () {
      showPeople();
      setTimeout(hideSplash, 300);
    });
  } else {

  if (!maxUser && urlUid) {
    maxUser = { id: Number(urlUid), name: urlParams.get('name') || '', username: '', photo: '' };
  }
  if (!maxUser || !maxUser.id) {
    var stored = savedUid() || savedUidCookie();
    if (stored) {
      maxUser = { id: stored, name: "", username: "", photo: "" };
    }
  }
  if (maxUser && maxUser.id) {
    rememberUid(maxUser.id);
    rememberUidCookie(maxUser.id);
  }

  var stateUrl = "/api/state";
  if (maxUser && maxUser.id) {
    stateUrl += "?uid=" + maxUser.id;
  }
  api(stateUrl).then(function (data) {
    if (!data || data.ok === false) {
      // Нет сессии — спрашиваем сервер, кого он видит (cookie или бот)
      api("/api/whoami").then(function (who) {
        if (who && who.uid) { doLogin(who.uid, ""); return; }
        showPeople();
        setTimeout(hideSplash, 300);
      }).catch(function () {
        showPeople();
        setTimeout(hideSplash, 300);
      });
      return;
    }
    S = normalizeState(data);
    rememberUid(S.user.id);
    rememberUidCookie(S.user.id);
    setSplashRole(S.user.role);
    if (start === "driver" || start === "admin" || start === "passenger") {
      if (S.user.roles.indexOf(start) >= 0 && S.user.role !== start) {
        run({ op: "role", role: start }).then(function () {
          setSplashRole(S.user.role);
          screen = homeOf();
          render();
          setTimeout(hideSplash, 420);
        });
        return;
      }
    }
    screen = entryScreen();
    render();
    startKeepAlive();
    setTimeout(hideSplash, 420);
  }).catch(function (err) {
    // Не смогли получить состояние — пробуем войти по любому известному uid
    var fallbackId = (maxUser && maxUser.id) || savedUid() || savedUidCookie();
    if (!fallbackId) {
      // Последняя попытка: спросить сервер, кого он видит по cookie
      api("/api/whoami").then(function (who) {
        if (who && who.uid) {
          doLogin(who.uid, "");
          return;
        }
        showPeople();
        setTimeout(hideSplash, 300);
      }).catch(function () {
        showPeople();
        setTimeout(hideSplash, 300);
      });
      return;
    }
    doLogin(fallbackId, (maxUser && maxUser.name) || "");
  });
  }

  function doLogin(uid, name) {
    api("/api/act", { op: "login", max_id: uid, name: name }).then(function (data) {
      if (data.state) {
        S = normalizeState(data.state);
        rememberUid(uid);
        rememberUidCookie(uid);
        setSplashRole(S.user.role);
        screen = entryScreen();
        render();
        startKeepAlive();
        setTimeout(hideSplash, 420);
        return;
      }
      showPeople();
      setTimeout(hideSplash, 300);
    }).catch(function () {
      showPeople();
      setTimeout(hideSplash, 300);
    });
  }

  // ==================== KEEP-ALIVE ====================
  // Приложение не должно «засыпать»: держим сессию активной и
  // подтягиваем свежие данные, когда вкладка снова в фокусе.
  var _keepAliveTimer = null;
  var _lastPing = 0;

  function keepAliveTick() {
    // uid берём из MAX, а если приложение открыто по персональной ссылке
    // (token/uid в URL) — из уже загруженного состояния.
    var uid = (maxUser && maxUser.id) || (S && S.user && S.user.id) || savedUid() || savedUidCookie();
    if (!uid) return;
    var now = Date.now();
    // Пингуем не чаще раза в 20 секунд
    if (now - _lastPing < 20000) return;
    _lastPing = now;
    api("/api/act", { op: "session.ping", uid: uid }).catch(function () {});
  }

  function startKeepAlive() {
    stopKeepAlive();
    _keepAliveTimer = setInterval(keepAliveTick, 25000);
    keepAliveTick();
  }

  function stopKeepAlive() {
    if (_keepAliveTimer) {
      clearInterval(_keepAliveTimer);
      _keepAliveTimer = null;
    }
  }

  // Возврат в приложение: обновляем, но с защитой от циклов
  var _lastRefresh = 0;

  function softRefresh() {
    if (!S || !maxUser || !maxUser.id) return;
    // Не чаще раза в 3 секунды — иначе focus-события в iframe
    // вызывают бесконечную перерисовку
    var now = Date.now();
    if (now - _lastRefresh < 3000) return;
    _lastRefresh = now;
    // Не перерисовываем, если пользователь печатает
    var el = document.activeElement;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) return;
    refresh().catch(function () {});
  }

  document.addEventListener("visibilitychange", function () {
    // Возврат в приложение: сразу подтягиваем свежие данные.
    // Keep-alive НЕ останавливаем — приложение не должно «засыпать».
    if (!document.hidden) {
      startKeepAlive();
      softRefresh();
    }
  });

  window.addEventListener("focus", function () {
    startKeepAlive();
    softRefresh();
  });

  // Останавливаем пинг только когда страницу реально закрывают
  window.addEventListener("pagehide", stopKeepAlive);

  // Обновление раз в 3 секунды на активных экранах — как было
  // ==================== УВЕДОМЛЕНИЯ ====================
  var _lastStatus = {};       // order_id -> последний известный статус
  var _notifySeen = {};       // чтобы не дублировать
  var _offerShown = {};       // чтобы предложение избранного показывать один раз

  var STATUS_TEXT = {
    accepted: "Водитель принял заказ и едет к вам",
    driver_arrived: "Водитель прибыл на место",
    in_progress: "Поездка началась",
    completed: "Поездка завершена",
    cancelled: "Заказ отменён"
  };

  var _chatSeen = {};
  // Подписи сообщений, отправленных С ЭТОГО устройства — их не показываем
  // во всплывающем окне. Раньше фильтр был по user_id: если пассажир и
  // водитель — один аккаунт (тест), уведомления не приходили вообще.
  var _myChatSent = {};
  var _chatCursor = "";        // ISO-время последнего полученного сообщения
  var _chatPolling = false;
  var _chatReady = false;      // первый опрос только задаёт курсор
  var _lastPopupAt = {};       // order_id -> время последнего окна (антидубль)

  function chatSig(m) {
    if (!m) return "";
    return String(m.at || "") + "|" + String(m.from || "") + "|" + String(m.text || "");
  }

  function noteMyMessage(orderId, chat) {
    var list = chat || [];
    var last = list.length ? list[list.length - 1] : null;
    if (!last) return;
    var sig = chatSig(last);
    _myChatSent[orderId] = _myChatSent[orderId] || {};
    _myChatSent[orderId][sig] = true;
    _chatSeen[orderId] = { n: list.length, sig: sig };
    // Курсор здесь НЕ двигаем: иначе наше сообщение будет пропущено
    // и на «своём» аккаунте уведомление не покажется никогда.
    // Фильтр своих сообщений делает pollChat по сигнатуре.
  }

  function openDialog(orderId) {
    picked = Number(orderId);
    var role = S && S.user && S.user.role;
    screen = role === "driver" ? "d-current" : "live";
    render();
    if (actions["sheet-chat"]) actions["sheet-chat"]({ dataset: { id: String(orderId) } });
  }

  // Опрос новых сообщений через /api/chat/updates.
  // Надёжнее сравнения /api/state: там чат мог не обновиться
  // (пропуск при inflight, другая роль, ограничение выборки заказов).
  function pollChat() {
    if (!S || !S.user || !S.user.id) return;
    if (_chatPolling) return;
    _chatPolling = true;
    var url = "/api/chat/updates?uid=" + encodeURIComponent(S.user.id);
    if (_chatCursor) url += "&after=" + encodeURIComponent(_chatCursor);
    api(url).then(function (data) {
      _chatPolling = false;
      if (!data || data.ok === false) return;
      var msgs = data.messages || [];
      if (!_chatReady) {
        // Первый опрос: только запоминаем курсор, старую историю не показываем.
        _chatReady = true;
        var lastAt = "";
        for (var q = 0; q < msgs.length; q++) {
          if (msgs[q] && msgs[q].at && msgs[q].at > lastAt) lastAt = msgs[q].at;
        }
        _chatCursor = lastAt || data.server_time || "";
        return;
      }
      if (!msgs.length) return;
      var me = String(S.user.id || "");
      var role = S.user.role;
      var grouped = {};
      var orderIds = [];
      for (var i = 0; i < msgs.length; i++) {
        var m = msgs[i];
        if (!m || !m.text) continue;
        if (m.at && m.at > _chatCursor) _chatCursor = m.at;
        // своё сообщение (отправлено с этого устройства) — пропускаем
        var own = (_myChatSent[m.order_id] || {})[chatSig(m)];
        // Когда пассажир и водитель — один аккаунт (тест на одном телефоне),
        // своё сообщение тоже показываем: иначе уведомлений не будет вообще.
        if (own && !m.same_account) continue;
        if (!grouped[m.order_id]) { grouped[m.order_id] = []; orderIds.push(m.order_id); }
        grouped[m.order_id].push(m.text);
      }
      for (var k = 0; k < orderIds.length; k++) {
        var oid = orderIds[k];
        var texts = grouped[oid];
        _lastPopupAt[oid] = Date.now();
        taxiBeep();
        showMsgPopup(texts.join(" "),
          (role === "driver" ? "Пассажир" : "Водитель") + " · заказ #" + oid,
          (function (id) { return function () { openDialog(id); }; })(oid));
      }
    }).catch(function () { _chatPolling = false; });
  }

  function checkIncomingChat(norm) {
    // Резервный путь: если /api/chat/updates недоступен — сравниваем
    // состояние, как раньше (по числу сообщений и подписи последнего).
    if (!norm || !norm.user) return;
    var role = norm.user.role;
    var orders = [];
    if (role === "driver") {
      if (norm.driver && norm.driver.current) orders.push(norm.driver.current);
    } else if (norm.passenger) {
      orders = (norm.passenger.orders || []).slice();
      if (norm.passenger.active) orders.push(norm.passenger.active);
    }
    var seenIds = {};
    for (var i = 0; i < orders.length; i++) {
      var o = orders[i];
      if (!o || !o.id || seenIds[o.id]) continue;
      seenIds[o.id] = true;
      var chat = o.chat || [];
      var last = chat.length ? chat[chat.length - 1] : null;
      var sig = chatSig(last);
      var prev = _chatSeen[o.id];
      _chatSeen[o.id] = { n: chat.length, sig: sig };
      if (!prev || prev.sig === sig) continue;
      var start = chat.length > prev.n ? prev.n : Math.max(0, chat.length - 1);
      var mine = _myChatSent[o.id] || {};
      var lines = [];
      var fresh = chat.slice(start);
      for (var j = 0; j < fresh.length; j++) {
        var m = fresh[j];
        if (!m || !m.text) continue;
        if (mine[chatSig(m)] && !o.same_account) continue;
        lines.push(m.text);
      }
      if (!lines.length) continue;
      taxiBeep();
      showMsgPopup(lines.join(" "),
        (role === "driver" ? "Пассажир" : "Водитель") + " · заказ #" + o.id,
        (function (id) { return function () { openDialog(id); }; })(o.id));
    }
  }

  function confirmAction(text, onYes) {
    // Оставлено для совместимости: в WebView MAX нативные confirm/prompt
    // могут блокироваться, поэтому действия удаления выполняем напрямую.
    if (typeof onYes === "function") onYes();
  }

  // ==================== ВСПЛЫВАЮЩЕЕ ОКНО СООБЩЕНИЯ ====================
  // Отдельное от обычного баннера: НЕ исчезает само — только по крестику
  // или по нажатию на само окно (тогда открывается диалог).
  // Так водитель/пассажир не пропустит новое сообщение.
  function showMsgPopup(text, who, onOpen) {
    var el = document.getElementById("msg-popup");
    if (!el) {
      // Создаём контейнер один раз (в index.html его может не быть)
      el = document.createElement("div");
      el.id = "msg-popup";
      el.className = "msg-popup";
      el.hidden = true;
      document.body.appendChild(el);
    }
    el.innerHTML =
      '<div class="msg-popup-card" role="alertdialog">' +
        '<div class="msg-popup-head">' +
          '<i>💬</i><b>' + esc(who || "Новое сообщение") + '</b>' +
          '<button type="button" class="msg-popup-close" data-popup-close="1" aria-label="Закрыть">✕</button>' +
        '</div>' +
        '<div class="msg-popup-text">' + esc(text) + '</div>' +
        '<div class="msg-popup-hint">Нажмите, чтобы открыть диалог</div>' +
      '</div>';
    el.hidden = false;
    el.onclick = function (e) {
      var closer = e.target && e.target.closest && e.target.closest("[data-popup-close]");
      if (closer) {
        e.preventDefault();
        e.stopPropagation();
        el.hidden = true;
        return;
      }
      // нажатие на само окно — открыть диалог
      el.hidden = true;
      if (typeof onOpen === "function") onOpen();
    };
  }

  function showBanner(text, kind, onTap) {
    var el = document.getElementById("notify-banner");
    if (!el) return;
    el.className = "notify-banner " + (kind || "");
    el.innerHTML = '<i>🔔</i><span>' + esc(text) + '</span>' +
      '<button type="button" data-banner-close="1">✕</button>';
    el.classList.add("show");
    el.onclick = function (e) {
      var closer = e.target && e.target.closest && e.target.closest("[data-banner-close]");
      el.classList.remove("show");
      clearTimeout(showBanner._t);
      if (closer) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (typeof onTap === "function") onTap();
    };
    if (window.Maximus && window.Maximus.haptic) window.Maximus.haptic("ok");
    clearTimeout(showBanner._t);
    // Для сообщений (передан onTap) баннер не закрываем автоматически,
    // чтобы не пропустить входящее сообщение.
    if (typeof onTap === "function") return;
    showBanner._t = setTimeout(function () {
      el.classList.remove("show");
    }, 4000);
  }

  var _lastRoute = {};

  function checkStatusChanges(data) {
    var norm = normalizeState(data);
    var orders = [];

    // Заказы пассажира
    if (norm.passenger && norm.passenger.orders) {
      orders = orders.concat(norm.passenger.orders);
    }
    // Текущий заказ водителя
    if (norm.driver && norm.driver.current) {
      orders.push(norm.driver.current);
    }

    for (var i = 0; i < orders.length; i++) {
      var o = orders[i];
      if (!o || !o.id) continue;
      var key = "o" + o.id;
      var prev = _lastStatus[key];
      var cur = o.status;
      _lastStatus[key] = cur;

      var routeSig = (o.destination || "") + "|" + ((o.waypoints || []).length) + "|" + (o.price || "");
      var prevRoute = _lastRoute[key];
      _lastRoute[key] = routeSig;
      if (prevRoute && prevRoute !== routeSig && (screen === "live" || screen === "d-current")) {
        render();
        showBanner("Маршрут заказа #" + o.id + " изменён");
      }

      if (!prev || prev === cur) continue;

      // Пассажир, чей заказ завершился: показываем детали + предложение
      if (cur === "completed" && S && S.user && S.user.role &&
          S.user.role === "passenger" && _offerShown["o" + o.id] !== true) {
        _offerShown["o" + o.id] = true;
        picked = Number(o.id);
        window._favOrder = Number(o.id);
        screen = "fav-offer";
        render();
        showBanner("Поездка #" + o.id + " завершена", "ok");
        continue;
      }

      // Показываем уведомление только о значимых переходах
      var msg = STATUS_TEXT[cur];
      if (!msg) continue;
      var dedup = key + "_" + cur;
      if (_notifySeen[dedup]) continue;
      _notifySeen[dedup] = true;

      var text = msg + " · заказ #" + o.id;
      if (cur === "completed" && o.price) {
        text = "Поездка #" + o.id + " завершена · " + money(o.price);
      }
      if (cur === "completed") {
        showBanner(text, "ok");
      } else if (cur === "cancelled") {
        showBanner(text, "err");
      } else {
        // Водитель принял / прибыл / поехали — свой сигнал, не просто вибрация.
        alertOnce(dedup, text);
      }
    }

    checkNoDriverPrompt(orders);
  }

  // ==================== ВОДИТЕЛЬ НЕ НАЙДЕН (10 минут) ====================
  // Дублируем логику бота: если заказ висит в pending дольше 10 минут и
  // водителя нет — показываем «ждать или отменить» и играем свой звук.
  var NO_DRIVER_ASK_MINUTES = 10;
  var NO_DRIVER_REPEAT_MINUTES = 10;
  var _noDriverPrompted = {};   // order_id -> время последнего показа

  function checkNoDriverPrompt(orders) {
    if (!S || !S.user || S.user.role !== "passenger") return;
    for (var i = 0; i < orders.length; i++) {
      var o = orders[i];
      if (!o || !o.id || o.status !== "pending" || o.driver_id) continue;
      var mins = pendingMinutes(o);
      if (mins < NO_DRIVER_ASK_MINUTES) continue;
      var last = _noDriverPrompted[o.id] || 0;
      if (last && (Date.now() - last) / 60000 < NO_DRIVER_REPEAT_MINUTES) continue;
      _noDriverPrompted[o.id] = Date.now();
      picked = Number(o.id);
      screen = "no-driver";
      render();
      taxiBeep();
      showBanner("Водитель пока не найден — заказ #" + o.id, "err");
      return;
    }
  }

  // ==================== АВТООБНОВЛЕНИЕ КОДА ====================
  // WebView в MAX кэширует app.js агрессивно: без этой проверки телефон
  // продолжает работать на старой версии и правки «не приходят».
  // Сравниваем build с сервером; если изменился — перезагружаем страницу.
  var _buildSeen = "";

  function checkBuild() {
    api("/api/version").then(function (v) {
      var build = (v && v.build) || "";
      if (!build) return;
      if (!_buildSeen) { _buildSeen = build; return; }
      if (build !== _buildSeen) {
        _buildSeen = build;
        toast("Обновление приложения…");
        setTimeout(function () { location.reload(); }, 600);
      }
    }).catch(function () {});
  }

  // Опрос состояния — уведомления без перерисовки, чтобы не стирать ввод
  setInterval(function () {
    if (!S || inflight) return;
    var stateUrl = "/api/state";
    if (maxUser && maxUser.id) stateUrl += "?uid=" + maxUser.id;
    api(stateUrl).then(function (data) {
      if (inflight) return;
      if (!data || data.ok === false) return;
      // Сначала обновляем S, потом обрабатываем переходы.
      // Иначе fav-offer рендерится со старым состоянием, где завершённого
      // заказа ещё нет, и детали поездки показываются пустыми.
      S = normalizeState(data);
      // Смена статусов и чат — независимо: если один упадёт,
      // второй всё равно отработает (иначе уведомления терялись).
      try { checkStatusChanges(data); } catch (e) {}
      try { checkIncomingChat(S); } catch (e) {}
    }).catch(function () {});
  }, 5000);

  // Отдельный частый опрос чата: всплывающие окна о новых сообщениях.
  setInterval(function () {
    if (!S || !S.user || !S.user.id) return;
    try { pollChat(); } catch (e) {}
  }, 3000);

  // Проверка версии кода: раз в 30 секунд.
  setInterval(function () {
    try { checkBuild(); } catch (e) {}
  }, 30000);


  function showPeople() {
    api("/api/people").then(function (data) {
      $("top").innerHTML = header();
      $("tabs").innerHTML = "";
      $("view").innerHTML = views.login(data.people);
    }).catch(function () {
      $("top").innerHTML = header();
      $("view").innerHTML = "<h2>Maximus Taxi</h2><p class=\"muted\">Откройте приложение из чата с ботом в MAX.</p>";
    });
  }
})();
