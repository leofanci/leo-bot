// Canvas blocks and the list grid: HTML for one block, plus the pure rules the
// canvas page needs (safe links, file routes, block moves, cell values).
// No DOM and no fetch: app.js wires events and passes `h`, its helpers:
//   h.esc     the app's escape helper (every user, bot or outside string)
//   h.md      the app's document markdown renderer (formatMdDoc, escapes first; "" for blank text)
//   h.icon    icon(name) from the app's sprite
//   h.owner   the viewer is the owner (Trust buttons)
//   h.canEdit the viewer may change this canvas
//   h.canOpen the viewer may open other dashboard routes (files, views)
//   h.ws, h.memoSection, h.viewRoutes [[route, label]], h.who(tag) -> name
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CanvasView = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const COLUMN_TYPES = ["text", "number", "date", "select", "checkbox", "url", "person"];
  const COLUMN_LABEL = { text: "Text", number: "Number", date: "Date", select: "Select",
    checkbox: "Checkbox", url: "Link", person: "Person" };
  const TYPE_LABEL = { text: "Text", checklist: "Checklist", list: "List", files: "Files",
    view: "View", browser: "Browser" };
  const OUTSIDE_TITLE = "Written by a coworker, or by a bot after it read outside content";
  const PERSON_RE = /^(owner|person:[A-Za-z0-9_-]{1,64}|bot:[a-z0-9-]{1,40})$/;

  // http(s) links and relative ones only; anything with a scheme, a backslash,
  // whitespace or a protocol-relative start is refused.
  function safeHref(u) {
    if (typeof u !== "string") return null;
    const s = u.trim();
    if (!s || /[\\\s\u0000-\u001f\u007f]/.test(s)) return null;
    if (/^https?:\/\/[^/]/i.test(s)) return s;
    if (s.startsWith("//")) return null;
    const colon = s.indexOf(":");
    const sep = s.search(/[/?#]/);
    if (colon !== -1 && (sep === -1 || colon < sep)) return null;
    return s;
  }

  // The dashboard route a workspace file opens at, "#/files" when it has no
  // view of its own, null outside this workspace (or in HQ, which has no Files).
  function fileRoute(ws, path, memoSection) {
    if (!ws || ws === "hq" || typeof path !== "string") return null;
    const pre = `projects/${ws}/`;
    if (!path.startsWith(pre) || path.split("/").includes("..")) return null;
    const rel = path.slice(pre.length);
    const parts = rel.split("/");
    const sec = s => `#/project/${ws}/${s}`;
    let m;
    if (rel === "strategy/intake.md") return sec("validation");
    if (rel === "technical.md") return sec("technical");
    if (parts.length === 2 && parts[0] === "canvases" && (m = parts[1].match(/^(cv\d{1,6})\.json$/))) return `#/canvas/${m[1]}`;
    if (parts[0] === "strategy" && parts[1] === "memos" && (m = parts[parts.length - 1].match(/^(.+)-v\d+\.json$/))) {
      return sec((memoSection || {})[m[1]] || "overview");
    }
    if (parts[0] === "strategy" && parts[1] === "experiments") return sec("experiments");
    if (parts[0] === "gtm") return sec("gtm");
    if (parts[0] === "products") return sec("product");
    if (parts[0] === "profiles" && parts.length >= 3 && /^[a-z0-9-]+$/.test(parts[1])) {
      if (parts[2] === "content") return "#/content";
      if (["brief-specs", "voices", "profile.md"].includes(parts[2])) return `#/profile/${parts[1]}/setup`;
      return `#/profile/${parts[1]}`;
    }
    return "#/files";
  }

  // move-block params for one step up or down, or null at the edge. The server
  // takes the block out, then puts it after `after` (no `after`: first).
  function moveParams(blocks, id, dir) {
    const i = (blocks || []).findIndex(b => b.id === id);
    if (i === -1) return null;
    if (dir === "up") {
      if (i === 0) return null;
      return i >= 2 ? { after: blocks[i - 2].id } : {};
    }
    if (i >= blocks.length - 1) return null;
    return { after: blocks[i + 1].id };
  }

  // What an input holds, as the column's value: {value} or {error}.
  function parseCell(col, raw) {
    const t = (col && col.type) || "text";
    if (t === "checkbox") return { value: raw === true || raw === "true" };
    const s = raw == null ? "" : String(raw);
    if (!s.trim()) return { value: null };
    if (t === "number") {
      const n = Number(s.trim());
      return Number.isFinite(n) ? { value: n } : { error: "Enter a number" };
    }
    if (t === "date") return /^\d{4}-\d{2}-\d{2}$/.test(s.trim()) ? { value: s.trim() } : { error: "Enter a date as YYYY-MM-DD" };
    if (t === "select") {
      const opts = col.options || [];
      return !opts.length || opts.includes(s) ? { value: s } : { error: `Pick one of: ${opts.join(", ")}` };
    }
    if (t === "person") return PERSON_RE.test(s.trim()) ? { value: s.trim() } : { error: "Enter owner, person:<id> or bot:<id>" };
    return { value: s };
  }

  // One cell's read view. Text is escaped; links only when the scheme is safe.
  function cellHtml(col, v, h) {
    if (v == null || v === "") return "";
    const t = (col && col.type) || "text";
    if (t === "checkbox") return v ? "Yes" : "No";
    if (t === "url") {
      const href = safeHref(String(v));
      const shown = h.esc(String(v));
      return href ? `<a href="${h.esc(href)}" target="_blank" rel="noopener noreferrer">${shown}</a>` : shown;
    }
    if (t === "person") return h.esc(h.who ? h.who(String(v)) : String(v));
    return h.esc(String(v));
  }

  function outsideTag(h, trustAttrs) {
    const tag = `<span class="tag cv-outside" title="${h.esc(OUTSIDE_TITLE)}">Outside content</span>`;
    const trust = h.owner && trustAttrs
      ? `<button type="button" class="btn ghost sm" data-cv-trust ${trustAttrs}>Trust</button>` : "";
    return tag + trust;
  }

  // data-* attributes for a Trust button; ids come from the server and are escaped anyway.
  const attrs = (h, pairs) => Object.entries(pairs)
    .map(([k, v]) => `data-${k}="${h.esc(v)}"`).join(" ");

  function blockMenu(b, h) {
    if (!h.canEdit) return "";
    const id = h.esc(b.id);
    return `<div class="cn-more cv-bmenu">
      <button type="button" class="term-btn" data-cv-bmenu aria-haspopup="menu" aria-expanded="false" aria-label="Block actions">${h.icon("more")}</button>
      <div class="menu" role="menu">
        <button type="button" role="menuitem" data-cv-move="up" data-bk="${id}">Move up</button>
        <button type="button" role="menuitem" data-cv-move="down" data-bk="${id}">Move down</button>
        <hr><button type="button" role="menuitem" class="cv-danger" data-cv-delblock="${id}">Delete block</button>
      </div></div>`;
  }

  function blockHead(b, state, h) {
    const kind = TYPE_LABEL[b.type] || "Block";
    let title = b.title || "";
    if (b.type === "list") {
      const data = (state.lists || {})[b.id];
      title = (data && data.title) || (b.summary && b.summary.title) || title;
    }
    const heading = title ? h.esc(title) : h.esc(kind);
    const kindTag = title ? `<small class="cv-kind">${h.esc(kind)}</small>` : "";
    const who = b.by ? `<small class="cv-by">${h.esc(h.who ? h.who(b.by) : b.by)}</small>` : "";
    const tag = b.outside ? outsideTag(h, attrs(h, { bk: b.id })) : "";
    return `<header class="cv-bhead"><h2 class="cv-btitle">${heading}</h2>${kindTag}${tag}${who}${blockMenu(b, h)}</header>`;
  }

  function textBlock(b, state, h) {
    const id = h.esc(b.id);
    const editing = (state.editing || {})[b.id];
    if (editing) {
      const draft = (state.drafts || {})[b.id] ?? b.md ?? "";
      const preview = (state.preview || {})[b.id];
      const conflict = (state.conflict || {})[b.id]
        ? `<div class="cv-conflict"${(state.announce || {})[b.id] ? ' role="alert"' : ""}><p><b>Someone else changed this block while you edited.</b>
            Their version is below and your draft is kept. Save replaces their version; Keep theirs drops your draft.</p>
            <div class="cv-md">${h.md(b.md) || "<p class=\"cv-note\">Their version is empty.</p>"}</div></div>` : "";
      const pane = preview
        ? `<div class="cv-md cv-preview" aria-live="polite">${h.md(draft) || "<p class=\"cv-note\">Nothing to preview yet.</p>"}</div>`
        : `<textarea class="cv-input cv-mdedit" data-cv-md="${id}" aria-label="Markdown" rows="10">${h.esc(draft)}</textarea>`;
      return `<div class="cv-tabs" role="group" aria-label="Edit or preview">
          <button type="button" class="chip${preview ? "" : " on"}" data-cv-mode="edit" data-bk="${id}" aria-pressed="${preview ? "false" : "true"}">Edit</button>
          <button type="button" class="chip${preview ? " on" : ""}" data-cv-mode="preview" data-bk="${id}" aria-pressed="${preview ? "true" : "false"}">Preview</button>
        </div>${conflict}${pane}
        <div class="cv-actions"><button type="button" class="btn primary sm" data-cv-savemd="${id}">${conflict ? "Replace their version" : "Save"}</button>
          <button type="button" class="btn ghost sm" data-cv-cancelmd="${id}">${conflict ? "Keep theirs" : "Cancel"}</button></div>`;
    }
    const body = h.md(b.md) || `<p class="cv-note">Empty.</p>`;
    const edit = h.canEdit ? `<div class="cv-actions"><button type="button" class="btn sm" data-cv-editmd="${id}">Edit</button></div>` : "";
    return `<div class="cv-md">${body}</div>${edit}`;
  }

  function checklistBlock(b, state, h) {
    const id = h.esc(b.id);
    const items = (b.items || []).map(it => {
      const iid = h.esc(it.id);
      const tag = it.outside ? outsideTag(h, attrs(h, { bk: b.id, item: it.id })) : "";
      const del = h.canEdit
        ? `<button type="button" class="term-btn cv-x" data-cv-delitem="${iid}" data-bk="${id}" aria-label="Delete item">${h.icon("x")}</button>` : "";
      return `<li class="cv-check${it.done ? " done" : ""}"><label>
          <input type="checkbox" data-cv-item="${iid}" data-bk="${id}"${it.done ? " checked" : ""}${h.canEdit ? "" : " disabled"}>
          <span>${h.esc(it.text)}</span></label>${tag}${del}</li>`;
    }).join("");
    const list = items ? `<ul class="cv-checks">${items}</ul>` : `<p class="cv-note">No items yet.</p>`;
    const add = h.canEdit ? `<form class="cv-inline" data-cv-additem="${id}">
        <input class="cv-input" name="text" aria-label="New item" placeholder="Add an item" maxlength="10000" autocomplete="off" value="${h.esc((state.itemDrafts || {})[b.id] || "")}">
        <button class="btn sm" type="submit">Add</button></form>` : "";
    return list + add;
  }

  // The editor of one cell. `edit.draft` is what the viewer typed (kept when a save
  // did not land); `edit.conflict` shows the value someone else saved meanwhile.
  function cellEditor(col, v, h, edit) {
    const t = col.type || "text";
    const label = h.esc(col.label);
    const cur = edit && edit.draft != null ? edit.draft : v;
    const now = cellHtml(col, v, h) || "(empty)";
    const note = edit && edit.conflict
      ? `<small class="cv-note"${edit.announce ? ' role="alert"' : ""}>Changed meanwhile to: ${now}. Enter saves yours, Escape keeps theirs.</small>` : "";
    if (t === "select") {
      const opts = [`<option value="">(empty)</option>`].concat((col.options || []).map(o =>
        `<option value="${h.esc(o)}"${o === cur ? " selected" : ""}>${h.esc(o)}</option>`)).join("");
      return `<select class="cv-input" data-cv-celledit aria-label="${label}">${opts}</select>${note}`;
    }
    const type = t === "number" ? "number" : t === "date" ? "date" : t === "url" ? "url" : "text";
    const extra = t === "person" ? ` placeholder="owner, person:id or bot:id"` : t === "number" ? ` step="any"` : "";
    const value = cur == null ? "" : h.esc(String(cur));
    return `<input class="cv-input" type="${type}" data-cv-celledit aria-label="${label}" value="${value}"${extra} maxlength="10000">${note}`;
  }

  function columnForm(bk, ls, form, cols, h) {
    const col = form.key ? cols.find(c => c.key === form.key) : null;
    // What the viewer typed survives a redraw (form.draft), else the column as stored.
    const draft = form.draft || {};
    const type = draft.type || (col && col.type) || "text";
    const label = h.esc(draft.label ?? (col ? col.label : ""));
    const options = h.esc(draft.options ?? (col && col.options ? col.options.join(", ") : ""));
    const types = COLUMN_TYPES.map(t => `<option value="${t}"${t === type ? " selected" : ""}>${COLUMN_LABEL[t]}</option>`).join("");
    const keyAttr = col ? ` data-key="${h.esc(col.key)}"` : "";
    const heading = col ? "Edit column" : "New column";
    return `<form class="cv-colform" data-cv-colform="${h.esc(bk)}" data-ls="${h.esc(ls)}"${keyAttr} aria-label="${heading}">
        <label>Name <input class="cv-input" name="label" value="${label}" required maxlength="200" autocomplete="off"></label>
        <label>Type <select class="cv-input" name="type">${types}</select></label>
        <label>Options <input class="cv-input" name="options" value="${options}" placeholder="For Select: new, sent, won" autocomplete="off"></label>
        <div class="cv-actions"><button class="btn primary sm" type="submit">${col ? "Save column" : "Add column"}</button>
          <button class="btn ghost sm" type="button" data-cv-colcancel>Cancel</button></div>
        <p class="field-help">A new type clears cells that no longer fit. Undo brings them back.</p></form>`;
  }

  function listBlock(b, state, h) {
    const data = (state.lists || {})[b.id];
    if ((b.summary && b.summary.deleted) || (state.missing || {})[b.id]) {
      return `<p class="cv-note">This list was deleted. Undo its delete from History, or remove this block.</p>`;
    }
    if (!data) return `<p class="cv-note">Loading list…</p>`;
    const ls = h.esc(data.id);
    const bk = h.esc(b.id);
    const hint = `cv-hint-${bk}`;
    const cols = data.columns || [];
    const edit = (state.editCell || {});
    const head = cols.map(c => {
      const inner = `${h.esc(c.label)} <small>${h.esc(COLUMN_LABEL[c.type] || c.type)}</small>`;
      return h.canEdit
        ? `<th scope="col"><button type="button" class="cv-colbtn" data-cv-col="${h.esc(c.key)}" aria-label="Edit column ${h.esc(c.label)}">${inner}</button></th>`
        : `<th scope="col">${inner}</th>`;
    }).join("");
    const rows = (data.rows || []).map(r => {
      const rw = h.esc(r.id);
      const cells = cols.map(c => {
        const v = (r.cells || {})[c.key];
        const key = h.esc(c.key);
        if (c.type === "checkbox" && h.canEdit) {
          return `<td><input type="checkbox" data-cv-cellbox="${key}" aria-label="${h.esc(c.label)}"${v ? " checked" : ""}></td>`;
        }
        if (edit.bk === b.id && edit.rw === r.id && edit.key === c.key) {
          return `<td class="cv-editing" data-cv-cell="${key}">${cellEditor(c, v, h, edit)}</td>`;
        }
        // A real cell: its name is its value; the edit hint is a description.
        const live = h.canEdit ? ` tabindex="0" aria-describedby="${hint}"` : "";
        return `<td data-cv-cell="${key}"${live}>${cellHtml(c, v, h)}</td>`;
      }).join("");
      const tag = r.outside ? outsideTag(h, attrs(h, { ls: data.id, row: r.id })) : "";
      const del = h.canEdit
        ? `<button type="button" class="term-btn cv-x" data-cv-delrow="${rw}" aria-label="Delete row">${h.icon("x")}</button>` : "";
      return `<tr data-rw="${rw}" class="${r.outside ? "cv-row-outside" : ""}">${cells}<td class="cv-rowact">${tag}${del}</td></tr>`;
    }).join("");
    const empty = rows ? "" : `<tr><td class="cv-note" colspan="${cols.length + 1}">No rows yet.</td></tr>`;
    const listTag = data.outside ? outsideTag(h, attrs(h, { ls: data.id })) : "";
    const viewNote = b.view && (b.view.sort || Object.keys(b.view.filter || {}).length)
      ? `<span class="cv-note">Saved filter and sort</span>` : "";
    const shown = (data.rows || []).length;
    const more = data.total > shown ? `<button type="button" class="btn ghost sm" data-cv-more="${bk}">Show more</button>` : "";
    const count = `<span class="cv-note">${shown} of ${Number(data.total) || 0} rows</span>`;
    const foot = (h.canEdit ? `<button type="button" class="btn sm" data-cv-addrow="${bk}">${h.icon("plus")} Add row</button>
        <button type="button" class="btn ghost sm" data-cv-addcol="${bk}">Add column</button>` : "") + count + more + viewNote;
    const form = state.colForm && state.colForm.bk === b.id ? columnForm(b.id, data.id, state.colForm, cols, h) : "";
    const hintEl = h.canEdit ? `<span class="cv-sr" id="${hint}">Press Enter to edit this cell</span>` : "";
    return `${listTag ? `<div class="cv-listtag">${listTag}</div>` : ""}<div class="cv-gridwrap"><table class="cv-grid" data-ls="${ls}">
        <thead><tr>${head}<th scope="col" class="cv-rowact"><span class="cv-sr">Row actions</span></th></tr></thead>
        <tbody>${rows}${empty}</tbody></table></div>${hintEl}
      <div class="cv-actions">${foot}</div>${form}`;
  }

  function filesBlock(b, state, h) {
    const id = h.esc(b.id);
    const items = (b.paths || []).map((p, i) => {
      const name = h.esc(p.split("/").pop() || p);
      const route = h.canOpen ? fileRoute(h.ws, p, h.memoSection) : null;
      const link = route ? `<a href="${h.esc(route)}">${name}</a>` : `<span>${name}</span>`;
      const del = h.canEdit
        ? `<button type="button" class="term-btn cv-x" data-cv-delpath="${i}" data-bk="${id}" aria-label="Remove file">${h.icon("x")}</button>` : "";
      return `<li>${link}<small>${h.esc(p)}</small>${del}</li>`;
    }).join("");
    const list = items ? `<ul class="cv-files">${items}</ul>` : `<p class="cv-note">No files yet.</p>`;
    const pre = h.ws && h.ws !== "hq" ? "projects/" + h.ws + "/" : "";
    const add = h.canEdit ? `<form class="cv-inline" data-cv-addpath="${id}">
        <input class="cv-input" name="path" aria-label="File path" placeholder="${h.esc(pre)}docs/notes.md" autocomplete="off" maxlength="1000" value="${h.esc((state.pathDrafts || {})[b.id] || "")}">
        <button class="btn sm" type="submit">Add file</button></form>` : "";
    return list + add;
  }

  function viewBlock(b, state, h) {
    const id = h.esc(b.id);
    const routes = h.viewRoutes || [];
    const known = routes.find(([r]) => r === b.route);
    const label = h.esc(known ? known[1] : b.route);
    const cur = b.route;
    const choices = (known ? [] : [[cur, cur]]).concat(routes);
    const opts = choices.map(([r, l]) => `<option value="${h.esc(r)}"${r === cur ? " selected" : ""}>${h.esc(l)}</option>`).join("");
    const pick = h.canEdit && routes.length
      ? `<select class="cv-input" data-cv-route="${id}" aria-label="Which view">${opts}</select>` : "";
    const open = h.canOpen && safeHref(b.route) ? `<a class="btn ghost sm" href="${h.esc(b.route)}">Open</a>` : "";
    // The last drawing of this route stays while a fresh one loads (no flash on redraw).
    const kept = (state.viewHtml || {})[b.id];
    const inner = kept && kept.route === cur ? kept.html : `<p class="cv-note">Loading view…</p>`;
    return `<div class="cv-viewbar"><span class="cv-note">Read-only: ${label}</span>${pick}${open}</div>
      <div class="cv-view" data-cv-view="${id}" inert>${inner}</div>`;
  }

  const browserImage = value => typeof value === "string" && /^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(value);

  function browserBlock(b, state, h) {
    if (!h.owner) return `<p class="cv-note">Browser view: owner only.</p>`;
    const bots = h.bots || [];
    const choices = bots.some(bot => bot.id === b.bot) ? bots : [{ id: b.bot, name: b.bot }, ...bots];
    const options = choices.map(bot => {
      const selected = bot.id === b.bot ? " selected" : "";
      return `<option value="${h.esc(bot.id)}"${selected}>${h.esc(bot.name)}</option>`;
    }).join("");
    const pick = h.canEdit && bots.length
      ? `<label class="cv-note">Bot <select class="cv-input" data-cv-browserbot="${h.esc(b.id)}">${options}</select></label>`
      : `<span class="cv-note">Browser of ${h.esc(b.bot)}</span>`;
    return `<div data-cv-browser="${h.esc(b.bot)}">
        <div class="cv-viewbar">${pick}<span class="cv-note" data-cv-browser-status role="status">Loading browser…</span>
          <button type="button" class="btn sm" data-cv-stopbot="${h.esc(b.bot)}" aria-label="Stop ${h.esc(b.bot)} browsing" disabled>Stop</button></div>
        <p class="cv-note" data-cv-browser-title hidden></p>
        <a class="cv-browser-url" data-cv-browser-url target="_blank" rel="noopener noreferrer" hidden></a>
        <div class="cv-browser-frame"><img data-cv-browser-image alt="Latest screenshot from ${h.esc(b.bot)}" hidden>
          <p class="cv-note" data-cv-browser-empty>No screenshot yet. It appears after this bot browses.</p></div>
        <p class="cv-note" data-cv-browser-time>No screenshot captured yet.</p>
        <p class="cv-note" data-cv-browser-error role="status" hidden></p></div>`;
  }

  const BLOCKS = { text: textBlock, checklist: checklistBlock, list: listBlock, files: filesBlock,
    view: viewBlock, browser: browserBlock };

  // Unknown types keep a placeholder until a renderer is registered.
  function otherBlock(b, state, h) {
    return `<p class="cv-note">This block type has no view here.</p>`;
  }

  // Hook for block types drawn elsewhere: fn(block, state, h) -> HTML.
  function registerBlock(type, fn) { BLOCKS[type] = fn; }

  function blockHtml(b, state, h) {
    const draw = BLOCKS[b.type] || otherBlock;
    return `<section class="cv-block cv-t-${h.esc(b.type)}" data-bk="${h.esc(b.id)}">
      ${blockHead(b, state || {}, h)}<div class="cv-body">${draw(b, state || {}, h)}</div></section>`;
  }

  const OP_LABEL = {
    "create-canvas": "Created the canvas", "update-canvas": "Changed the canvas",
    "add-block": "Added a block", "update-block": "Edited a block", "move-block": "Moved a block",
    "delete-block": "Deleted a block", "restore-block": "Restored a block", "delete-canvas": "Deleted the canvas",
    "pin-canvas": "Pinned to a channel", "unpin-canvas": "Unpinned from a channel",
    "create-list": "Created a list", "update-list": "Changed the columns", "add-row": "Added a row",
    "add-rows": "Added rows", "update-row": "Edited a row", "delete-row": "Deleted a row",
    "delete-list": "Deleted a list", "trust": "Trusted outside content", "undo": "Undid a change",
    "save-template": "Saved a template",
  };
  const historyLabel = e => OP_LABEL[e && e.op] || (e && e.op) || "Change";

  // True while the viewer is mid-edit: a background reload waits for the edit to end.
  // `typing`: a focused field in the canvas holds unsaved text.
  function busy(state, typing) {
    const s = state || {};
    return !!(typing || s.editCell || s.colForm || s.renaming || Object.values(s.editing || {}).some(Boolean));
  }

  // What the viewer may do to the canvas as a whole; the same rules as core/canvases.py
  // (_delete_canvas, _pin_check). A person: deletes any canvas the owner did not create;
  // pins into another room only a canvas they created, never in a DM room.
  function rights({ person, me, doc, pinned, roomKind, canEdit }) {
    if (!canEdit) return { mayDelete: false, mayPin: false };
    if (!person) return { mayDelete: true, mayPin: true };
    return { mayDelete: doc.created_by !== "owner",
      mayPin: roomKind !== "dm" && (!!pinned || doc.created_by === me) };
  }

  // A pinned-tabs list's identity: the tabs redraw only when it changes.
  const pinSig = list => JSON.stringify((list || []).map(c => [c.id, c.title]));

  return { busy, rights, pinSig, COLUMN_TYPES, COLUMN_LABEL, TYPE_LABEL, safeHref, fileRoute, moveParams, parseCell,
    cellHtml, blockHtml, browserImage, registerBlock, historyLabel };
});
