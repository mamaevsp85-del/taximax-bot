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
  function field(id, label, value, placeholder) {
    return '<label class="field"><span>' + label + '</span><input id="' + id + '" value="' + esc(value || "") + '" placeholder="' + esc(placeholder || "") + '"></label>';
  }

  var views = {
    login: function (people) {
      return "<h2>Кто вы</h2><p class=\"muted\">Это живые профили из базы бота. В MAX откроется ваш аккаунт сам.</p>" +
        people.map(function (p) {
          return row(p.name, p.roles.join(" · "), "login", { id: p.id });
        }).join("");
    },
    roles: function () {
      var names = { passenger: ["Пассажир", "Заказ, история, избранное"], driver: ["Водитель", "Линия, рейсы, баланс"], admin: ["Админ", "Водители, заказы, настройки"] };
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
        '<button class="btn" data-act="pickup-next">Дальше</button>';
      if (step === 2) return "<h2>Куда</h2><div class=\"steps\">" + bars + "</div><p class=\"muted\">Подача: " + esc(draft.pickup) + "</p>" +
        field("dest", "Назначение", draft.destination, "Город, улица, дом") + '<button class="btn" data-act="dest-next">Посчитать</button>';
      if (draft.pending) return "<h2>Подтверждение</h2><div class=\"steps\">" + bars + "</div><div class=\"card\" style=\"margin-top:0\"><div class=\"route\">" +
        esc(draft.pickup) + "<small>" + esc(draft.destination) + "</small></div><p class=\"muted\">Считаю маршрут…</p></div>";
      return "<h2>Подтверждение</h2><div class=\"steps\">" + bars + "</div><div class=\"card\" style=\"margin-top:0\"><div class=\"route\">" +
        esc(draft.pickup) + (draft.stop ? "<small>через " + esc(draft.stop) + "</small>" : "") + "<small>" + esc(draft.destination) +
        '</small></div><div class="row" style="margin-top:12px"><span class="muted">' + draft.km + " км · ~" + draft.minutes +
        ' мин</span><div class="price">' + money(draft.price) + "</div></div></div>" +
        '<button class="btn" data-act="confirm">Подтвердить заказ</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="sheet-price">Своя цена</button><button class="btn ghost" data-act="sheet-stop">Остановка</button></div>';
    },
    live: function () {
      var o = (S.passenger.orders || []).filter(function (x) { return x.id === picked; })[0] || S.passenger.active;
      if (!o) return "<h2>Заказ</h2><p class=\"muted\">Заказ не найден.</p>";
      var canCancel = ["pending", "accepted", "driver_arrived"].indexOf(o.status) >= 0;
      var driver = o.driver_name ? '<div class="card"><b>' + esc(o.driver_name) + '</b><p class="muted">' +
        esc([o.car_model, o.car_number, o.car_color].filter(Boolean).join(" · ")) + "<br>" + esc(o.driver_phone) + "</p>" +
        '<div class="btn-row"><button class="btn ghost" data-act="sheet-chat" data-id="' + o.id + '">Написать</button>' +
        '<button class="btn ghost" data-act="call" data-phone="' + esc(o.driver_phone) + '">Позвонить</button></div></div>' : "";
      return '<div class="row"><h2 style="margin:0">Заказ #' + o.id + "</h2>" + badge(o.status) + "</div>" +
        '<div class="card"><div class="route">' + esc(o.pickup) + "<small>" + esc(o.destination) + "</small></div>" +
        '<div class="row" style="margin-top:10px"><span class="muted">' + Number(o.distance).toFixed(1) + " км</span><b>" + money(o.price) + "</b></div></div>" +
        driver +
        (canCancel ? '<button class="btn ghost" data-act="sheet-price-live" data-id="' + o.id + '">Предложить свою цену</button>' +
          '<button class="btn ghost" data-act="sheet-complaint" data-id="' + o.id + '">Жалоба на авто</button>' +
          '<button class="btn danger" data-act="cancel" data-id="' + o.id + '">Отменить заказ</button>' : "") +
        (o.status === "completed" && !o.rating ? '<button class="btn yellow" data-act="to-rate" data-id="' + o.id + '">Оценить поездку</button>' : "") +
        (o.status === "completed" ? '<button class="btn ghost" data-act="fav-add" data-id="' + o.id + '">В избранное</button>' : "") +
        (o.complaint ? '<p class="note">Жалоба: ' + esc(o.complaint) + "</p>" : "");
    },
    orders: function () {
      var list = (S.passenger.orders || []).filter(function (o) { return ["pending", "accepted", "driver_arrived", "in_progress"].indexOf(o.status) >= 0; });
      return "<h2>Мои заказы</h2>" + (list.length ? list.map(function (o) { return orderCard(o, "open-order"); }).join("") : '<p class="list-empty">Активных заказов нет</p>');
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
        return row(f.label || "Маршрут", "", "use-fav", { index: i });
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
        '<p class="muted">Откуда вы обычно заказываете такси.</p>' +
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
      if (S.user.driver_status === "pending") return "<h2>Заявка на проверке</h2><p class=\"muted\">Админ увидит её в модерации.</p>";
      if (S.user.driver_status === "approved") return "<h2>Вы уже водитель</h2><button class=\"btn\" data-act=\"role\" data-role=\"driver\">Открыть меню водителя</button>";
      var note = S.user.driver_status === "rejected" ? '<p class="note">Прошлая заявка отклонена: ' + esc(S.user.reject_reason || "без причины") + "</p>" : "";
      return "<h2>Анкета водителя</h2>" + note + field("car", "Марка и модель", "", "Kia Rio") +
        field("plate", "Госномер", "", "А123ВС 19") + field("color", "Цвет", "", "белый") +
        '<button class="btn" data-act="apply">Отправить на проверку</button>';
    },
    rate: function () {
      var stars = [1, 2, 3, 4, 5].map(function (n) {
        return '<button data-act="star" data-n="' + n + '">' + (n <= (window._star || 0) ? "★" : "☆") + "</button>";
      }).join("");
      return "<h2>Оценка поездки #" + picked + '</h2><div class="stars">' + stars + '</div><button class="btn" data-act="save-rate">Сохранить</button>';
    },
    driver: function () {
      var u = S.user;
      var line = u.line === "online" ? "На линии" : u.line === "break" ? "Перерыв" : "Оффлайн";
      return '<div class="card" style="margin-top:0"><div class="row"><div><b>' + esc(u.driver_name || u.name) +
        '</b><div class="muted">' + esc([u.car_model, u.car_number].filter(Boolean).join(" · ")) + "</div></div>" +
        '<span class="badge ' + (u.line === "online" ? "ok" : "off") + '">' + line + "</span></div>" +
        '<div class="seg" style="margin-top:12px">' +
        seg("online", "На линии") + seg("break", "Перерыв") + seg("offline", "Оффлайн") + "</div></div>" +
        (S.driver.current ? orderCard(S.driver.current, "d-open") : "") +
        group("На линии") +
        '<div class="grid">' +
        tile("d-orders", "☰", "Список заказов", "свободные рядом") +
        tile("d-current", "▶", "Текущий рейс", S.driver.current ? "заказ #" + S.driver.current.id : "нет рейса") +
        tile("d-profile", "☺", "Профиль", "авто, баланс, документы") +
        "</div>";
    },
    "d-orders": function () {
      if (S.user.line !== "online") return "<h2>Список заказов</h2><p class=\"list-empty\">Сначала выйдите на линию</p>";
      var list = S.driver.available || [];
      return "<h2>Список заказов</h2>" + (list.length ? list.map(function (o) {
        return '<div class="card">' + orderCard(o, "noop") + '<button class="btn" data-act="accept" data-id="' + o.id + '">Принять</button></div>';
      }).join("") : '<p class="list-empty">Свободных заказов нет</p>');
    },
    "d-current": function () {
      var o = S.driver.current;
      if (!o) return "<h2>Текущие заказы</h2><p class=\"list-empty\">Нет активного рейса</p>";
      var actions = "";
      if (o.status === "accepted") actions = '<button class="btn" data-act="arrived" data-id="' + o.id + '">Я прибыл</button>';
      if (o.status === "driver_arrived") actions = '<button class="btn" data-act="start-trip" data-id="' + o.id + '">Начать движение</button><button class="btn ghost" data-act="wait" data-id="' + o.id + '">Ожидание</button>';
      if (o.status === "in_progress") actions = '<button class="btn" data-act="complete" data-id="' + o.id + '">Завершить поездку</button>';
      return '<div class="row"><h2 style="margin:0">Рейс #' + o.id + "</h2>" + badge(o.status) + "</div>" +
        '<div class="card"><div class="route">' + esc(o.pickup) + "<small>" + esc(o.destination) + "</small></div>" +
        '<p class="muted">Пассажир ' + esc(o.passenger_name) + " · " + esc(o.passenger_phone) + "</p><b>" + money(o.price) + "</b></div>" +
        actions + '<div class="btn-row"><button class="btn ghost" data-act="nav" data-q="' + esc(o.status === "in_progress" ? o.destination : o.pickup) +
        '">Навигация</button><button class="btn ghost" data-act="sheet-chat" data-id="' + o.id + '">Пассажир</button></div>' +
        (["accepted", "driver_arrived", "in_progress", "pending"].indexOf(o.status) >= 0 ? '<button class="btn danger" data-act="d-cancel" data-id="' + o.id + '">Отменить заказ</button>' : "");
    },
    "d-profile": function () {
      var hidden = S.user.balance_hidden;
      return "<h2>Профиль</h2><div class=\"card\"><b>" + esc(S.user.driver_name) + '</b><p class="muted">' +
        esc(S.user.driver_phone) + "<br>" + esc(S.user.car_model) + " · " + esc(S.user.car_number) + " · " + esc(S.user.car_color) +
        (hidden ? "" : "<br>Баланс " + money(S.user.balance)) + "<br>Доступ " + (S.user.access ? "есть" : "нет") + "</p></div>" +
        group("Машина") +
        '<div class="grid">' +
        tile("d-car", "▣", "Автомобиль", "марка, номер, цвет") +
        tile("d-docs", "▣", "Документы", "фото для проверки") +
        tile("d-stats", "▦", "Статистика", "день и месяц") +
        tile("d-history", "↺", "История", "закрытые рейсы") +
        "</div>" +
        group("Аккаунт") +
        row("Имя", S.user.driver_name, "sheet-dname", null, "☺") +
        row("Телефон", S.user.driver_phone, "sheet-dphone", null, "#") +
        (hidden ? "" : row("Баланс", money(S.user.balance), "tab", { screen: "d-balance" }, "₽")) +
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
      return "<h2>" + esc(d.name) + "</h2><p class=\"muted\">" + esc(d.phone) + "<br>" + esc(d.car_model) + " " + esc(d.car_number) +
        " " + esc(d.car_color) + "<br>Баланс " + money(d.balance) + " · " + esc(d.verification_status) + "</p>" +
        '<div class="btn-row"><button class="btn" data-act="adm" data-op="admin.approve" data-id="' + d.user_id + '">Одобрить</button>' +
        '<button class="btn danger" data-act="adm-reject" data-id="' + d.user_id + '">Отклонить</button></div>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.unblock" data-id="' + d.user_id + '">Разблокировать</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="adm" data-op="admin.block" data-id="' + d.user_id + '" data-minutes="60">Блок 1 ч</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.block" data-id="' + d.user_id + '">Блок навсегда</button></div>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.priority" data-id="' + d.user_id + '" data-seconds="3600">Приоритет 1 ч</button>' +
        '<div class="btn-row"><button class="btn ghost" data-act="adm" data-op="admin.grant" data-id="' + d.user_id + '" data-hours="8">Смена 8 ч</button>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.unlim" data-id="' + d.user_id + '">Безлимит</button></div>' +
        '<button class="btn ghost" data-act="adm" data-op="admin.resetacc" data-id="' + d.user_id + '">Обнулить доступ</button>' +
        '<button class="btn ghost" data-act="sheet-admin-balance" data-id="' + d.user_id + '">Пополнить баланс</button>' +
        '<button class="btn danger" data-act="adm" data-op="admin.delete" data-id="' + d.user_id + '">Удалить</button>';
    },
    "a-moderation": function () {
      var list = S.admin.pending || [];
      return "<h2>Модерация</h2>" + (list.length ? list.map(function (d) {
        return row(d.name, d.car_number || "без номера", "a-open", { id: d.user_id });
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
      return "<h2>Пассажиры</h2>" + (S.admin.passengers || []).map(function (p) {
        return '<div class="card"><b>' + esc(p.name) + '</b><p class="muted">' + esc(p.phone) + " · заказов " + p.orders_count +
          " · " + money(p.total_spent) + '</p><button class="btn ghost" data-act="adm" data-op="admin.passenger.block" data-id="' +
          p.user_id + '" data-minutes="60">Блок 1 ч</button></div>';
      }).join("");
    },
    "a-tariffs": function () {
      var cities = S.admin.tariffs || {};
      return "<h2>Тарифы</h2>" + Object.keys(cities).map(function (c) {
        return '<div class="doc"><span>' + esc(c) + "</span><b>" + (cities[c].price_per_km || "") + " ₽/км</b></div>";
      }).join("") + field("city", "Город", "", "Абакан") + field("ppk", "Цена за км", "", "35") +
        '<button class="btn" data-act="save-tariff">Сохранить тариф</button>';
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
      return "<h2>Настройки</h2><p class=\"muted\">Балансы у водителей: " + (S.admin.balances_disabled ? "скрыты" : "показаны") + "</p>" +
        '<button class="btn" data-act="adm" data-op="admin.balances" data-hidden="' + (S.admin.balances_disabled ? "0" : "1") + '">' +
        (S.admin.balances_disabled ? "Показать балансы" : "Скрыть балансы") + "</button>";
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
        document.cookie = UID_KEY + "=" + n + "; path=/; max-age=31536000; SameSite=Lax";
      } catch (e) {}
    }
    return n;
  }

  // ==================== АВТОДОПОЛНЕНИЕ АДРЕСА ====================
  var _suggestTimer = null;
  var _suggestSeq = 0;

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
          "<i>" + icon + "</i><span><b>" + esc(it.title) + "</b>" +
          "<small>" + esc(it.subtitle || it.address) + "</small></span></button>";
      }
      box.innerHTML = html;
      box.hidden = false;

      var btns = box.querySelectorAll(".suggest-item");
      for (var k = 0; k < btns.length; k++) {
        (function (btn) {
          btn.addEventListener("click", function (e) {
            e.preventDefault();
            var it = items[Number(btn.dataset.i)];
            input.value = it.title + (it.subtitle ? ", " + it.subtitle.split(",")[0] : "");
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
    $("top").innerHTML = header();
    $("tabs").innerHTML = tabs();
    var fn = views[screen];
    $("view").innerHTML = fn ? fn() : "<p>Экран не найден</p>";

    // Подключаем автодополнение адреса на нужных экранах
    if (screen === "trip") {
      var step = (draft && draft.step) || 1;
      if (step === 1) {
        attachSuggest("pickup", function (it) {
          draft.pickup = it.title + (it.subtitle ? ", " + it.subtitle.split(",")[0] : "");
        });
      } else if (step === 2) {
        attachSuggest("dest", function (it) {
          draft.destination = it.title + (it.subtitle ? ", " + it.subtitle.split(",")[0] : "");
        });
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
      render();
      api("/api/act", {
        op: "quote",
        pickup: draft.pickup,
        destination: dest,
        stop: draft.stop || "",
        pickup_coords: draft.pickup_coords || null
      }).then(function (data) {
        draft = data.draft; draft.step = 3; draft.pending = false; render();
      }).catch(function (e) { draft.step = 2; draft.pending = false; toast(e.message); render(); });
    },
    "sheet-price": function () { sheet("<h2>Своя цена</h2>" + field("own", "Сумма", draft.price, "220") + '<button class="btn" data-act="apply-price">Поставить</button>'); },
    "apply-price": function () {
      var n = Number(val("own"));
      if (n < 50) { toast("Минимум 50 ₽"); return; }
      draft.price = n; closeSheet(); render();
    },
    "sheet-stop": function () { sheet("<h2>Остановка</h2>" + field("stop", "Адрес", draft.stop || "", "") + '<button class="btn" data-act="apply-stop">Пересчитать</button>'); },
    "apply-stop": function () {
      var stop = val("stop");
      closeSheet();
      draft.stop = stop;
      draft.pending = true;
      render();
      api("/api/act", { op: "quote", pickup: draft.pickup, destination: draft.destination, stop: stop }).then(function (data) {
        draft = data.draft; draft.step = 3; draft.pending = false; render();
      }).catch(function (e) { draft.pending = false; toast(e.message); render(); });
    },
    confirm: async function () {
      var ok = await run({ op: "order.create", draft: draft, price: draft.price });
      if (ok && S.passenger.active) { draft = null; picked = S.passenger.active.id; screen = "live"; render(); }
    },
    "open-order": function (el) { picked = Number(el.dataset.id); go("live"); },
    cancel: function (el) { run({ op: "order.cancel", id: Number(el.dataset.id) }); },
    "sheet-price-live": function (el) { sheet("<h2>Своя цена</h2>" + field("own", "Сумма", "", "220") + '<button class="btn" data-act="save-live-price" data-id="' + el.dataset.id + '">Сохранить</button>'); },
    "save-live-price": function (el) { closeSheet(); run({ op: "order.price", id: Number(el.dataset.id), price: Number(val("own")) }); },
    "sheet-complaint": function (el) { sheet("<h2>Жалоба</h2>" + field("complaint", "Что не совпало", "", "номер, цвет, водитель") + '<button class="btn" data-act="save-complaint" data-id="' + el.dataset.id + '">Отправить</button>'); },
    "save-complaint": function (el) { closeSheet(); run({ op: "order.complaint", id: Number(el.dataset.id), text: val("complaint") }); },
    "fav-add": function (el) { run({ op: "favorite.add", id: Number(el.dataset.id) }).then(function () { toast("В избранном"); }); },
    "use-fav": function (el) { run({ op: "favorite.use", index: Number(el.dataset.index) }); },
    "to-rate": function (el) { picked = Number(el.dataset.id); window._star = 0; go("rate"); },
    star: function (el) { window._star = Number(el.dataset.n); render(); },
    "save-rate": function () { run({ op: "order.rate", id: picked, stars: window._star || 0 }).then(function () { screen = "home"; }); },
    "sheet-chat": function (el) {
      picked = Number(el.dataset.id);
      var pool = [].concat((S.passenger && S.passenger.orders) || [], (S.driver && S.driver.current) ? [S.driver.current] : [], (S.admin && S.admin.orders) || []);
      var o = pool.filter(function (x) { return x && x.id === picked; })[0] || {};
      var bubbles = (o.chat || []).map(function (m) {
        return '<div class="bubble' + (m.from === S.user.id ? " me" : "") + '">' + esc(m.text) + "</div>";
      }).join("");
      sheet("<h2>Сообщения</h2><div class=\"chat\">" + (bubbles || '<p class="muted">Пока пусто</p>') + "</div>" +
        field("msg", "Текст", "", "") + '<button class="btn" data-act="send-msg" data-id="' + picked + '">Отправить</button>');
    },
    "send-msg": function (el) { closeSheet(); run({ op: "order.chat", id: Number(el.dataset.id), text: val("msg") }); },
    call: function (el) { toast(el.dataset.phone ? "Номер " + el.dataset.phone : "Номера нет"); },
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
      if (city.length < 2) { toast("Укажите город"); return; }
      run({
        op: "profile",
        name: window._onbName || val("onb_name"),
        phone: window._onbPhone || val("onb_phone"),
        home_city: city
      }).then(function (ok) {
        if (!ok) return;
        window._onbStep = null;
        toast("Профиль сохранён");
        stack = [];
        screen = homeOf();
        render();
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
    apply: function () { run({ op: "driver.apply", car: val("car"), plate: val("plate"), color: val("color") }); },
    period: function (el) { period = el.dataset.p; render(); },
    dperiod: function (el) { driverPeriod = el.dataset.p; render(); },
    line: function (el) { run({ op: "driver.line", status: el.dataset.status }); },
    accept: function (el) { screen = "d-current"; run({ op: "driver.accept", id: Number(el.dataset.id) }); },
    "d-open": function (el) { screen = "d-current"; render(); },
    arrived: function (el) { run({ op: "driver.arrived", id: Number(el.dataset.id) }); },
    wait: function (el) { run({ op: "driver.wait", id: Number(el.dataset.id) }); },
    "start-trip": function (el) { run({ op: "driver.start", id: Number(el.dataset.id) }); },
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
      var reason = window.prompt("Причина отклонения", "не хватает данных");
      if (reason == null) return;
      run({ op: "admin.reject", id: Number(el.dataset.id), reason: reason });
    },
    "sheet-admin-balance": function (el) { sheet("<h2>Баланс</h2>" + field("amount", "Сумма", "100", "") + '<button class="btn" data-act="admin-balance" data-id="' + el.dataset.id + '">Начислить</button>'); },
    "admin-balance": function (el) { closeSheet(); run({ op: "admin.balance", id: Number(el.dataset.id), amount: Number(val("amount")) }); },
    "sheet-admin-price": function (el) { sheet("<h2>Цена заказа</h2>" + field("amount", "Новая цена", "", "") + '<button class="btn" data-act="admin-price" data-id="' + el.dataset.id + '">Сохранить</button>'); },
    "admin-price": function (el) { closeSheet(); run({ op: "admin.order.price", id: Number(el.dataset.id), price: Number(val("amount")) }); },
    "save-tariff": function () { run({ op: "admin.tariff", city: val("city"), price_per_km: Number(val("ppk")) }); },
    "add-manager": function () { run({ op: "admin.manager.add", id: Number(val("mid")) }); },
    "add-admin": function () { run({ op: "admin.admin.add", phone: val("aphone") }); },
    "save-content": function () { run({ op: "admin.content", passenger: val("wp"), driver: val("wd"), support: val("ws") }); },
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

  // Порядок определения пользователя:
  // 1) данные из MAX, 2) uid в URL, 3) сохранённый uid в localStorage
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
    S = normalizeState(data);
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
    setTimeout(hideSplash, 420);
  }).catch(function (err) {
    // Не смогли получить состояние — пробуем войти по известному uid
    var fallbackId = (maxUser && maxUser.id) || savedUid();
    if (fallbackId) {
      api("/api/act", { op: "login", max_id: fallbackId, name: (maxUser && maxUser.name) || "" }).then(function (data) {
        if (data.state) {
          S = normalizeState(data.state);
          rememberUid(fallbackId);
          if (maxUser) window.Maximus.storeUser(maxUser);
          setSplashRole(S.user.role);
          screen = entryScreen();
          render();
          setTimeout(hideSplash, 420);
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
    showPeople();
    setTimeout(hideSplash, 300);
  });

  // ==================== KEEP-ALIVE ====================
  // Приложение не должно «засыпать»: держим сессию активной и
  // подтягиваем свежие данные, когда вкладка снова в фокусе.
  var _keepAliveTimer = null;
  var _lastPing = 0;

  function keepAliveTick() {
    if (!maxUser || !maxUser.id) return;
    var now = Date.now();
    // Пингуем не чаще раза в 20 секунд
    if (now - _lastPing < 20000) return;
    _lastPing = now;
    api("/api/act", { op: "session.ping" }).catch(function () {});
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

  // Возврат в приложение: сразу обновляем состояние
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopKeepAlive();
      return;
    }
    startKeepAlive();
    if (S && maxUser && maxUser.id) {
      refresh().catch(function () {});
    }
  });

  window.addEventListener("focus", function () {
    startKeepAlive();
    if (S && maxUser && maxUser.id) refresh().catch(function () {});
  });

  window.addEventListener("blur", stopKeepAlive);

  // Обновление раз в 3 секунды на активных экранах — как было
  setInterval(function () {
    if (!S || inflight) return;
    if (["live", "d-current", "d-orders"].indexOf(screen) < 0) return;
    var stateUrl = "/api/state";
    if (maxUser && maxUser.id) stateUrl += "?uid=" + maxUser.id;
    api(stateUrl).then(function (data) {
      if (inflight) return;
      S = normalizeState(data);
      render();
    }).catch(function () {});
  }, 3000);

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
