"use strict";

(() => {
  const UNITS = [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];

  function span(seconds, parts) {
    const pieces = [];
    let rest = Math.max(0, Math.floor(seconds));
    for (const [unit, size] of UNITS) {
      const count = Math.floor(rest / size);
      rest -= count * size;
      if (count || pieces.length) pieces.push(`${count}${unit}`);
      if (pieces.length === parts) break;
    }
    return pieces.join(" ") || "0s";
  }

  function tick() {
    const now = Date.now() / 1000;
    for (const element of document.querySelectorAll("[data-next-at]")) {
      const remaining = Number(element.dataset.nextAt) - now;
      element.textContent = remaining > 0 ? `in ${span(remaining, 3)}` : "due now";
    }
    for (const element of document.querySelectorAll("time.ago[data-at]")) {
      const elapsed = span(now - Number(element.dataset.at), 2);
      element.textContent = element.dataset.prefix ? `${element.dataset.prefix}${elapsed}` : `${elapsed} ago`;
    }
  }

  tick();
  setInterval(tick, 1000);
  document.body.addEventListener("htmx:afterSwap", tick);
})();
