(() => {
  let nextId = 0;
  for (const field of document.querySelectorAll('textarea[name="body"]')) {
    const id = `mention-options-${nextId++}`;
    const list = document.createElement("ul");
    list.id = id;
    list.className = "mention-options";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Mention suggestions");
    list.hidden = true;
    field.insertAdjacentElement("afterend", list);
    field.setAttribute("role", "combobox");
    field.setAttribute("aria-haspopup", "listbox");
    field.setAttribute("aria-autocomplete", "list");
    field.setAttribute("aria-controls", id);
    field.setAttribute("aria-expanded", "false");
    let generation = 0;
    let choices = [];
    let selected = 0;
    let active = null;
    let pending = null;

    function close() {
      generation++;
      if (pending) pending.abort();
      choices = [];
      active = null;
      list.hidden = true;
      field.setAttribute("aria-expanded", "false");
      field.removeAttribute("aria-activedescendant");
    }

    function choose(index) {
      if (!active || !choices[index]) return;
      field.setRangeText(`${choices[index].value} `, active.start, active.end, "end");
      close();
      field.focus();
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function highlight() {
      for (let index = 0; index < list.children.length; index++) {
        list.children[index].setAttribute("aria-selected", String(index === selected));
      }
      const option = list.children[selected];
      if (option) {
        field.setAttribute("aria-activedescendant", option.id);
        option.scrollIntoView({ block: "nearest" });
      }
    }

    async function update() {
      close();
      if (field.selectionStart !== field.selectionEnd) return;
      const caret = field.selectionStart;
      const match = field.value.slice(0, caret).match(/(?:^|[^\w.@-])(@[A-Za-z0-9_.@-]*)$/);
      if (!match) return;
      const range = { start: caret - match[1].length, end: caret };
      const version = generation;
      pending = new AbortController();
      try {
        const response = await fetch(`/mention-options?q=${encodeURIComponent(match[1].slice(1))}`, {
          signal: pending.signal,
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const found = await response.json();
        if (version !== generation || document.activeElement !== field) return;
        choices = found;
        if (!choices.length) return;
        active = range;
        selected = 0;
        list.replaceChildren();
        choices.forEach((choice, index) => {
          const option = document.createElement("li");
          option.id = `${id}-${index}`;
          option.setAttribute("role", "option");
          option.textContent = choice.value;
          option.addEventListener("mousedown", (event) => {
            event.preventDefault();
            choose(index);
          });
          list.append(option);
        });
        list.hidden = false;
        field.setAttribute("aria-expanded", "true");
        highlight();
      } catch (error) {
        if (error.name !== "AbortError") close();
      }
    }

    field.addEventListener("input", update);
    field.addEventListener("click", update);
    field.addEventListener("blur", close);
    field.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (event.key === "Escape") {
        close();
      } else if (choices.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        selected = (selected + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
        highlight();
      } else if (choices.length && ["Enter", "Tab"].includes(event.key) && !event.shiftKey) {
        event.preventDefault();
        choose(selected);
      } else if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        close();
      }
    });
  }
})();
