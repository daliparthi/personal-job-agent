// Behaviour shared by the fixture pages, modelled on Workday's application forms:
// - button[aria-haspopup="listbox"] opens a list of [role="option"] items (from data-options); picking one shows it
// - button[data-add] appends an entry built from <template id="tpl-...">, numbered like Workday ("Work Experience 2")
// - window.__clicks records the text of every button clicked, so tests can check what autofill clicked.
window.__clicks = [];

function closeLists() {
  document.querySelectorAll('[role="listbox"][data-for]').forEach((ul) => ul.remove());
}

function openList(btn) {
  closeLists();
  const ul = document.createElement("ul");
  ul.setAttribute("role", "listbox");
  ul.dataset.for = btn.id;
  for (const text of btn.dataset.options.split("|")) {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.textContent = text;
    ul.appendChild(li);
  }
  btn.after(ul);
}

function addEntry(kind) {
  const list = document.getElementById(`entries-${kind}`);
  const n = list.children.length + 1;
  const html = document.getElementById(`tpl-${kind}`).innerHTML.replaceAll("{n}", String(n));
  const wrap = document.createElement("div");
  wrap.innerHTML = html.trim();
  list.appendChild(wrap.firstElementChild);
  const add = document.querySelector(`[data-add="${kind}"]`);
  if (add) add.textContent = "Add Another";
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) window.__clicks.push(b.textContent.trim());
}, true);

document.addEventListener("click", (e) => {
  const btn = e.target.closest('button[aria-haspopup="listbox"]');
  if (btn) return openList(btn);
  const opt = e.target.closest('[role="listbox"][data-for] [role="option"]');
  if (opt) {
    const b = document.getElementById(opt.parentElement.dataset.for);
    b.textContent = opt.textContent;
    closeLists();
    return;
  }
  const add = e.target.closest("button[data-add]");
  if (add) return addEntry(add.dataset.add);
  if (!e.target.closest('[role="listbox"]')) closeLists();
});
