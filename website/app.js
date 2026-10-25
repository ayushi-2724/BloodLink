(function () {
  "use strict";
  var CFG = window.BLOODLINK_CONFIG;
  var COGNITO_URL = "https://cognito-idp." + CFG.region + ".amazonaws.com/";
  var TOKEN_KEY = "bloodlink_id_token";
  var logoutTimer = null;
  var allRequests = [];

  function $(id) { return document.getElementById(id); }

  // ---------- messages ----------
  function showMsg(text, kind) {
    var el = $("msg");
    el.textContent = text;
    el.className = "msg " + (kind || "info");
    el.hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function clearMsg() { $("msg").hidden = true; }

  // ---------- Cognito (plain fetch) ----------
  function cognito(action, body) {
    return fetch(COGNITO_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService." + action
      },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var type = (data.__type || "").split("#").pop();
          var err = new Error(friendlyCognitoError(type, data.message));
          err.code = type;
          throw err;
        }
        return data;
      });
    });
  }

  function friendlyCognitoError(type, message) {
    switch (type) {
      case "UsernameExistsException": return "An account with this email already exists. Try logging in.";
      case "UserNotConfirmedException": return "Your email is not verified yet. Enter the code we emailed you in the Verify code tab.";
      case "NotAuthorizedException": return "Incorrect email or password.";
      case "UserNotFoundException": return "No account found with this email.";
      case "CodeMismatchException": return "That verification code is wrong. Please check the email and try again.";
      case "ExpiredCodeException": return "That code has expired. Click 'Resend code' to get a new one.";
      case "InvalidPasswordException": return "Password is too weak: use at least 8 characters with upper & lower case letters, a number and a symbol.";
      case "LimitExceededException":
      case "TooManyRequestsException": return "Too many attempts. Please wait a few minutes and try again.";
      case "InvalidParameterException": return message || "Please check the details you entered.";
      default: return message || "Something went wrong. Please try again.";
    }
  }

  // ---------- token helpers ----------
  function decodeJwt(token) {
    try {
      var part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      while (part.length % 4) part += "=";
      var json = decodeURIComponent(atob(part).split("").map(function (c) {
        return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
      }).join(""));
      return JSON.parse(json);
    } catch (e) { return null; }
  }
  function getSession() {
    var token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) return null;
    var claims = decodeJwt(token);
    if (!claims || !claims.exp || claims.exp * 1000 <= Date.now()) {
      sessionStorage.removeItem(TOKEN_KEY);
      return null;
    }
    return { token: token, email: claims.email, sub: claims.sub, exp: claims.exp };
  }

  // ---------- API ----------
  function api(method, path, body) {
    var s = getSession();
    if (!s) {
      logout("Your session has expired. Please log in again.");
      return Promise.reject(new Error("Session expired"));
    }
    var opts = { method: method, headers: { "Authorization": "Bearer " + s.token } };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(CFG.apiUrl + path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) {
          logout("Your session has expired. Please log in again.");
          throw new Error("Session expired");
        }
        if (!res.ok) throw new Error(data.message || ("Request failed (" + res.status + ")"));
        return data;
      });
    }, function () {
      throw new Error("Could not reach the server. Check your internet connection.");
    });
  }

  // ---------- views ----------
  function render() {
    var s = getSession();
    $("authView").hidden = !!s;
    $("appView").hidden = !s;
    $("userBox").hidden = !s;
    if (s) {
      $("userEmail").textContent = s.email || "";
      scheduleAutoLogout(s.exp);
      loadRequests();
      loadDonorStatus();
    }
  }
  function scheduleAutoLogout(exp) {
    if (logoutTimer) clearTimeout(logoutTimer);
    var ms = exp * 1000 - Date.now();
    logoutTimer = setTimeout(function () { logout("Your session has expired. Please log in again."); }, Math.max(ms, 0));
  }
  function logout(message) {
    sessionStorage.removeItem(TOKEN_KEY);
    if (logoutTimer) clearTimeout(logoutTimer);
    allRequests = [];
    $("requestList").innerHTML = "";
    render();
    if (message) showMsg(message, "info"); else clearMsg();
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".tab-panel").forEach(function (p) {
      p.hidden = p.getAttribute("data-panel") !== name;
    });
  }

  function withBusy(btn, promise) {
    btn.disabled = true;
    return promise.then(function (r) { btn.disabled = false; return r; },
      function (e) { btn.disabled = false; throw e; });
  }

  // ---------- auth handlers ----------
  function onSignup(e) {
    e.preventDefault();
    var email = $("signupEmail").value.trim().toLowerCase();
    var password = $("signupPassword").value;
    var btn = e.target.querySelector("button[type=submit]");
    withBusy(btn, cognito("SignUp", {
      ClientId: CFG.clientId, Username: email, Password: password,
      UserAttributes: [{ Name: "email", Value: email }]
    })).then(function () {
      $("verifyEmail").value = email;
      switchTab("verify");
      showMsg("Account created! We sent a 6-digit code to " + email + ". Enter it below to verify your email.", "ok");
    }).catch(function (err) { showMsg(err.message, "error"); });
  }

  function onVerify(e) {
    e.preventDefault();
    var email = $("verifyEmail").value.trim().toLowerCase();
    var btn = e.target.querySelector("button[type=submit]");
    withBusy(btn, cognito("ConfirmSignUp", {
      ClientId: CFG.clientId, Username: email, ConfirmationCode: $("verifyCode").value.trim()
    })).then(function () {
      $("loginEmail").value = email;
      switchTab("login");
      showMsg("Email verified! You can now log in.", "ok");
    }).catch(function (err) { showMsg(err.message, "error"); });
  }

  function onResend() {
    var email = $("verifyEmail").value.trim().toLowerCase();
    if (!email) { showMsg("Enter your email first, then click Resend code.", "error"); return; }
    withBusy($("resendBtn"), cognito("ResendConfirmationCode", { ClientId: CFG.clientId, Username: email }))
      .then(function () { showMsg("A new code was sent to " + email + ".", "ok"); })
      .catch(function (err) { showMsg(err.message, "error"); });
  }

  function onLogin(e) {
    e.preventDefault();
    var email = $("loginEmail").value.trim().toLowerCase();
    var btn = e.target.querySelector("button[type=submit]");
    withBusy(btn, cognito("InitiateAuth", {
      ClientId: CFG.clientId, AuthFlow: "USER_PASSWORD_AUTH",
      AuthParameters: { USERNAME: email, PASSWORD: $("loginPassword").value }
    })).then(function (data) {
      if (!data.AuthenticationResult || !data.AuthenticationResult.IdToken) {
        throw new Error("Login needs an extra step that this app does not support.");
      }
      sessionStorage.setItem(TOKEN_KEY, data.AuthenticationResult.IdToken);
      $("loginPassword").value = "";
      clearMsg();
      render();
      showMsg("Welcome! You are logged in.", "ok");
    }).catch(function (err) {
      if (err.code === "UserNotConfirmedException") {
        $("verifyEmail").value = email;
        switchTab("verify");
      }
      showMsg(err.message, "error");
    });
  }

  // ---------- requests ----------
  function onPost(e) {
    e.preventDefault();
    var phone = $("contactPhone").value.trim();
    if (!/^\d{10}$/.test(phone)) { showMsg("Contact phone must be exactly 10 digits.", "error"); return; }
    var body = {
      patientName: $("patientName").value.trim(),
      bloodGroup: $("bloodGroup").value,
      hospital: $("hospital").value.trim(),
      city: $("city").value.trim(),
      unitsNeeded: parseInt($("unitsNeeded").value, 10),
      contactPhone: phone,
      requiredBy: $("requiredBy").value,
      notes: $("notes").value.trim()
    };
    withBusy($("postBtn"), api("POST", "/requests", body)).then(function () {
      e.target.reset();
      $("unitsNeeded").value = 1;
      showMsg("Request posted! All registered donors have been alerted by email.", "ok");
      $("statusFilter").value = "Open";
      loadRequests();
    }).catch(function (err) { if (err.message !== "Session expired") showMsg(err.message, "error"); });
  }

  // ---------- donor status ----------
  function setDonorUI(status) {
    var label = $("donorStatus"), btn = $("donorBtn"), out = $("optoutBtn");
    label.className = "donor-status";
    btn.hidden = true; out.hidden = true;
    if (status === "donor") {
      label.textContent = "\u2714 You are a donor";
      label.classList.add("yes");
      out.textContent = "Opt out";
      out.hidden = false;
    } else if (status === "pending") {
      label.textContent = "Donor confirmation pending – check your email";
      label.classList.add("pending");
      out.textContent = "Cancel donor request";
      out.hidden = false;
    } else {
      label.textContent = "You are not a donor";
      btn.hidden = false;
    }
  }

  function loadDonorStatus() {
    $("donorStatus").textContent = "Checking donor status…";
    api("GET", "/donor").then(function (data) { setDonorUI(data.status); })
      .catch(function () { setDonorUI("none"); });
  }

  function onBecomeDonor() {
    withBusy($("donorBtn"), api("POST", "/subscribe")).then(function (data) {
      showMsg(data.message || "Check your inbox and confirm the subscription.", data.status === "pending" ? "info" : "ok");
      setDonorUI(data.status || "pending");
    }).catch(function (err) { if (err.message !== "Session expired") showMsg(err.message, "error"); });
  }

  function onOptout() {
    withBusy($("optoutBtn"), api("POST", "/donor/optout")).then(function (data) {
      showMsg(data.message, "ok");
      setDonorUI(data.status);
    }).catch(function (err) { if (err.message !== "Session expired") showMsg(err.message, "error"); });
  }

  function onDonate(id, btn) {
    withBusy(btn, api("POST", "/requests/" + encodeURIComponent(id) + "/respond")).then(function (data) {
      showMsg(data.message, "ok");
      loadRequests();
    }).catch(function (err) {
      if (err.message !== "Session expired") { showMsg(err.message, "error"); loadRequests(); }
    });
  }

  function onFulfill(id, btn) {
    withBusy(btn, api("PUT", "/requests/" + encodeURIComponent(id) + "/fulfill")).then(function () {
      showMsg("Marked as fulfilled. Donors have been informed — thank you!", "ok");
      loadRequests();
    }).catch(function (err) { if (err.message !== "Session expired") showMsg(err.message, "error"); });
  }

  function loadRequests() {
    $("requestList").innerHTML = '<p class="empty">Loading requests…</p>';
    api("GET", "/requests").then(function (data) {
      allRequests = data.items || [];
      renderRequests();
    }).catch(function (err) {
      if (err.message !== "Session expired") {
        $("requestList").innerHTML = "";
        showMsg(err.message, "error");
      }
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function renderRequests() {
    var me = getSession();
    var status = $("statusFilter").value;
    var group = $("groupFilter").value;
    var items = allRequests.filter(function (r) {
      return (status === "All" || r.status === status) && (group === "All" || r.bloodGroup === group);
    });
    var list = $("requestList");
    if (!items.length) {
      list.innerHTML = '<p class="empty">No requests match these filters.</p>';
      return;
    }
    list.innerHTML = items.map(function (r) {
      var open = r.status === "Open";
      var mine = me && r.createdBy === me.sub;
      return '<article class="req ' + (open ? "" : "fulfilled") + '">' +
        '<div class="req-top"><span class="group-badge">' + esc(r.bloodGroup) + '</span>' +
        '<span class="status ' + (open ? "open" : "fulfilled") + '">' + esc(r.status) + '</span></div>' +
        '<h3>' + esc(r.patientName) + '</h3>' +
        '<dl>' +
        '<dt>Hospital</dt><dd>' + esc(r.hospital) + '</dd>' +
        '<dt>City</dt><dd>' + esc(r.city) + '</dd>' +
        '<dt>Units</dt><dd>' + esc(r.unitsNeeded) + '</dd>' +
        '<dt>Phone</dt><dd><a href="tel:' + esc(r.contactPhone) + '">' + esc(r.contactPhone) + '</a></dd>' +
        '<dt>Needed by</dt><dd>' + esc(r.requiredBy) + '</dd>' +
        (r.notes ? '<dt>Notes</dt><dd>' + esc(r.notes) + '</dd>' : '') +
        '</dl>' +
        '<div class="meta">Posted ' + esc(fmtDate(r.createdAt)) + (mine ? " · by you" : "") +
        (r.fulfilledAt ? " · Fulfilled " + esc(fmtDate(r.fulfilledAt)) : "") + '</div>' +
        (mine ? renderResponders(r) : '') +
        (open && mine ? '<button class="btn btn-primary fulfill-btn" type="button" data-id="' + esc(r.requestId) + '">Mark as Fulfilled</button>' : '') +
        (open && !mine && !r.iOffered ? '<button class="btn btn-donate donate-btn" type="button" data-id="' + esc(r.requestId) + '">&#10084; I can donate</button>' : '') +
        (!mine && r.iOffered ? '<div class="offered">\u2714 You offered to donate</div>' : '') +
        '</article>';
    }).join("");
  }

  function renderResponders(r) {
    var list = r.responders || [];
    if (!list.length) return '<div class="responders">No donor has offered yet.</div>';
    return '<div class="responders"><strong>' + list.length + ' donor' + (list.length > 1 ? 's' : '') + ' offered to help:</strong><ul>' +
      list.map(function (d) {
        return '<li><a href="mailto:' + esc(d.email) + '">' + esc(d.email) + '</a> <span class="meta">(' + esc(fmtDate(d.at)) + ')</span></li>';
      }).join("") + '</ul></div>';
  }

  // ---------- wire up ----------
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".tab").forEach(function (t) {
      t.addEventListener("click", function () { clearMsg(); switchTab(t.getAttribute("data-tab")); });
    });
    $("signupForm").addEventListener("submit", onSignup);
    $("verifyForm").addEventListener("submit", onVerify);
    $("loginForm").addEventListener("submit", onLogin);
    $("resendBtn").addEventListener("click", onResend);
    $("logoutBtn").addEventListener("click", function () { logout("You have been logged out."); });
    $("requestForm").addEventListener("submit", onPost);
    $("donorBtn").addEventListener("click", onBecomeDonor);
    $("optoutBtn").addEventListener("click", onOptout);
    $("refreshBtn").addEventListener("click", loadRequests);
    $("statusFilter").addEventListener("change", renderRequests);
    $("groupFilter").addEventListener("change", renderRequests);
    $("requestList").addEventListener("click", function (e) {
      var btn = e.target.closest(".fulfill-btn");
      if (btn) onFulfill(btn.getAttribute("data-id"), btn);
      var dbtn = e.target.closest(".donate-btn");
      if (dbtn) onDonate(dbtn.getAttribute("data-id"), dbtn);
    });
    var today = new Date();
    $("requiredBy").min = today.toISOString().slice(0, 10);
    render();
  });
})();
