/* Обёртка MAX Bridge. В обычном браузере методы просто ничего не делают. */
(function () {
  function app() {
    return window.WebApp || null;
  }

  function call(fn) {
    try {
      var wa = app();
      if (wa) fn(wa);
    } catch (e) {
      /* вне MAX моста нет */
    }
  }

  window.Maximus = {
    user: function () {
      var wa = app();
      var u = wa && wa.initDataUnsafe && wa.initDataUnsafe.user;
      if (u) {
        var name = [u.first_name, u.last_name].filter(Boolean).join(" ").trim();
        return {
          id: u.id,
          name: name || u.username || "",
          username: u.username || "",
          photo: u.photo_url || ""
        };
      }
      // Fallback: check URL parameters
      var params = new URLSearchParams(window.location.search);
      var uid = params.get('uid') || params.get('user_id') || params.get('max_id');
      if (uid) {
        return { id: Number(uid), name: params.get('name') || '', username: '', photo: '' };
      }
      // Fallback: check localStorage
      try {
        var stored = localStorage.getItem('maximus_user');
        if (stored) {
          var su = JSON.parse(stored);
          if (su && su.id) return su;
        }
      } catch (e) {}
      return null;
    },
    storeUser: function (user) {
      try { localStorage.setItem('maximus_user', JSON.stringify(user)); } catch (e) {}
    },
    startParam: function () {
      var wa = app();
      var sp = (wa && wa.initDataUnsafe && wa.initDataUnsafe.start_param) || "";
      if (!sp) {
        var params = new URLSearchParams(window.location.search);
        sp = params.get('startapp') || params.get('start') || "";
      }
      return sp;
    },
    ready: function () {
      call(function (wa) {
        if (typeof wa.ready === "function") wa.ready();
      });
    },
    haptic: function (kind) {
      call(function (wa) {
        if (!wa.HapticFeedback) return;
        if (kind === "ok") wa.HapticFeedback.notificationOccurred("success");
        else if (kind === "err") wa.HapticFeedback.notificationOccurred("error");
        else wa.HapticFeedback.impactOccurred("light");
      });
    },
    back: function (visible, handler) {
      call(function (wa) {
        if (!wa.BackButton) return;
        if (visible) {
          wa.BackButton.show();
          if (handler) wa.BackButton.onClick(handler);
        } else {
          wa.BackButton.hide();
        }
      });
    },
    confirmClose: function (on) {
      call(function (wa) {
        if (on && wa.enableClosingConfirmation) wa.enableClosingConfirmation();
        if (!on && wa.disableClosingConfirmation) wa.disableClosingConfirmation();
      });
    },
    phone: function () {
      var wa = app();
      if (!wa || !wa.requestContact) return Promise.resolve(null);
      return wa.requestContact().then(function (res) {
        return res && res.phone ? res.phone : null;
      }).catch(function () { return null; });
    },
    openLink: function (url) {
      call(function (wa) {
        if (wa.openLink) wa.openLink(url);
        else window.open(url, "_blank");
      });
      if (!app()) window.open(url, "_blank");
    },

    /**
     * ПРУТОК: закрывает мини-приложение и возвращает в чат с ботом.
     * Там уже лежит сообщение с нативной кнопкой гео — остаётся нажать.
     */
    close: function () {
      var wa = app();
      if (wa && typeof wa.close === "function") {
        try { wa.close(); return true; } catch (e) { /* fallback ниже */ }
      }
      return false;
    },

    /** Открывает чат с ботом по прямой ссылке MAX. */
    openBotChat: function (botLink) {
      var wa = app();
      if (wa && typeof wa.openMaxLink === "function" && botLink) {
        try { wa.openMaxLink(botLink); return true; } catch (e) { /* fallback */ }
      }
      if (wa && typeof wa.close === "function") {
        try { wa.close(); return true; } catch (e) { /* fallback */ }
      }
      if (botLink) window.open(botLink, "_blank");
      return false;
    },

    /** Есть ли нативный мост MAX. */
    hasBridge: function () {
      return !!app();
    }
  };
})();
