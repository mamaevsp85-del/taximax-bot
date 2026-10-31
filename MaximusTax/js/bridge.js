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
      if (!u) return null;
      var name = [u.first_name, u.last_name].filter(Boolean).join(" ").trim();
      return {
        id: u.id,
        name: name || u.username || "",
        username: u.username || "",
        photo: u.photo_url || ""
      };
    },
    startParam: function () {
      var wa = app();
      return (wa && wa.initDataUnsafe && wa.initDataUnsafe.start_param) || "";
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
    }
  };
})();
