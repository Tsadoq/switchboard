(() => {
  const posts = document.getElementById("posts");
  if (!posts) return;
  const thread = posts.dataset.thread;
  const status = document.getElementById("poll-status");
  let active = posts.dataset.live === "true";
  let cursor = posts.dataset.after;
  let busy = false;
  let timer;
  let leaving = false;

  function resolveAnchor() {
    const match = /^#post-([0-9]+)$/.exec(location.hash);
    if (match && !document.getElementById(`post-${match[1]}`)) {
      location.replace(`/p/${match[1]}`);
      return true;
    }
    return false;
  }

  function schedule(delay) {
    clearTimeout(timer);
    if (active && !leaving && !document.hidden && !busy) timer = setTimeout(poll, delay);
  }

  async function poll() {
    if (busy || !active || leaving) return;
    busy = true;
    let more = false;
    try {
      const response = await fetch(`/t/${thread}/posts?after=${cursor}`, {
        credentials: "same-origin",
        headers: { "HX-Request": "true" },
      });
      if (response.status === 401 || response.redirected) {
        active = false;
        location.assign("/login");
        return;
      }
      if (!response.ok) throw new Error("Poll failed");
      const html = await response.text();
      const template = document.createElement("template");
      template.innerHTML = html;
      const batch = [...template.content.querySelectorAll("article.post")];
      const next = response.headers.get("X-Next-After");
      let previous = BigInt(cursor);
      for (const article of batch) {
        if (!/^post-[0-9]+$/.test(article.id)) throw new Error("Invalid post id");
        const id = BigInt(article.id.slice(5));
        if (id <= previous) throw new Error("Posts out of order");
        previous = id;
      }
      if (!next || !/^[0-9]+$/.test(next) || BigInt(next) !== previous) {
        throw new Error("Invalid cursor");
      }
      for (const article of batch) {
        if (!document.getElementById(article.id)) posts.append(article);
      }
      cursor = next;
      posts.dataset.after = cursor;
      let visible = [...posts.querySelectorAll("article.post")];
      const trimmed = visible.length > 50;
      while (visible.length > 50) visible.shift().remove();
      const older = document.getElementById("older-posts");
      if (visible.length && older) {
        older.href = `/t/${thread}?before=${visible[0].id.slice(5)}`;
        if (trimmed) older.hidden = false;
      }
      const total = response.headers.get("X-Post-Total");
      if (total && /^[0-9]+$/.test(total)) document.getElementById("post-total").textContent = `${total} posts; showing ${visible.length}`;
      status.textContent = "";
      more = response.headers.get("X-Has-More") === "true";
      if (more && !batch.length) throw new Error("Empty intermediate batch");
      if (response.status === 286) {
        active = false;
        status.textContent = "This ticket is done, so the thread no longer refreshes.";
      }
    } catch {
      more = false;
      status.textContent = "Updates paused after an error. Retrying shortly; you can also reload Latest.";
    } finally {
      busy = false;
      schedule(more ? 0 : 10000);
    }
  }

  window.addEventListener("hashchange", resolveAnchor);
  window.addEventListener("pagehide", () => { leaving = true; clearTimeout(timer); });
  window.addEventListener("pageshow", () => {
    leaving = false;
    if (!resolveAnchor()) schedule(10000);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else schedule(0);
  });
})();
