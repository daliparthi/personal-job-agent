// Resizable, collapsible side panes: drag the strip beside the positions list or the job description, double-click
// it (or click its arrow) to collapse and expand, or use the arrow keys when it has focus. Sizes are kept per browser.
const KEY = "jobagent.panes";
const MIN = { list: 200, jd: 240 };
const RESUME_MIN = 360; // the resume in the middle never gets narrower than this
const STEP = 24;

const SIDES = {
  list: { splitter: "#split-list", pane: ".list-pane", prop: "--list-w", sign: 1, label: "positions list", open: "‹", shut: "›" },
  jd: { splitter: "#split-jd", pane: ".jd-pane", prop: "--jd-w", sign: -1, label: "job description", open: "›", shut: "‹" },
};

function load() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
}
function save(state) {
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* private window: sizes just reset next time */ }
}

export function initPanes() {
  const panes = document.querySelector(".panes");
  if (!panes) return;
  const state = load(); // { list: {w, collapsed}, jd: {w, collapsed} }
  const wide = window.matchMedia("(min-width: 1101px)");

  const room = (side) => { // the widest this pane may be while the others keep their size
    const other = side === "list" ? "jd" : "list";
    const otherEl = panes.querySelector(SIDES[other].pane);
    const otherW = state[other]?.collapsed ? 0 : otherEl.getBoundingClientRect().width;
    return Math.max(MIN[side], panes.clientWidth - 2 * 14 - otherW - RESUME_MIN - 32);
  };

  function apply(side) {
    const s = SIDES[side];
    const st = (state[side] ||= {});
    const pane = panes.querySelector(s.pane);
    const splitter = panes.querySelector(s.splitter);
    const toggle = splitter.querySelector(".split-toggle");
    pane.classList.toggle("collapsed", !!st.collapsed && wide.matches);
    if (st.collapsed) panes.style.setProperty(s.prop, "0px");
    else if (st.w) panes.style.setProperty(s.prop, `${Math.round(Math.min(Math.max(st.w, MIN[side]), room(side)))}px`);
    else panes.style.removeProperty(s.prop);
    toggle.dataset.arrow = st.collapsed ? s.shut : s.open;
    const verb = st.collapsed ? "Expand" : "Collapse";
    toggle.setAttribute("aria-label", `${verb} the ${s.label}`);
    toggle.title = `${verb} the ${s.label}`;
    splitter.setAttribute("aria-expanded", String(!st.collapsed));
  }

  function setWidth(side, w) {
    const st = (state[side] ||= {});
    st.collapsed = false;
    st.w = Math.round(Math.min(Math.max(w, MIN[side]), room(side)));
    apply(side);
    save(state);
  }

  function toggle(side) {
    const st = (state[side] ||= {});
    if (!st.collapsed && !st.w) st.w = panes.querySelector(SIDES[side].pane).getBoundingClientRect().width;
    st.collapsed = !st.collapsed;
    apply(side);
    save(state);
  }

  for (const side of Object.keys(SIDES)) {
    const s = SIDES[side];
    const splitter = panes.querySelector(s.splitter);
    splitter.querySelector(".split-toggle").addEventListener("click", (e) => { e.stopPropagation(); toggle(side); });
    splitter.addEventListener("dblclick", () => toggle(side));

    splitter.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest(".split-toggle")) return;
      const st = (state[side] ||= {});
      const pane = panes.querySelector(s.pane);
      const startX = e.clientX;
      const startW = st.collapsed ? 0 : pane.getBoundingClientRect().width;
      splitter.setPointerCapture(e.pointerId);
      splitter.classList.add("dragging");
      document.body.classList.add("pane-dragging");
      const move = (ev) => {
        const w = startW + s.sign * (ev.clientX - startX);
        if (st.collapsed && w < MIN[side] / 2) return; // still closed: wait for a real drag
        setWidth(side, w);
      };
      const stop = () => {
        splitter.releasePointerCapture?.(e.pointerId);
        splitter.classList.remove("dragging");
        document.body.classList.remove("pane-dragging");
        splitter.removeEventListener("pointermove", move);
        splitter.removeEventListener("pointerup", stop);
        splitter.removeEventListener("pointercancel", stop);
      };
      splitter.addEventListener("pointermove", move);
      splitter.addEventListener("pointerup", stop);
      splitter.addEventListener("pointercancel", stop);
    });

    splitter.addEventListener("keydown", (e) => {
      const st = (state[side] ||= {});
      const cur = st.collapsed ? 0 : panes.querySelector(s.pane).getBoundingClientRect().width;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(side); }
      else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const dx = (e.key === "ArrowRight" ? 1 : -1) * STEP * s.sign;
        if (st.collapsed && dx <= 0) return;
        setWidth(side, (st.collapsed ? MIN[side] - STEP : cur) + dx);
      }
    });
  }

  const reapply = () => Object.keys(SIDES).forEach(apply);
  wide.addEventListener("change", reapply);
  window.addEventListener("resize", reapply);
  reapply();
}
