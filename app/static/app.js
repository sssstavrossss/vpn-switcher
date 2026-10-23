// Progressive enhancement. Each location is a normal form, so the page
// still switches with JavaScript disabled (it reloads). This file keeps
// you on the page, shows "Switching…", and fills in the exit IP.

const banner = document.getElementById("banner");
const exitLine = document.getElementById("exit");
const place = document.getElementById("place");
const list = document.getElementById("locations");

let busy = false;
// Bumped when a switch starts, so a slow /status response cannot
// overwrite the "Switching…" message.
let epoch = 0;

loadStatus();

if (list) {
  list.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.classList.contains("loc-form")) {
      return;
    }
    event.preventDefault();
    if (busy || form.classList.contains("on")) {
      return;
    }
    switchTo(form);
  });
}

async function loadStatus() {
  const seen = epoch;
  try {
    const response = await fetch("/status", { headers: { Accept: "application/json" } });
    if (seen !== epoch) {
      return;
    }
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    const payload = await response.json();
    if (seen !== epoch) {
      return;
    }
    if (!payload.ok) {
      showExit(null, payload.detail || "Couldn't read the exit IP.");
      return;
    }
    if (payload.switching && banner.hidden) {
      setBanner("A switch is still running…", "info");
    } else if (payload.warning && banner.hidden) {
      setBanner(payload.warning, "warn");
    }
    if (payload.exit_ip) {
      showExit(payload.exit_ip, payload.exit_label);
    } else {
      showExit(null, payload.exit_detail || "Exit IP unavailable.");
    }
  } catch (error) {
    showExit(null, "Couldn't read the exit IP.");
  }
}

async function switchTo(form) {
  const label = form.dataset.label || form.dataset.city || "the new location";
  const country = form.dataset.country || "";
  const previous = document.querySelector(".loc-form.on");
  busy = true;
  epoch += 1;
  document.body.classList.add("is-switching");
  form.classList.add("is-target");
  setButtonsDisabled(true);
  mark(form);
  setBanner(`Switching to ${label}${country ? ", " + country : ""}… this usually takes under a minute.`, "info");

  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    const payload = await response.json();
    if (!payload.ok) {
      if (!payload.env_updated && previous) {
        mark(previous);
      }
      setBanner(payload.detail || "The switch failed.", "error");
      return;
    }
    if (payload.label && payload.country) {
      place.textContent = `${payload.label}, ${payload.country}`;
    }
    if (payload.exit_ip) {
      showExit(payload.exit_ip, payload.exit_label);
    } else if (payload.detail) {
      showExit(null, payload.detail);
    }
    if (payload.detail) {
      setBanner(payload.detail, payload.unchanged ? "info" : "warn");
    } else {
      setBanner(payload.exit_ip ? `Now exiting via ${payload.exit_ip}.` : "Location updated.", "info");
    }
  } catch (error) {
    if (previous) {
      mark(previous);
    }
    setBanner("The switch request failed before the server answered.", "error");
  } finally {
    busy = false;
    form.classList.remove("is-target");
    document.body.classList.remove("is-switching");
    setButtonsDisabled(false);
  }
}

function mark(form) {
  document.querySelectorAll(".loc-form").forEach((item) => {
    item.classList.remove("on");
    const button = item.querySelector("button");
    if (button) {
      button.setAttribute("aria-pressed", "false");
    }
  });
  form.classList.add("on");
  const button = form.querySelector("button");
  if (button) {
    button.setAttribute("aria-pressed", "true");
  }
}

function setButtonsDisabled(disabled) {
  document.querySelectorAll(".loc-form button").forEach((button) => {
    button.disabled = disabled;
  });
}

function setBanner(text, kind) {
  banner.hidden = false;
  banner.textContent = text;
  banner.className = `banner ${kind}`;
}

function showExit(ip, label) {
  if (ip) {
    exitLine.textContent = label ? `Exit ${ip} · ${label}` : `Exit ${ip}`;
    return;
  }
  exitLine.textContent = label || "Exit IP unavailable.";
}
