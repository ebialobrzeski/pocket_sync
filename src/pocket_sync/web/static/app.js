"use strict";

// Confirmation prompts: <form data-confirm="..."> or <button data-confirm="...">.
document.addEventListener("submit", (event) => {
  const button = event.submitter;
  const message = (button && button.dataset.confirm) || event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

function storage(key, value) {
  try {
    if (value === undefined) return window.localStorage.getItem(key);
    window.localStorage.setItem(key, value);
  } catch (e) {
    return null;
  }
  return null;
}

// --- recording page: player <-> transcript ------------------------------------------------
(function () {
  const audio = document.getElementById("player");
  const list = document.getElementById("transcript");
  const segments = list ? Array.from(list.querySelectorAll(".seg")) : [];
  const starts = segments.map((s) => parseFloat(s.dataset.start) || 0);

  if (audio) {
    const speed = document.getElementById("speed");
    if (speed) {
      const saved = storage("pocket.speed");
      if (saved && Array.from(speed.options).some((o) => o.value === saved)) speed.value = saved;
      const apply = () => { audio.playbackRate = parseFloat(speed.value) || 1; };
      speed.addEventListener("change", () => { apply(); storage("pocket.speed", speed.value); });
      audio.addEventListener("loadedmetadata", apply);
      apply();
    }
    document.querySelectorAll("[data-skip]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const t = audio.currentTime + parseFloat(btn.dataset.skip);
        audio.currentTime = Math.max(0, Math.min(t, audio.duration || t));
      });
    });
    // Space toggles playback when not typing.
    document.addEventListener("keydown", (e) => {
      if (e.code !== "Space" || e.target.closest("input, textarea, select, button, audio")) return;
      e.preventDefault();
      if (audio.paused) audio.play(); else audio.pause();
    });
  }

  if (!segments.length) return;

  if (audio) {
    list.addEventListener("click", (e) => {
      const ts = e.target.closest(".ts");
      if (!ts) return;
      const seg = ts.closest(".seg");
      audio.currentTime = parseFloat(seg.dataset.start) || 0;
      audio.play();
    });

    const follow = document.getElementById("follow");
    let active = -1;
    const activeIndex = (t) => {
      // last segment starting at or before t (binary search)
      let lo = 0, hi = starts.length - 1, found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (starts[mid] <= t + 0.05) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      return found;
    };
    audio.addEventListener("timeupdate", () => {
      const i = activeIndex(audio.currentTime);
      if (i === active) return;
      if (active >= 0) segments[active].classList.remove("active");
      active = i;
      if (i < 0) return;
      const seg = segments[i];
      seg.classList.add("active");
      if (!follow || !follow.checked || audio.paused) return;
      // Scroll only inside the transcript pane when it is scrollable (wide layout).
      if (list.scrollHeight > list.clientHeight + 1) {
        const top = seg.offsetTop - list.offsetTop;
        if (top < list.scrollTop || top + seg.offsetHeight > list.scrollTop + list.clientHeight) {
          list.scrollTo({ top: top - list.clientHeight / 3, behavior: "smooth" });
        }
      }
    });
  }

  // Find in transcript: hides non-matching segments and highlights matches.
  const filter = document.getElementById("transcript-filter");
  if (filter) {
    const texts = segments.map((s) => s.querySelector(".seg-text"));
    const originals = texts.map((t) => t.textContent);
    const fold = (s) => s.normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/ł/g, "l").replace(/Ł/g, "L").toLowerCase();
    const folded = originals.map(fold);
    let timer = null;
    filter.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const q = fold(filter.value.trim());
        segments.forEach((seg, i) => {
          const el = texts[i];
          el.textContent = originals[i];
          if (!q) { seg.classList.remove("hidden"); return; }
          const pos = folded[i].indexOf(q);
          seg.classList.toggle("hidden", pos < 0);
          // NFKD folding keeps one character per letter for Latin scripts, so positions line up.
          if (pos >= 0 && folded[i].length === originals[i].length) {
            const text = originals[i];
            const mark = document.createElement("mark");
            mark.textContent = text.slice(pos, pos + q.length);
            el.textContent = "";
            el.append(text.slice(0, pos), mark, text.slice(pos + q.length));
          }
        });
      }, 120);
    });
  }
})();

// --- sync page: live status -----------------------------------------------------------------
(function () {
  const box = document.querySelector("[data-refresh]");
  if (!box) return;
  const refresh = async () => {
    if (document.hidden) return;
    try {
      const res = await fetch(box.dataset.refresh, { headers: { Accept: "text/html" }, credentials: "same-origin" });
      if (res.ok && !res.redirected) box.innerHTML = await res.text();
    } catch (e) {
      /* offline: keep the last status */
    }
  };
  setInterval(refresh, 5000);
})();
