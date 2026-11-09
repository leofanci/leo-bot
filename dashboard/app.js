const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => (s==null?"":String(s)).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// One outline icon set: the sprite in app.html, symbol ids "i-<name>".
const icon = (name, cls = "") => `<svg class="i${cls ? " " + cls : ""}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const EMPTY = "n/a";
function normalizeDashes(s){ return String(s==null?"":s).replace(/\s*—\s*/g,", "); }

function formatInlineMd(s){
  let t=esc(normalizeDashes(s));
  t=t.replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>");
  t=t.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g,"<em>$1</em>");
  t=t.replace(/_([^_\n]+)_/g,"<em>$1</em>");
  return t;
}

function formatBlocks(raw){
  if(raw==null||raw==="") return "";
  const text=normalizeDashes(raw).replace(/\r\n/g,"\n");
  const blocks=text.split(/\n\s*\n/);
  let html="";
  for(const block of blocks){
    const lines=block.split("\n").map(l=>l.trim()).filter(Boolean);
    if(!lines.length) continue;
    const bullet=lines.every(l=>/^[-*•]\s+/.test(l));
    const numbered=lines.every(l=>/^\d+[.)]\s+/.test(l));
    if(bullet&&lines.length){
      html+=`<ul class="memo-list">${lines.map(l=>`<li>${formatInlineMd(l.replace(/^[-*•]\s+/,""))}</li>`).join("")}</ul>`;
    } else if(numbered&&lines.length){
      html+=`<ol class="memo-list">${lines.map(l=>`<li>${formatInlineMd(l.replace(/^\d+[.)]\s+/,""))}</li>`).join("")}</ol>`;
    } else {
      html+=`<p>${formatInlineMd(lines.join(" "))}</p>`;
    }
  }
  return html;
}

function formatMdDoc(raw, opts={}){
  if(raw==null||String(raw).trim()==="") return `<p class="memo-empty">Empty — fill via chat or your editor.</p>`;
  const lines=String(raw).replace(/\r\n/g,"\n").split("\n");
  let html="", para=[], list=null;
  const flushPara=()=>{ if(para.length){ html+=`<p>${formatInlineMd(para.join(" "))}</p>`; para=[]; } };
  const closeList=()=>{ if(list){ html+=`</${list.tag}>`; list=null; } };
  const pushListItem=(tag,line)=>{
    if(!list||list.tag!==tag){ flushPara(); closeList(); html+=`<${tag} class="memo-list">`; list={tag}; }
    const item=tag==="ul"?line.replace(/^[-*•]\s+/,""):line.replace(/^\d+[.)]\s+/,"");
    html+=`<li>${formatInlineMd(item)}</li>`;
  };
  for(const line of lines){
    const t=line.trim();
    if(!t){ flushPara(); closeList(); continue; }
    if(/^#{1,3}\s+/.test(t)){
      flushPara(); closeList();
      const lvl=(t.match(/^#+/)||["#"])[0].length;
      if (opts.dropH1 && lvl === 1) continue;
      const txt=t.replace(/^#+\s+/,"");
      html+=lvl<=1?`<h3 class="md-h">${esc(txt)}</h3>`:`<h4 class="md-h">${esc(txt)}</h4>`;
    } else if(/^[-*•]\s+/.test(t)) pushListItem("ul",t);
    else if(/^\d+[.)]\s+/.test(t)) pushListItem("ol",t);
    else { closeList(); para.push(t); }
  }
  flushPara(); closeList();
  return html||`<p class="memo-empty">Empty — fill via chat or your editor.</p>`;
}

function memoField(label,value){
  const body=formatBlocks(value);
  return body?`<div class="memo-field">${label?`<b>${esc(label)}</b>`:""}${body}</div>`:"";
}

const MEMO_BODY_META=new Set(["status","date","version"]);
/** Fallback until /api/schemas loads — keep in sync with core/project_schemas.py */
const MEMO_FIELD_LABELS_FALLBACK={
  problem_statement:"Problem", who_has_it:"Who", current_workaround:"Workaround",
  cheapest_next_test:"Next test", willingness_to_pay_signal:"WTP signal",
  pace_recommendation:"Pace", riskiest_assumption:"Riskiest assumption",
  recommendation:"Call", summary:null, evidence:"Evidence",
  segment:"Segment", sam:"SAM", som:"SOM", sizing_confidence:"Confidence",
};
const MEMO_FIELD_ORDER_FALLBACK={
  "problem-validation":["problem_statement","who_has_it","_status","current_workaround","willingness_to_pay_signal","cheapest_next_test","evidence","recommendation"],
  assessment:["pace_recommendation","riskiest_assumption","recommendation"],
  "market-sizing":["segment","sam","som","sizing_confidence","recommendation"],
  _default:["summary","recommendation"],
};

function memoFieldOrder(type){
  const o = _SCHEMAS?.memo_render_order?.[type];
  if (o && o.length) return o;
  return MEMO_FIELD_ORDER_FALLBACK[type] || MEMO_FIELD_ORDER_FALLBACK._default;
}
function memoFieldLabel(key){
  const fromApi = _SCHEMAS?.memo_field_labels?.[key];
  if (fromApi != null) return fromApi;
  if (key in MEMO_FIELD_LABELS_FALLBACK) return MEMO_FIELD_LABELS_FALLBACK[key];
  return humanizeKey(key);
}

function renderMemoEvidence(list){
  if(!Array.isArray(list)||!list.length) return "";
  const items=list.map(e=>{
    if(e&&typeof e==="object"){
      const sig=e.signal||e.text||e.note||"";
      const tag=e.strength||e.kind||"signal";
      const src=e.source?` <span class="memo-src">(${esc(e.source)})</span>`:"";
      return `<li><b>${esc(tag)}</b> ${formatInlineMd(sig)}${src}</li>`;
    }
    return `<li>${formatInlineMd(e)}</li>`;
  }).join("");
  return `<div class="memo-field"><b>Evidence</b><ul class="memo-list">${items}</ul></div>`;
}

const SAM_LABELS={population:"Population",reach_filter:"Reach filter",arpu_assumption:"ARPU",
  annual_revenue_low:"Revenue (low)",annual_revenue_mid:"Revenue (mid)",annual_revenue_high:"Revenue (high)"};
const SOM_LABELS={capture_rate:"Capture rate",customers_year1:"Customers (yr1)",mrr_year1:"MRR (yr1)",
  arr_year1:"ARR (yr1)",what_changes_the_number:"What changes it"};
function renderMemoKV(obj,labels){
  if(!obj||typeof obj!=="object") return "";
  const rows=Object.entries(obj).filter(([,v])=>v!=null&&v!=="")
    .map(([k,v])=>`<li><b>${esc(labels[k]||k)}</b> ${formatInlineMd(String(v))}</li>`).join("");
  return rows?`<ul class="memo-list">${rows}</ul>`:"";
}

function renderMemoValue(key,val){
  if(val==null||val==="") return "";
  // the decision call-out gets a highlighted block (mockup "recommend" card)
  if((key==="recommendation"||key==="pace_recommendation")&&typeof val!=="object"){
    const label=memoFieldLabel(key)||"Recommend";
    return `<div class="memo-rec"><span class="rec-lbl">${esc(label)}</span><div class="rec-body">${formatBlocks(val)}</div></div>`;
  }
  if(key==="evidence"&&Array.isArray(val)) return renderMemoEvidence(val);
  if(key==="sam"&&val&&typeof val==="object"&&!Array.isArray(val)){
    const kv=renderMemoKV(val,SAM_LABELS);
    return kv?`<div class="memo-field"><b>SAM</b>${kv}</div>`:"";
  }
  if(key==="som"&&val&&typeof val==="object"&&!Array.isArray(val)){
    const kv=renderMemoKV(val,SOM_LABELS);
    return kv?`<div class="memo-field"><b>SOM</b>${kv}</div>`:"";
  }
  if(Array.isArray(val)){
    if(!val.length) return "";
    const items=val.map(v=>`<li>${typeof v==="object"?esc(JSON.stringify(v)):formatInlineMd(v)}</li>`).join("");
    const label=memoFieldLabel(key);
    return `<div class="memo-field"><b>${esc(label)}</b><ul class="memo-list">${items}</ul></div>`;
  }
  const label=memoFieldLabel(key);
  if(label===null) return `<div class="memo-field">${formatBlocks(val)}</div>`;
  return memoField(label, val);
}

function renderMemoBody(b,type){
  const order=memoFieldOrder(type);
  const seen=new Set();
  let html="";
  for(const key of order){
    if(key==="_status"&&type==="problem-validation"){
      html+=`<div class="memo-field"><b>Status</b><p>${esc(b.validation_status||"?")} · severity ${esc(b.severity||"?")} · ${esc(b.frequency||EMPTY)}</p></div>`;
      seen.add("validation_status"); seen.add("severity"); seen.add("frequency");
      continue;
    }
    if(seen.has(key)||!(key in b)) continue;
    seen.add(key);
    html+=renderMemoValue(key,b[key]);
  }
  for(const [key,val] of Object.entries(b||{})){
    if(MEMO_BODY_META.has(key)||seen.has(key)) continue;
    if(val==null||val===""||(Array.isArray(val)&&!val.length)) continue;
    html+=renderMemoValue(key,val);
  }
  return html;
}

// Chat text: paragraphs and lists, with @mentions as accent-colored names.
function formatChatText(raw){
  return formatBlocks(raw).replace(/(^|[\s>(])@([a-z][a-z0-9-]*)/g, '$1<b class="mention">@$2</b>');
}
const slugify = s => s.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/(^-|-$)/g,"");
function toast(m,sticky){const t=$("#toast");t.textContent=m;t.style.opacity=1;clearTimeout(t._timer);if(!sticky)t._timer=setTimeout(()=>t.style.opacity=0,2400);}
async function api(p,o){const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j;}
function jpost(p,body){return api(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}
function postUrl(id, profileSlug, suffix=""){
  const base = `/api/post/${id}${suffix}`;
  return profileSlug ? `${base}?profile=${encodeURIComponent(profileSlug)}` : base;
}

// ── router ───────────────────────────────────────────────────────────────────
let _NAV_EXTRAS = {};
// Bumped on every navigation. An async view takes a guard before it fetches and
// checks it before drawing, so a slow load never paints over the next page.
let ROUTE_SEQ = 0;
const routeGuard = () => { const seq = ROUTE_SEQ; return () => seq === ROUTE_SEQ; };
function navigate(hash, extras){
  _NAV_EXTRAS = extras || {};
  if(location.hash===hash) parseRoute(hash);
  else location.hash = hash;
}
const ROUTES = [
  [/^\/content(?:\/(posts|calendar|briefs|voice))?$/, ([t]) => { setState("content",{tab:t||"posts"}); renderContent(t||"posts"); }],
  [/^\/calendar$/,                              ()       => { setState("calendar"); renderTimeline(); }],
  [/^\/day\/(\d{4}-\d{2}-\d{2})$/,              ([d])    => { setState("day",{day:d}); renderDay(d); }],
  [/^\/operations$/,                            ()       => { setState("operations"); renderOperations(); }],
  [/^\/team$/,                                  ()       => { setState("team"); renderTeam(); }],
  [/^\/approvals$/,                             ()       => { setState("approvals"); renderApprovals(); }],
  [/^\/settings(?:\/(engines|bots|integrations|access))?$/, ([t]) => { setState("settings",{tab:t||"engines"}); renderSettings(t||"engines"); }],
  [/^\/settings\/folders$/,                     ()       => location.replace("#/folders")],
  [/^\/(engines|integrations)$/,                ([t])    => location.replace(`#/settings/${t}`)],
  [/^\/folders$/,                               ()       => { setState("folders"); renderFolders(); }],
  [/^\/files$/,                                 ()       => { setState("files"); renderFiles(); }],
  [/^\/room\/new$/,                              ()       => renderRoomForm(null)],
  [/^\/room\/([a-z0-9-]+)\/edit$/,                ([n])    => renderRoomForm(n)],
  [/^\/needs$/,                                 ()       => { setState("needs"); renderNeeds(); }],
  [/^\/workspace\/([^/]+)\/logo$/,               ([w])    => { let id = w; try { id = decodeURIComponent(w); } catch {} renderWsLogo(id); }],
  [/^\/project\/new$/,                          ()       => renderNewProject()],
  [/^\/project\/([^/]+)\/edit$/,                ([s])    => renderEditProject(s)],
  [/^\/project\/([^/]+)\/delete$/,              ([s])    => renderConfirmDeleteProject(s)],
  [/^\/project\/([^/]+)\/profile\/new$/,        ([s])    => renderNewProfile(s)],
  [/^\/project\/([^/]+)\/intake\/new$/,          ([s])    => renderNewIntake(s)],
  [/^\/project\/([^/]+)\/technical\/new$/,       ([s])    => renderNewTechnical(s)],
  [/^\/project\/([^/]+)\/technical\/subsection\/new$/, ([s]) => renderNewDocSubsection(s, "technical")],
  [/^\/project\/([^/]+)\/technical\/subsection\/edit\/([^/]+)$/, ([s,k]) => renderEditDocSubsection(s, "technical", k, _NAV_EXTRAS)],
  [/^\/project\/([^/]+)\/intake\/subsection\/edit\/([^/]+)$/, ([s,k]) => renderEditDocSubsection(s, "intake", k, _NAV_EXTRAS)],
  [/^\/project\/([^/]+)\/memo\/new\/([^/]+)$/,   ([s,t])  => renderNewMemo(s,t)],
  [/^\/project\/([^/]+)\/memo\/([^/]+)\/(\d+)\/edit$/,   ([s,t,v]) => renderEditMemo(s,t,v)],
  [/^\/project\/([^/]+)\/memo\/([^/]+)\/(\d+)\/delete$/, ([s,t,v]) => renderConfirmDeleteMemo(s,t,v)],
  [/^\/project\/([^/]+)\/experiment\/new$/,     ([s])    => renderNewExperiment(s)],
  [/^\/project\/([^/]+)\/experiment\/([^/]+)\/edit$/,   ([s,stem]) => renderEditExperiment(s,stem)],
  [/^\/project\/([^/]+)\/experiment\/([^/]+)\/delete$/, ([s,stem]) => renderConfirmDeleteExperiment(s,stem)],
  [/^\/project\/([^/]+)\/gtm\/phase\/new$/,            ([s])      => renderNewPhase(s)],
  [/^\/project\/([^/]+)\/gtm\/phase\/([^/]+)\/edit$/,  ([s,pid])  => renderEditPhase(s,pid)],
  [/^\/project\/([^/]+)\/gtm\/strategy\/new$/,         ([s])      => renderNewStrategy(s)],
  [/^\/project\/([^/]+)\/gtm\/strategy\/([^/]+)\/edit$/, ([s,stem]) => renderEditStrategy(s,stem)],
  [/^\/project\/([^/]+)\/product\/new$/,          ([s])    => renderNewProduct(s)],
  [/^\/product\/([^/]+)\/feature\/new$/,         ([ps])   => renderNewFeature(ps, _NAV_EXTRAS.projectSlug)],
  [/^\/product\/([^/]+)\/feature\/([^/]+)\/edit$/,   ([ps,fid]) => renderEditFeature(ps,fid,_NAV_EXTRAS.projectSlug)],
  [/^\/product\/([^/]+)\/feature\/([^/]+)\/delete$/, ([ps,fid]) => renderConfirmDeleteFeature(ps,fid,_NAV_EXTRAS.projectSlug)],
  [/^\/project\/([^/]+)\/([^/]+)$/,             ([s,k])  => { setState("section",{project:s,section:k}); OPEN.projects.add(s); saveOpen(); renderProjectSection(s,k); }],
  [/^\/profile\/([^/]+)\/setup$/,               ([s])    => { setState("profileSetup",{profile:s}); _expandProfile(s); renderProfileSetup(s); }],
  [/^\/profile\/([^/]+)\/delete$/,              ([s])    => { setProfileContext(s); renderConfirmDeleteProfile(s); }],
  [/^\/profile\/([^/]+)\/add$/,                 ([s])    => { setProfileContext(s); renderAddIdea(s); }],
  [/^\/profile\/([^/]+)\/log$/,                 ([s])    => { setProfileContext(s); renderLogPosted(s); }],
  [/^\/profile\/([^/]+)\/generate$/,            ([s])    => { setProfileContext(s); renderGenerateIdeas(s); }],
  [/^\/profile\/([^/]+)\/channel\/new$/,        ([s])    => { setProfileContext(s); renderNewChannel(s); }],
  [/^\/profile\/([^/]+)$/,                      ([s])    => { setState("profile",{profile:s}); _expandProfile(s); renderProfile(s, _NAV_EXTRAS.chanFilter||null); }],
  [/^\/channel\/([^/]+)\/setup$/,               ([s])    => { setProfileContext(_NAV_EXTRAS.profileSlug||""); renderChannelSetup(s, _NAV_EXTRAS.profileSlug||""); }],
  [/^\/channel\/([^/]+)\/delete$/,              ([s])    => { setProfileContext(_NAV_EXTRAS.profileSlug||""); renderConfirmDeleteChannel(s, _NAV_EXTRAS.profileSlug||""); }],
  [/^\/post\/([^/]+)\/revise$/,                 ([id])   => { setProfileContext(_NAV_EXTRAS.profileSlug||""); renderRevise(id); }],
  [/^\/post\/([^/]+)\/edit$/,                   ([id])   => { setProfileContext(_NAV_EXTRAS.profileSlug||""); renderEditPost(id, _NAV_EXTRAS.profileSlug||""); }],
  [/^\/post\/([^/]+)\/delete$/,                 ([id])   => { setProfileContext(_NAV_EXTRAS.profileSlug||""); renderConfirmDelete(id, _NAV_EXTRAS.profileSlug||""); }],
  [/^\/post\/([^/]+)$/,                         ([id])   => { setProfileContext(_NAV_EXTRAS.profileSlug||""); renderPostDetail(id, _NAV_EXTRAS.profileSlug||""); }],
  [/^\/posts\/delete$/,                         ()       => renderConfirmBulkDelete(_NAV_EXTRAS.ids||[], _NAV_EXTRAS.profileSlug||"")],
  [/^\/activity\/new$/,                         ()       => renderNewActivity(_NAV_EXTRAS)],
  [/^\/milestone\/new$/,                        ()       => renderNewMilestone(_NAV_EXTRAS)],
  [/^\/milestone\/([^/]+)\/edit$/,              ([id])   => renderEditMilestone(id, _NAV_EXTRAS)],
];
// What an invited person may open: Team and channel forms (admins).
const PERSON_ROUTE = /^\/(team|room\/new|room\/[a-z0-9-]+\/edit)$/;
function parseRoute(hash){
  let path = (hash||"#/calendar").replace(/^#/,"").replace(/\?.*$/,"");
  if (isPerson() && !PERSON_ROUTE.test(path)) path = "/team";
  ROUTE_SEQ += 1;
  $("#main")?.classList.toggle("form-view", /\/(?:new|edit|setup|add|log|generate|revise)$/.test(path));
  $("#app")?.classList.toggle("settings-open", /^\/settings\b/.test(path));
  CURRENT_POST = null;
  CURRENT_PROFILE_SLUG = null;
  setState("route");
  const ex = _NAV_EXTRAS; _NAV_EXTRAS = {};
  setFocus(ex.focus);   // only a deliverable link focuses; any other navigation shows all
  for(const [re, fn] of ROUTES){
    const m = path.match(re);
    if(m){
      _NAV_EXTRAS = ex;
      const out = fn(m.length>1?m.slice(1):[]);
      if (out && typeof out.then === "function") void out;
      _NAV_EXTRAS={}; highlight(); return;
    }
  }
  navigate(defaultRoute());
}
// Where the canvas opens when nothing else is asked for.
const defaultRoute = () => (isPerson() ? "#/team" : wsProject() ? "#/content" : "#/calendar");
function setState(view, extra={}){
  STATE={view, project:extra.project||null, section:extra.section||null,
         profile:extra.profile||null, channelGuidelines:extra.channelGuidelines||null,
         day:extra.day||null, tab:extra.tab||null};
}
// True only while the URL shows this profile's post list. STATE.view stays
// "profile" on post/form sub-pages, so redraws must check the route itself.
function onProfileList(slug){
  const path = (location.hash||"").replace(/^#/,"").replace(/\?.*$/,"");
  return path === `/profile/${slug}`;
}
function setProfileContext(slug, view="profile"){
  if(!slug) return;
  setState(view,{profile:slug});
  _expandProfile(slug);
}
function _expandProfile(slug){
  const pp=_TREE.find(p=>p.profiles.some(pr=>pr.slug===slug));
  if(pp && !OPEN.projects.has(pp.slug)){
    OPEN.projects.add(pp.slug); saveOpen();
    void renderRail();
  }
}
window.addEventListener("hashchange", ()=>parseRoute(location.hash));

// ── form helpers ─────────────────────────────────────────────────────────────
const flabel = (t, osId=null) => {
  const chip = osId ? ` <span class="label-id">${sectionIdChip(osId)}</span>` : "";
  return `<label class="flabel">${esc(t)}${chip}</label>`;
};
const finput = (name,val='',extra='',osId=null) => `<input name="${name}"${osId?` data-os-id="${esc(osId)}"`:""} value="${esc(val)}" ${extra}>`;
const fsel = (name,opts,val,osId=null) => `<select name="${name}"${osId?` data-os-id="${esc(osId)}"`:""}>`+opts.map(([v,l])=>`<option value="${esc(v)}"${v===val?" selected":""}>${esc(l)}</option>`).join("")+`</select>`;
const fta = (name,val='',rows=5,extra='',osId=null) => `<textarea name="${name}" rows="${rows}"${osId?` data-os-id="${esc(osId)}"`:""} ${extra}>${esc(val)}</textarea>`;
// channel picker: checkboxes from the profile's channels (no manual slug typing)
const fchannels = (channels, selected=[]) => (channels && channels.length)
  ? `<div class="chan-checks">${channels.map(c=>`<label class="chan-check"><input type="checkbox" class="chanbox" value="${esc(c.slug)}"${(selected||[]).includes(c.slug)?" checked":""}> <span style="opacity:.55">${platIcon(c.platform)}</span> ${esc(PLAT_NAME[c.platform]||c.platform||c.name||c.slug)}</label>`).join("")}</div>`
  : `<div class="help">No channels yet — add one in Setup first.</div>`;
const checkedChannels = root => [...root.querySelectorAll(".chanbox:checked")].map(b=>b.value).join(",");
const mentionBare = c => {
  if (c.osId && c.osId.includes(".")) return c.osId.split(".").pop();
  if (c.osId && c.osId.includes(":")) return c.osId.split(":").pop();
  return c.slug;
};
function formVals(root){ const d={}; root.querySelectorAll("[name]").forEach(i=>d[i.name]=i.value); return d; }
function pageHeader(title, crumb, btns='', pageId=null){
  const idBar = pageId ? `<div class="sec-meta" style="margin-top:5px">${sectionIdChip(pageId, {dropTabSuffix:true})}</div>` : "";
  return `<div class="topbar"><div><div class="crumbs"><a class="bk text-link">← ${esc(crumb)}</a></div><h1 class="title">${esc(title)}</h1>${idBar}</div><div style="margin-left:auto;display:flex;gap:8px">${btns}</div></div>`;
}
document.addEventListener("click", e=>{ if(e.target.classList.contains("bk")) history.back(); });

// ── undo toast (for small reversible deletes) ────────────────────────────────
function undoToast(msg, undoFn){
  const t=$("#toast");
  t.innerHTML=`${esc(msg)} <button>Undo</button>`;
  t.style.opacity=1; clearTimeout(t._timer);
  t.querySelector("button").onclick=async()=>{ t.style.opacity=0; try{await undoFn();}catch(e){toast("✗ "+e.message);} };
  t._timer=setTimeout(()=>t.style.opacity=0, 8000);
}

const SECTIONS = PROJECT_SECTIONS;
let STATE = {view:"calendar", project:null, section:null, profile:null, channelGuidelines:null};
let _TREE = [];
let _POSTS = [];   // flat post index for @-mentions, refreshed in renderRail
let _SKILLS = [];  // [{name, description}] for the manual skill picker (/ and +)
let _BOTS = [];    // team roster from /api/bots (@handle teammates that own skills)
const botById = id => _BOTS.find(b => b.id === id);
// Identity hues shared by bots, profiles and project tiles (--ui-id-<hue>-*).
const ID_HUES = ["ocean", "clay", "plum", "ochre", "slate", "rose"];
const hueFor = key => ID_HUES[[...String(key)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 0) % ID_HUES.length];
// Built-in teammates get a fixed icon and hue; any other bot falls back to its
// initial on a hue derived from its id.
const BOT_LOOK = {
  cmo: ["chart", "clay"], cos: ["compass", "slate"], cto: ["terminal", "plum"], gtm: ["route", "ocean"],
  product: ["layers", "ochre"], research: ["search", "rose"], social: ["compose", "ocean"],
};
function botAvatar(b, cls = ""){
  const [ic, hue] = BOT_LOOK[b?.id] || [null, hueFor(b?.id || "")];
  return `<span class="av bot t-${hue}${cls ? " " + cls : ""}" aria-hidden="true">${ic ? icon(ic) : esc((b?.name || "?")[0].toUpperCase())}</span>`;
}
// Post open in detail view. Full slot+brief inlined only when the turn needs it
// (content keywords, @mention) — otherwise a compact pointer saves tokens.
// Set in renderPostDetail, cleared on every navigation in parseRoute.
let CURRENT_POST = null;
// Profile slug for the current main view — lean pointer by default; voice/spec
// only when the turn is content-related or user @mentions the profile.
let CURRENT_PROFILE_SLUG = null;

// Rail fold state — which projects/profiles are expanded. Persisted so folds
// survive the full renderRail() that fires after every chat action / refresh.
const OPEN = loadOpen();
function loadOpen(){
  try{ const o = JSON.parse(localStorage.getItem("Leo Bot.rail.open") || "{}");
    return { projects:new Set(o.projects||[]), profiles:new Set(o.profiles||[]) }; }
  catch{ return { projects:new Set(), profiles:new Set() }; }
}
function saveOpen(){
  localStorage.setItem("Leo Bot.rail.open",
    JSON.stringify({ projects:[...OPEN.projects], profiles:[...OPEN.profiles] }));
}
// A node is open if it's in the OPEN set (persisted). Navigate actions below
// auto-add the parent to OPEN so navigating to a child keeps it visible,
// but the user can still manually collapse the parent afterwards.
// Who is signed in: the owner, or a person invited into some workspaces
// (core.people). A person sees only their workspaces' Team and channels.
let _ME = { kind: "owner", id: "ceo", name: "Owner", ws: {} };
const isPerson = () => _ME.kind === "person";
const myRole = (ws = _WS) => isPerson() ? (_ME.ws || {})[ws] || null : "owner";
async function loadMe(){
  try { _ME = await api("/api/me"); } catch {}
  document.body.classList.toggle("as-person", isPerson());
  const joined = new URLSearchParams(location.search).get("joined");
  if (joined) {
    _WS = joined;
    try { localStorage.setItem("wsId", joined); } catch {}
    history.replaceState(null, "", location.pathname + location.hash);
  }
}
async function boot(){ await loadMe(); await ensureSchemas(); await renderRail(); parseRoute(location.hash||defaultRoute()); }

async function ensureIdRegistry(force=false){
  if (!force && Object.keys(IdReg.lookup || {}).length) return;
  try { IdReg.load(await api("/api/id-registry")); }
  catch { IdReg.load({}); }
}

async function refreshIdRegistry(){
  try { IdReg.load(await api("/api/id-registry")); }
  catch { IdReg.load({}); }
}

let _SCHEMAS = null;
async function ensureSchemas(force=false){
  if (!force && _SCHEMAS) return _SCHEMAS;
  try { _SCHEMAS = await api("/api/schemas"); }
  catch { _SCHEMAS = { memos: {}, experiment: [], feature: [] }; }
  setSchemas(_SCHEMAS);
  return _SCHEMAS;
}

function renderSchemaField(spec, values={}){
  const key = spec.key;
  const label = spec.label || humanizeKey(key);
  const req = spec.required ? " required" : "";
  const ph = spec.placeholder ? ` placeholder="${esc(spec.placeholder)}"` : "";
  let val = values[key];
  if (spec.type === "textarea" || spec.type === "evidence") {
    const hint = spec.type === "evidence" ? ' placeholder="One signal per line"' : ph;
    // evidence is stored as [{signal,source,strength}, ...]; the form only
    // edits the signal text (one per line), same as what saving re-creates
    // via _coerce_evidence — so flatten to text for display, not [object Object].
    if (spec.type === "evidence" && Array.isArray(val)) {
      val = val.map(e => (e && typeof e === "object") ? (e.signal || "") : String(e)).join("\n");
    }
    return `${flabel(label)}${fta(key, val ?? "", spec.rows || 3, hint + req)}`;
  }
  if (spec.type === "select") {
    const opts = (spec.options || []).map(o => [o, o === "" ? "—" : (o.charAt(0).toUpperCase() + o.slice(1))]);
    return `${flabel(label)}${fsel(key, opts, val ?? (spec.default || opts[0]?.[0] || ""))}`;
  }
  return `${flabel(label)}${finput(key, val ?? "", ph + req)}`;
}

function schemaFields(kind, memoType=null){
  const s = _SCHEMAS || {};
  if (kind === "memo") return (s.memos && s.memos[memoType]) || [];
  if (kind === "experiment") return s.experiment || [];
  if (kind === "feature") return s.feature || [];
  return [];
}

// ── workspaces (Slack-style): HQ = the holding's CEO office, every project =
// one company. Each has its own channels, DMs, threads and switched-on bots;
// bots in one never see another. The calendar is the one shared thing.
let _WORKSPACES = [];  // [{id, name, kind}] from /api/workspaces
let _WS = "hq";
try { _WS = localStorage.getItem("wsId") || "hq"; } catch {}
let _ROOMS = [];   // current workspace's rooms: [{id, kind, name, members, lead}]
let _APPROVALS = [];  // pending approvals visible here (HQ: every workspace)
const fmtTokens = n => !n ? "" : n >= 1e6 ? `${(n/1e6).toFixed(1)}M tok` : n >= 1000 ? `${(n/1000).toFixed(1)}k tok` : `${n} tok`;
const fmtCost = c => c ? `$${c < 0.01 ? c.toFixed(4) : c.toFixed(2)}` : "";
const fmtUsage = (tokens, cost) => [fmtTokens(tokens), fmtCost(cost)].filter(Boolean).join(" · ");
const roomById = id => _ROOMS.find(r => r.id === id);
const wsById = id => _WORKSPACES.find(w => w.id === id);
const wsProject = () => (_WS !== "hq" ? _TREE.find(p => p.slug === _WS) || null : null);

function wsBadge(w){
  if (w.id === "hq") return "HQ";
  const words = (w.name || w.id).split(/[\s_-]+/).filter(Boolean);
  return ((words[0] || "")[0] + ((words[1] || "")[0] || (words[0] || "")[1] || "")).toUpperCase();
}

// Uploaded workspace logo (versioned so a new upload shows at once); HQ falls
// back to the Leo Bot mark, a project to its initials.
const wsLogoUrl = w => w?.logo ? `/api/workspace-logo/${encodeURIComponent(w.id)}?v=${encodeURIComponent(w.logo)}`
  : (w?.id === "hq" ? "/logo.svg" : null);

function renderWsBar(){
  const bar = $("#ws-bar"); if (!bar) return;
  bar.innerHTML = _WORKSPACES.map(w => {
    const url = wsLogoUrl(w);
    return `<button class="ws-btn${w.id === _WS ? " active" : ""}${w.logo ? " has-logo" : url ? " ws-mark" : ` t-${hueFor(w.id)}`}" data-ws="${esc(w.id)}" title="${esc(w.name)}${w.id === "hq" ? " · holding" : ` · ${esc(w.kind)}`}" aria-label="${esc(w.name)}">${url ? `<img src="${esc(url)}" alt="">` : esc(wsBadge(w))}</button>`;
  }).join("")
    + (isPerson() ? "" : `<button class="ws-btn ws-add" id="new-project-btn" data-os-id="btn:global:new-project" title="New project (= new workspace)" aria-label="New project">${icon("plus")}</button>`)
    + (isPerson() ? "" : `<button class="ws-btn ws-settings" id="ws-settings" title="Settings" aria-label="Settings">${icon("settings")}</button>`);
  bar.querySelectorAll("[data-ws]").forEach(b => b.onclick = () => switchWorkspace(b.dataset.ws));
  if ($("#new-project-btn")) $("#new-project-btn").onclick = e => { e.stopPropagation(); openNewProject(); };
  if ($("#ws-settings")) $("#ws-settings").onclick = () => STATE.view === "settings" ? closeSettings() : openSettings();
}

// Settings covers the whole machine, so it takes over the window (.settings-open)
// and closing it returns to exactly where you were.
let _BEFORE_SETTINGS = null;
function openSettings(){
  _BEFORE_SETTINGS = { hash: location.hash, hidden: !!$("#app")?.classList.contains("canvas-hidden") };
  closeNav();
  window.ChatRooms?.showCanvas();
  navigate("#/settings");
}
function closeSettings(){
  const back = _BEFORE_SETTINGS;
  _BEFORE_SETTINGS = null;
  navigate(back?.hash || defaultRoute());
  if (!back || back.hidden) window.ChatRooms?.hideCanvas();
}

async function switchWorkspace(ws){
  if (window.ChatRooms?.busy()) return toast("Reply in progress · Esc to stop");
  const changed = ws !== _WS;
  _WS = ws;
  try { localStorage.setItem("wsId", ws); } catch {}
  await renderRail();
  await window.ChatRooms?.enterWorkspace();
  if (changed) {   // new company: back to the conversation, canvas closed but ready on its content
    window.ChatRooms?.hideCanvas();
    navigate(isPerson() ? "#/team" : ws === "hq" ? "#/calendar" : "#/content");
  } else if (STATE.view === "settings") closeSettings();
}
window.Workspaces = { switchTo: switchWorkspace, current: () => _WS };

// Sidebar sections the user folded away (Channels, Teammates, Your time).
const RAIL_COLLAPSED = new Set((() => { try { return JSON.parse(localStorage.getItem("railCollapsed")) || []; } catch { return []; } })());
// Narrow windows: the workspace bar and sidebar live in a drawer.
const closeNav = () => document.getElementById("app")?.classList.remove("nav-open");
// One menu open at a time; the button that opened it tracks aria-expanded.
const closeMenus = () => document.querySelectorAll(".menu.open").forEach(m => {
  m.classList.remove("open"); m._btn?.setAttribute("aria-expanded", "false");
});
function toggleMenu(btn, menu){
  const opening = !menu.classList.contains("open");
  closeMenus();
  if (!opening) return;
  menu.classList.add("open"); menu._btn = btn; btn.setAttribute("aria-expanded", "true");
}
document.addEventListener("click", closeMenus);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenus(); });

async function renderRail(){
  const skillsP = _SKILLS.length ? Promise.resolve(_SKILLS) : api("/api/skills-index").catch(() => []);
  const person = isPerson();   // an invited person: no ids, tree, posts or approvals (owner-only)
  const regP = person ? Promise.resolve(IdReg.load({})) : api("/api/id-registry").then(d => { IdReg.load(d); }).catch(() => IdReg.load({}));
  const schP = ensureSchemas();
  const wsP = api("/api/workspaces").then(d => d.workspaces || []).catch(() => [{ id: "hq", name: "HQ", kind: "hq" }]);
  [_TREE, _POSTS, _SKILLS, , , _WORKSPACES] = await Promise.all([
    person ? [] : api("/api/tree"), person ? [] : api("/api/posts-index").catch(() => []),
    skillsP, regP, schP, wsP]);
  if (!wsById(_WS)) { _WS = _WORKSPACES[0]?.id || "hq"; try { localStorage.setItem("wsId", _WS); } catch {} }
  const q = `?ws=${encodeURIComponent(_WS)}`;
  [_BOTS, _ROOMS, _APPROVALS] = await Promise.all([
    api("/api/bots" + q).then(d => d.bots || []).catch(() => []),
    api("/api/rooms" + q).then(d => d.rooms || []).catch(() => []),
    person ? [] : api("/api/approvals" + q).then(d => d.approvals || []).catch(() => []),
  ]);
  renderWsBar();
  const w = wsById(_WS) || { id: "hq", name: "HQ", kind: "hq" };
  const roomRow = r => {
    const b = r.kind === "dm" ? botById(r.lead) : null;
    return `<a class="room-row" data-room="${esc(r.id)}"${r.private ? ' title="Private channel"' : ""}>${b ? botAvatar(b) : icon(r.private ? "lock" : "hash")}<span class="rail-name">${esc(r.name)}</span></a>`;
  };
  const section = (key, title, rows, add = "") => `<section class="rail-sec${RAIL_COLLAPSED.has(key) ? " collapsed" : ""}">
      <div class="rail-hdr"><button class="rail-tg" data-rail-tg="${key}" aria-expanded="${!RAIL_COLLAPSED.has(key)}">${icon("chevron")}<span>${title}</span></button>${add}</div>
      <nav class="nav${key === "time" ? "" : " rooms"}">${rows}</nav>
    </section>`;
  const channels = _ROOMS.filter(r => r.kind !== "dm");
  const manage = myRole() === "owner" || myRole() === "admin";
  const dms = _ROOMS.filter(r => r.kind === "dm");
  $("#rail").innerHTML = `
    <div class="rail-head">
      <div class="ws-name" id="brand-btn">${wsLogoUrl(w) ? `<img class="mark" src="${esc(wsLogoUrl(w))}" alt="">` : `<span class="mark t-${hueFor(w.id)}" aria-hidden="true">${esc(wsBadge(w))}</span>`}<b>${esc(w.name)}</b></div>
      <button class="rail-ib" id="rail-compose" title="New thread" aria-label="New thread">${icon("compose")}</button>
    </div>
    <div class="rail-scroll">
      <nav class="nav">
        ${person ? "" : `<a data-view="approvals">${icon("inbox")}<span class="rail-name">Approvals</span>${_APPROVALS.length ? `<span class="badge">${_APPROVALS.length}</span>` : ""}</a>
        ${w.id !== "hq" ? `<a data-view="files">${icon("folder")}<span class="rail-name">Files</span></a>` : ""}
        <a data-view="folders">${icon("link")}<span class="rail-name">Linked folders</span></a>
        <a data-view="content">${icon("layers")}<span class="rail-name">Content</span></a>`}
        <a data-view="team">${icon("users")}<span class="rail-name">Team</span><span class="count">${_BOTS.filter(b => b.active).length}</span></a>
      </nav>
      ${section("channels", "Channels", channels.map(roomRow).join(""),
        manage ? `<button class="add-btn" id="new-room-btn" title="New channel" aria-label="New channel">${icon("plus")}</button>` : "")}
      ${section("teammates", "Teammates", dms.map(roomRow).join(""))}
      ${person ? "" : section("time", "Your time", `
        <a data-view="needs">${icon("sun")}<span class="rail-name">Today</span></a>
        <a data-view="calendar">${icon("calendar")}<span class="rail-name">Calendar</span></a>
        <a data-view="operations">${icon("list-check")}<span class="rail-name">Operations</span></a>`)}
    </div>`;
  $("#rail").querySelectorAll("[data-view]").forEach(a => a.onclick = () => {
    closeNav();
    window.ChatRooms?.showCanvas();
    selectGlobal(a.dataset.view);
  });
  $("#rail").querySelectorAll("[data-room]").forEach(a => a.onclick = () => { closeNav(); window.ChatRooms?.select(a.dataset.room); });
  $("#rail").querySelectorAll("[data-rail-tg]").forEach(b => b.onclick = () => {
    const key = b.dataset.railTg;
    RAIL_COLLAPSED.has(key) ? RAIL_COLLAPSED.delete(key) : RAIL_COLLAPSED.add(key);
    try { localStorage.setItem("railCollapsed", JSON.stringify([...RAIL_COLLAPSED])); } catch {}
    b.closest(".rail-sec").classList.toggle("collapsed", RAIL_COLLAPSED.has(key));
    b.setAttribute("aria-expanded", String(!RAIL_COLLAPSED.has(key)));
  });
  if ($("#new-room-btn")) $("#new-room-btn").onclick = e => { e.stopPropagation(); closeNav(); window.ChatRooms?.showCanvas(); navigate("#/room/new"); };
  $("#rail-compose").onclick = () => { closeNav(); window.ChatRooms?.newThread(); };
  window.ChatRooms?.refresh();
  highlight();
}

// Project the canvas is showing: the open view's project, else (unless
// viewOnly) the active room's.
function canvasProject(viewOnly=false){
  if (STATE.project) return _TREE.find(p => p.slug === STATE.project) || null;
  const prof = STATE.profile || CURRENT_PROFILE_SLUG;
  if (prof) return _TREE.find(p => p.profiles.some(x => x.slug === prof)) || null;
  if (STATE.channelGuidelines) return _TREE.find(p => p.profiles.some(pr => pr.channels.some(c => c.slug === STATE.channelGuidelines))) || null;
  const path = (location.hash || "").replace(/^#/, "");
  const m = path.match(/^\/project\/([^/]+)/);
  if (m && _TREE.some(p => p.slug === m[1])) return _TREE.find(p => p.slug === m[1]);
  return viewOnly ? null : wsProject();
}

// Canvas bar: a breadcrumb of what is open, Files, and a More menu holding the
// project's sections and profiles.
// kind: [icon, Files filter label, one item's label]
const KIND_META = {
  memo: ["doc", "Memos", "Memo"], experiment: ["flask", "Experiments", "Experiment"], doc: ["book", "Docs", "Doc"],
  roadmap: ["map", "Roadmap", "Roadmap"], plan: ["route", "Go-to-market", "Go-to-market plan"],
  content: ["calendar", "Content", "Content plan"], brief: ["compose", "Post briefs", "Post brief"], voice: ["mic", "Voice", "Voice"],
};
const VIEW_LABEL = { content: "Content", files: "Files", team: "Team", approvals: "Approvals", folders: "Linked folders", calendar: "Calendar", needs: "Today", operations: "Operations" };
const profileName = slug => _TREE.flatMap(p => p.profiles).find(x => x.slug === slug)?.name || slug;
// "Instagram, TikTok": the channels of a deliverable's profile.
const delivChannels = d => (_TREE.flatMap(p => p.profiles).find(x => x.slug === d.profile)?.channels || [])
  .map(c => PLAT_NAME[c.platform] || c.platform || c.slug).join(", ");

// [[label, route or null]] from the workspace down to the open item.
function canvasCrumbs(){
  const p = wsProject();
  const path = (location.hash || "").replace(/^#/, "").replace(/\?.*$/, "");
  if (/^\/settings\b/.test(path)) return [];
  const crumbs = [[p ? p.name : (wsById(_WS)?.name || "HQ"), defaultRoute()]];
  const prof = STATE.profile || CURRENT_PROFILE_SLUG;
  let m;
  if (/^\/content\b/.test(path)) crumbs.push(["Content", "#/content"]);
  else if ((m = path.match(/^\/profile\/([^/]+)/))) crumbs.push([profileName(m[1]), `#/profile/${m[1]}`]);
  else if ((m = path.match(/^\/post\/([^/]+)/))) {
    if (prof) crumbs.push([profileName(prof), `#/profile/${prof}`]);
    crumbs.push([(OSID.post(m[1]) || m[1]).split(".").pop(), null]);
  }
  else if ((m = path.match(/^\/project\/[^/]+\/([^/]+)$/))) crumbs.push([SECTIONS.find(x => x.key === m[1])?.label || m[1], null]);
  else if (VIEW_LABEL[path.slice(1)]) crumbs.push([VIEW_LABEL[path.slice(1)], null]);
  return crumbs;
}

function renderCanvasNav(){
  const nav = $("#canvas-nav"); if (!nav) return;
  const p = STATE.view === "settings" ? null : wsProject();
  const crumbs = canvasCrumbs();
  nav.hidden = false;
  nav.innerHTML = `<div class="cn-row">
      <span class="cn-crumb">${crumbs.map(([label, go], i) => i === crumbs.length - 1 || !go
        ? `<b>${esc(label)}</b>` : `<a data-go="${esc(go)}">${esc(label)}</a>`).join(icon("chevron-right"))}</span>
      ${p ? `<button class="btn ghost sm cn-files${STATE.view === "files" ? " on" : ""}" data-cn-files>${icon("folder")} Files</button>
      <div class="cn-more"><button class="term-btn" data-cn-more aria-haspopup="menu" aria-expanded="false" title="More" aria-label="More">${icon("more")}</button>
        <div class="menu" role="menu">
          <div class="menu-h">Sections</div>
          ${SECTIONS.map(x => `<button data-cn-sec="${esc(x.key)}">${esc(x.label)}</button>`).join("")}
          ${(p.profiles || []).length ? `<hr><div class="menu-h">Profiles</div>
          ${p.profiles.map(x => `<button data-cn-prof="${esc(x.slug)}">${esc(x.name)}</button>`).join("")}` : ""}
          <hr><button data-cn-edit>Project settings</button>
        </div></div>` : ""}
      ${FOCUS_ID && STATE.view !== "content" ? `<button class="btn ghost sm" data-cn-all title="Show the rest of this section">Show all</button>` : ""}
      <button class="term-btn cn-close" data-cn-close title="Close canvas" aria-label="Close canvas">${icon("x")}</button>
    </div>`;
  nav.querySelectorAll("[data-go]").forEach(a => a.onclick = () => navigate(a.dataset.go));
  nav.querySelector("[data-cn-files]")?.addEventListener("click", () => navigate("#/files"));
  nav.querySelector("[data-cn-close]").onclick = () => STATE.view === "settings" ? closeSettings() : window.ChatRooms?.hideCanvas();
  nav.querySelector("[data-cn-more]")?.addEventListener("click", e => {
    e.stopPropagation();
    toggleMenu(e.currentTarget, e.currentTarget.nextElementSibling);
  });
  nav.querySelectorAll("[data-cn-sec]").forEach(b => b.onclick = () => selectSection(p.slug, b.dataset.cnSec));
  nav.querySelectorAll("[data-cn-prof]").forEach(b => b.onclick = () => selectProfile(b.dataset.cnProf, null));
  nav.querySelector("[data-cn-edit]")?.addEventListener("click", () => openEditProject(p.slug));
  nav.querySelector("[data-cn-all]")?.addEventListener("click", () => { setFocus(null); renderCanvasNav(); });
}

// Open one deliverable in the canvas, scrolled to its card. On project
// sections, focus also hides the other groups (a generated rule keyed on the
// item's id, so re-renders keep it); the Content view highlights the row.
let FOCUS_ID = null;
function setFocus(id){
  FOCUS_ID = id || null;
  let tag = document.getElementById("focus-style");
  if (!tag) { tag = document.createElement("style"); tag.id = "focus-style"; document.head.appendChild(tag); }
  tag.textContent = FOCUS_ID
    ? `#main .sec-group:not(:has([data-os-id="${CSS.escape(FOCUS_ID)}"])){display:none}`
    : "";
}
function openDeliverable(item){
  if (!item) return;
  window.ChatRooms?.showCanvas();
  const from = item.bot ? { bot: item.bot, at: item.updated } : null;
  navigate(item.link, { ...(item.profile ? { profileSlug: item.profile } : {}), focus: item.focus || null, from });
  if (!item.focus) return;
  let tries = 0;
  const seek = () => {
    const el = $("#main").querySelector(`[data-os-id="${CSS.escape(item.focus)}"]`);
    if (!el) { if (++tries < 20) setTimeout(seek, 100); return; }
    (el.closest(".post-row, .pf, .pcard, .post, .sec-group") || el).scrollIntoView({ block: "start", behavior: "smooth" });
  };
  setTimeout(seek, 60);
}

// ── Content (canvas default): every profile's posts, one card per profile ────
let CONTENT_FILTER = "all";              // all | ideas | drafts | ready | published
const CONTENT_EXPANDED = new Set();      // profile slugs showing every row
const CONTENT_COLLAPSED = new Set();     // profile slugs whose card is folded
const CONTENT_TABS = [["posts", "Posts"], ["calendar", "Calendar"], ["briefs", "Briefs"], ["voice", "Voice"]];
const CONTENT_FILTERS = [["all", "All"], ["ideas", "Ideas"], ["drafts", "Drafts"], ["ready", "Ready"], ["published", "Published"]];
const FILTER_EMPTY = { all: "No posts yet", ideas: "No ideas", drafts: "No drafts", ready: "Nothing ready", published: "Nothing published" };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The workspace's project, or every project in HQ. Profiles carry their
// identity hue, by position in the project.
const contentProjects = () => (wsProject() ? [wsProject()] : _TREE).map(p => ({
  ...p, profiles: (p.profiles || []).map((pf, i) => ({ ...pf, hue: ID_HUES[i % ID_HUES.length] })),
}));

const channelChips = channels => channels.map(c =>
  `<span class="plat">${platIcon(c.platform)}${esc(PLAT_NAME[c.platform] || c.platform || c.slug)}</span>`).join("");

// One post as a row: status, channels, date; title; excerpt; next step.
function postRowHtml(post, channels, { selectable = false, focused = false, selected = false, note = "" } = {}){
  const na = nextActionFor(post);
  const osId = OSID.post(post.id);
  const platforms = [...new Set((post.channels || []).map(sl => channels.find(c => c.slug === sl)).filter(Boolean))];
  const excerpt = post.concept
    ? `<div class="pr-x">${esc(post.concept)}</div>`
    : post.brief_path ? "" : `<div class="pr-x empty">Not written yet</div>`;
  return `<div class="post-row${focused ? " focused" : ""}${selected ? " sel" : ""}" data-post="${esc(post.id)}"${osId ? ` data-os-id="${esc(osId)}"` : ""}>
    ${selectable ? `<input type="checkbox" class="selbox" data-sel="${esc(post.id)}"${selected ? " checked" : ""} aria-label="Select post">` : ""}
    <div class="pr-top"><span class="stp ${STATUS_PILL_CLASS[post.status] || "idea"}">${esc(plainStatus(post.status))}</span>${channelChips(platforms)}
      <span class="when">${icon("clock")}${esc(post.date || "No date")}</span></div>
    <button type="button" class="pr-title" data-view="${esc(post.id)}">${esc(post.working_title || post.pillar || post.id)}</button>
    ${excerpt}
    <div class="pr-foot">
      ${na ? `<button class="btn sm${na.kind === "review" ? " primary" : ""}" data-act="${esc(post.id)}">${esc(na.label)}</button>` : ""}
      <button class="btn sm ghost" data-edit="${esc(post.id)}">Edit</button>
      ${note ? `<span class="focus-note">${icon("reply")}${esc(note)}</span>` : ""}
      <span class="pr-id">${sectionIdChip(osId)}</span>
    </div></div>`;
}

// Profile header shared by the Content cards and the profile page.
function profileHeadHtml(profile, { tag = "div", end = "", sub = "", chips = true } = {}){
  const name = profile.name || profile.slug;
  return `<${tag} class="pf-head">
      <span class="av t-${profile.hue || "slate"}" aria-hidden="true">${esc(name[0].toUpperCase())}</span>
      <div class="pf-id">
        <div class="pf-name"><a href="#/profile/${esc(profile.slug)}">${esc(name)}</a>${sectionIdChip(OSID.prof(profile.slug))}</div>
        <div class="pf-sub">${chips ? channelChips(profile.channels || []) : ""}${sub}</div>
      </div>
      <div class="pf-end">${end}</div>
    </${tag}>`;
}

// One collapsible card per profile; the first `limit` rows, then "Show N more".
function profileCardHtml(group, { limit = ContentView.PAGE_SIZE, filter = "all", focusId = null, note = "", collapsed = false } = {}){
  const { profile, counts, needsYou } = group;
  const posts = group.posts.filter(p => filter === "all" || STAGE_GROUP[p.status] === filter);
  const shown = posts.slice(0, limit);
  const channels = profile.channels || [];
  const mix = ["ideas", "drafts", "ready", "published"].filter(b => counts[b])
    .map(b => `<i class="mix-${b}" style="width:${(counts[b] / counts.all * 100).toFixed(1)}%"></i>`).join("");
  const rows = shown.map(p => {
    const focused = !!focusId && OSID.post(p.id) === focusId;
    return postRowHtml(p, channels, { focused, note: focused ? note : "" });
  }).join("") || `<div class="empty">${FILTER_EMPTY[group.posts.length ? filter : "all"]}</div>`;
  const osId = OSID.prof(profile.slug);
  return `<details class="pf${focusId && focusId === osId ? " focused" : ""}"${collapsed ? "" : " open"} data-profile="${esc(profile.slug)}"${osId ? ` data-os-id="${esc(osId)}"` : ""}>
    ${profileHeadHtml(profile, { tag: "summary",
      sub: needsYou ? `<span class="pf-needs">${needsYou} waiting on you</span>` : "",
      end: `${counts.all ? `<span class="mix" aria-hidden="true">${mix}</span>` : ""}<span class="cnt">${plural(counts.all, "post")}</span>${icon("chevron", "chev")}` })}
    ${rows}
    ${posts.length > shown.length ? `<button class="pf-more" data-pf-more="${esc(profile.slug)}">Show ${posts.length - shown.length} more</button>` : ""}
  </details>`;
}

const contentHead = (tab, summary) => `<div class="doc-head">
    <h2 class="doc-title">Content</h2>
    <p class="doc-lede">${esc(summary)}</p>
    <nav class="doc-tabs">${CONTENT_TABS.map(([k, label]) => `<a href="#/content${k === "posts" ? "" : "/" + k}"${k === tab ? ' class="on" aria-current="page"' : ""}>${label}</a>`).join("")}</nav>
  </div>`;

async function renderContent(tab = "posts"){
  const from = _NAV_EXTRAS.from || null, focusArrived = !!_NAV_EXTRAS.focus;
  const stillOn = routeGuard();
  const projects = contentProjects();
  const profiles = projects.flatMap(p => p.profiles);
  const noProfiles = `<div class="empty">No profiles yet. Ask a teammate in chat to set one up.</div>`;
  if (tab === "calendar") {
    const scope = wsProject() ? new Set(projects.flatMap(p => [p.slug, ...(p.products || []).map(x => x.slug),
      ...p.profiles.flatMap(pf => [pf.slug, ...(pf.channels || []).map(c => c.slug)])])) : null;
    return renderTimeline({ scope, head: contentHead(tab, plural(profiles.length, "profile")) });
  }
  if (tab === "briefs" || tab === "voice") {
    const [seg, key, ic, noun] = tab === "briefs" ? ["brief-specs", "specs", "doc", "brief spec"] : ["voices", "voices", "mic", "voice"];
    const lists = await Promise.all(profiles.map(pf =>
      api(`/api/profile/${encodeURIComponent(pf.slug)}/${seg}`).then(d => d[key] || []).catch(() => [])));
    if (!stillOn()) return;
    const cards = profiles.map((pf, i) => `<section class="pf">${profileHeadHtml(pf, { end: `<span class="cnt">${plural(lists[i].length, noun)}</span>` })}
        ${lists[i].map(x => `<a class="doc-row" href="#/profile/${esc(pf.slug)}/setup">${icon(ic)}<b>${esc(x.id)}</b>
          <span class="doc-row-x">${esc((x.text || "").split("\n").find(l => l.trim()) || "Empty")}</span>
          <span class="plat">${esc(x.platforms || "all")}</span></a>`).join("")
          || `<div class="empty">No ${noun}s yet</div>`}
      </section>`).join("");
    $("#main").innerHTML = `<div class="scroll"><div class="doc">${contentHead(tab, plural(profiles.length, "profile"))}${cards || noProfiles}</div></div>`;
    return;
  }

  const lists = await Promise.all(profiles.map(pf =>
    api(`/api/profile/${encodeURIComponent(pf.slug)}/posts`).catch(() => [])));
  if (!stillOn()) return;
  const posts = lists.flat();
  const byId = new Map(posts.map(p => [p.id, p]));
  const sections = projects.map(project => ({ project, groups: ContentView.groupByProfile(posts, project.profiles) }))
    .filter(sec => sec.groups.length);
  const groups = sections.flatMap(sec => sec.groups);
  const waiting = groups.reduce((n, g) => n + g.needsYou, 0);
  const summary = [plural(profiles.length, "profile"), plural(posts.length, "post"), waiting ? `${waiting} waiting on you` : ""]
    .filter(Boolean).join(" · ");
  const focusId = FOCUS_ID;
  if (focusArrived) CONTENT_FILTER = "all";   // a deliverable's row must be visible
  const age = from && relAge(from.at);
  const note = from ? `from @${botById(from.bot)?.handle || from.bot}, ${age === "now" ? "just now" : `${age} ago`}` : "";
  const total = b => groups.reduce((n, g) => n + g.counts[b], 0);

  const card = g => {
    const focused = !!focusId && (OSID.prof(g.profile.slug) === focusId || g.posts.some(p => OSID.post(p.id) === focusId));
    const opts = { filter: CONTENT_FILTER, focusId, note, collapsed: CONTENT_COLLAPSED.has(g.profile.slug) && !focused };
    return profileCardHtml(g, CONTENT_EXPANDED.has(g.profile.slug) || focused ? { ...opts, limit: Infinity } : opts);
  };
  const draw = () => {
    $("#main").innerHTML = `<div class="scroll"><div class="doc">${contentHead("posts", summary)}
      ${groups.length ? `<div class="filters">${CONTENT_FILTERS.map(([k, label]) => `<button type="button" class="chip${CONTENT_FILTER === k ? " on" : ""}" data-f="${k}" aria-pressed="${CONTENT_FILTER === k}">${label} <span class="n">${total(k)}</span></button>`).join("")}</div>
      ${sections.map(sec => `${wsProject() ? "" : `<h3 class="cv-project">${esc(sec.project.name)}</h3>`}${sec.groups.map(card).join("")}`).join("")}` : noProfiles}
    </div></div>`;
    const main = $("#main");
    main.querySelectorAll("[data-f]").forEach(c => c.onclick = () => { CONTENT_FILTER = c.dataset.f; draw(); });
    main.querySelectorAll("[data-pf-more]").forEach(b => b.onclick = () => { CONTENT_EXPANDED.add(b.dataset.pfMore); draw(); });
    main.querySelectorAll("details.pf").forEach(d => d.addEventListener("toggle", () => {
      d.open ? CONTENT_COLLAPSED.delete(d.dataset.profile) : CONTENT_COLLAPSED.add(d.dataset.profile);
    }));
    const open = (id, suffix = "") => navigate(`#/post/${id}${suffix}`, { profileSlug: byId.get(id)?.profile_slug });
    main.querySelectorAll("[data-view]").forEach(a => a.onclick = () => open(a.dataset.view));
    main.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => open(b.dataset.edit, "/edit"));
    main.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
      const post = byId.get(b.dataset.act);
      b.disabled = true;
      if (!await runNextAction(post, post.profile_slug)) { b.disabled = false; return; }
      renderRail();
      if (stillOn()) renderContent("posts");
    });
  };
  draw();
}

// Files: everything this company's team has handed you, newest first.
async function renderFiles(){
  const p = wsProject();
  if (!p) { $("#main").innerHTML = `<div class="scroll"><div class="empty">Files live in each company's workspace.</div></div>`; return; }
  const stillOn = routeGuard();
  const files = (await api(`/api/files?ws=${encodeURIComponent(_WS)}`).catch(() => ({}))).files || [];
  if (!stillOn()) return;
  let filt = "all";
  const kinds = [...new Set(files.map(f => f.kind))];
  const draw = () => {
    const list = filt === "all" ? files : files.filter(f => f.kind === filt);
    const rows = list.length ? list.map((f, i) => `<div class="post file-row" data-file="${i}">
        <span class="kind-ic">${icon(KIND_META[f.kind]?.[0] || "doc")}</span>
        <div class="t">${esc(f.label)}<small>${esc(KIND_META[f.kind]?.[1] || f.kind)}${f.status ? ` · ${esc(f.status)}` : ""} · ${relAge(f.updated)}</small></div>
        <button class="go">Open</button></div>`).join("")
      : `<div class="empty">Nothing here yet. Ask in #general, e.g. "@cmo draft our positioning".</div>`;
    $("#files-list").innerHTML = `<div class="filters" style="margin-bottom:12px">
        <button class="chip${filt === "all" ? " on" : ""}" data-kind="all">All <span class="n">${files.length}</span></button>
        ${kinds.map(k => `<button class="chip${filt === k ? " on" : ""}" data-kind="${esc(k)}">${esc(KIND_META[k]?.[1] || k)}</button>`).join("")}
      </div><div class="rowc">${rows}</div>`;
    $("#files-list").querySelectorAll("[data-kind]").forEach(c => c.onclick = () => { filt = c.dataset.kind; draw(); });
    $("#files-list").querySelectorAll("[data-file]").forEach(r => r.onclick = () => openDeliverable(list[+r.dataset.file]));
  };
  $("#main").innerHTML = `<div class="topbar"><div><div class="crumbs">${esc(p.name)}</div><h1 class="title">Files</h1></div></div>
    <div class="scroll"><div id="files-list"></div></div>`;
  draw();
}

// Topic channel form (canvas): name, project, members, lead.
async function renderRoomForm(name){
  const stillOn = routeGuard();
  const existing = name ? roomById(`${_WS}/t-${name}`) : null;
  if (name && !existing) { $("#main").innerHTML = `<div class="scroll"><div class="empty">Channel not found.</div></div>`; return; }
  if (!["owner", "admin"].includes(myRole())) { $("#main").innerHTML = `<div class="scroll"><div class="empty">Only admins manage channels.</div></div>`; return; }
  // Private channels: only the owner and the people ticked here see them.
  const folks = _WS === "hq" ? [] : ((await api(`/api/people?ws=${encodeURIComponent(_WS)}`).catch(() => null))?.people || [])
    .filter(r => r.role !== "owner");
  if (!stillOn()) return;
  const inRoom = new Set(existing?.people || []);
  const active = _BOTS.filter(b => b.active);
  const members = new Set(existing ? existing.members : []);
  const lead = existing ? existing.lead : "cos";
  $("#main").innerHTML = `
    ${pageHeader(existing ? `#${existing.name}` : "New channel", "Back", existing ? `<button class="btn" id="room-del">Delete channel</button>` : "")}
    <div class="scroll"><form class="form fpage" id="room-form">
      ${flabel("Name")}${finput("name", existing?.name || "", 'placeholder="acme-launch" autocomplete="off"')}
      <p class="field-help">Lowercase, digits, dashes. Shows as #name.</p>
      <p class="field-help">In ${esc(wsById(_WS)?.name || "HQ")}: bots here only see this workspace.</p>
      ${flabel("Members")}
      <div class="chan-checks">${active.map(b => `<label class="chan-check"><input type="checkbox" class="room-member" value="${esc(b.id)}"${members.has(b.id) ? " checked" : ""}> ${botAvatar(b)} ${esc(b.name)}</label>`).join("")}</div>
      ${flabel("Lead")}${fsel("lead", active.map(b => [b.id, b.name]), lead)}
      <p class="field-help">Untagged messages go to the lead only. Members answer when you @tag them.</p>
      ${_WS === "hq" ? "" : `<label class="chan-check"><input type="checkbox" id="room-private"${existing?.private ? " checked" : ""}> Private channel</label>
      <p class="field-help">Everyone in the workspace sees an open channel. A private one shows only to the owner and the people ticked below.</p>
      <div class="chan-checks" id="room-people"${existing?.private ? "" : " hidden"}>${folks.length ? folks.map(r => `<label class="chan-check"><input type="checkbox" class="room-person" value="${esc(r.id)}"${inRoom.has(r.id) || r.you ? " checked" : ""}${r.you ? " disabled" : ""}> ${esc(r.name)} <small>${esc(r.role)}</small></label>`).join("") : `<p class="field-help">Nobody invited yet. Invite people on the Team page.</p>`}</div>`}
      <div style="margin-top:18px;display:flex;gap:8px"><button class="btn primary" type="submit">${existing ? "Save" : "Create channel"}</button></div>
    </form></div>`;
  $("#room-form").onsubmit = async e => {
    e.preventDefault();
    const v = formVals($("#room-form"));
    const picked = [...document.querySelectorAll(".room-member:checked")].map(i => i.value);
    try {
      const priv = !!$("#room-private")?.checked;
      const ppl = [...document.querySelectorAll(".room-person:checked")].map(i => i.value);
      const out = await jpost("/api/rooms", { ws: _WS, name: v.name, members: picked, lead: v.lead, replace: existing?.name || null,
                                              private: priv, people: priv ? ppl : [] });
      await renderRail();
      window.ChatRooms?.select(out.id);
      history.back();
      toast(existing ? "Channel saved ✓" : "Channel created ✓");
    } catch (err) { toast("✗ " + err.message); }
  };
  const privBox = $("#room-private");
  if (privBox) privBox.onchange = () => { $("#room-people").hidden = !privBox.checked; };
  const del = $("#room-del");
  if (del) del.onclick = async () => {
    if (!del.classList.contains("armed")) { del.classList.add("armed"); del.textContent = "Click again to delete"; return; }
    try {
      await jpost("/api/rooms/delete", { ws: _WS, name: existing.name });
      await renderRail(); window.ChatRooms?.select(`${_WS}/general`);
      toast("Channel deleted ✓ (threads moved to #general)");
    } catch (err) { toast("✗ " + err.message); }
  };
}

function openNewProject(){ navigate("#/project/new"); }
function openEditProject(slug){ navigate(`#/project/${slug}/edit`); }
function openNewProfile(projectSlug){ navigate(`#/project/${projectSlug}/profile/new`); }
function openNewChannel(profileSlug){ navigate(`#/profile/${profileSlug}/channel/new`); }

// New project: one optional folder, linked with edit access once the project exists.
// Every other change (more folders, access, limits, unlink) is on the Folders screen.
function localFolderField(){
  return `${flabel("Folder (optional)")}
    <div class="folder-link-control">
      ${finput("local_folder","",'placeholder="/Users/you/Projects/my-app"')}
      <button type="button" class="btn" data-pick-local-folder>Choose…</button>
    </div>
    <p class="field-help">Bots can read and search it, and change files in it when you tag them. Add more folders, or make one read-only, on the workspace's Folders screen.</p>`;
}

function wireLocalFolderControls(root){
  const input=root.querySelector("input[name=local_folder]");
  const pick=root.querySelector("[data-pick-local-folder]");
  if(pick) pick.onclick=async()=>{
    pick.disabled=true;
    try{
      const out=await jpost("/api/local-folder/pick",{});
      if(!out.cancelled&&out.path) input.value=out.path;
    }catch(err){ toast("✗ "+err.message); }
    finally{ pick.disabled=false; }
  };
}

// ── Folders (owner only): linked folders, their access, channel and bot limits ──
async function renderFolders(){
  const stillOn = routeGuard();
  const ws = _WS;
  const wname = wsById(ws)?.name || (ws === "hq" ? "HQ" : ws);
  $("#main").innerHTML = `
    <div class="topbar"><div><div class="crumbs">${esc(wname)}</div><h1 class="title">Linked folders</h1></div></div>
    <div class="scroll"><div class="help">Folders on this machine that ${esc(wname)}'s bots can work in. Bots read and search every folder here on your own private threads, and change files in Edit folders when you tag them (@bot, /skill or a DM). They cannot run tests, builds or git, and never touch git, editor or secret files. Invited people never see these folders.</div><div id="fold"></div></div>`;
  if (isPerson()) { $("#fold").innerHTML = `<div class="empty">Only the owner manages folders.</div>`; return; }
  const draw = async () => {
    const d = await api(`/api/folders?ws=${encodeURIComponent(ws)}`).catch(e => ({ error: e.message }));
    if (!stillOn()) return;
    const box = $("#fold");
    if (d.error) { box.innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
    const rows = d.folders || [];
    const accessSel = (cur, attrs) => `<select ${attrs} aria-label="Access">${[["read", "Read"], ["edit", "Edit"]].map(([v, l]) =>
      `<option value="${v}"${cur === v ? " selected" : ""}>${l}</option>`).join("")}</select>`;
    const warn = r => [r.missing && "Missing on this machine", r.problem && esc(r.problem),
      r.not_git && "Not a git repo: edits can't be undone easily", r.cant_write && "Leo Bot can't write here"]
      .filter(Boolean).map(t => `<span class="folder-warn">${t}</span>`).join("");
    const folderRows = rows.map(r => `<div class="post"><div class="t"><b>${esc(r.name)}</b><small>${esc(r.path)}</small>${warn(r)}</div>
        ${accessSel(r.access, `data-access="${esc(r.id)}"`)}
        ${d.chooser && !r.missing ? `<button class="go" data-open="${esc(r.id)}">Open</button>` : ""}
        <button class="more" data-unlink="${esc(r.id)}">Unlink</button></div>`).join("")
      || `<div class="empty">No folders yet.</div>`;
    const add = `<form class="integ-key folder-add">
        <input name="path" placeholder="${d.chooser ? "/Users/you/Projects/my-app" : "/home/you/my-app (a folder on this server)"}" aria-label="Folder path">
        ${accessSel("edit", 'name="access"')}
        ${d.chooser ? `<button class="btn sm" type="button" data-choose>Choose…</button>` : ""}
        <button class="btn sm primary" type="submit">Add folder</button></form>`;
    const limitRow = (kind, key, label, layer) => {
      const limited = layer !== undefined;
      const per = limited ? rows.map(r => `<label class="folder-pick">${esc(r.name)}
          <select data-limit-folder="${esc(r.id)}" data-kind="${kind}" data-key="${esc(key)}" aria-label="${esc(r.name)}">
          ${(r.access === "edit" ? [["", "None"], ["read", "Read"], ["edit", "Edit"]] : [["", "None"], ["read", "Read"]]).map(([v, l]) => `<option value="${v}"${(layer[r.id] || "") === v ? " selected" : ""}>${l}</option>`).join("")}
          </select></label>`).join("") : "";
      return `<div class="eng-choice"><label>${esc(label)} <select data-limit-mode data-kind="${kind}" data-key="${esc(key)}">
          <option value="all"${limited ? "" : " selected"}>All folders</option><option value="limit"${limited ? " selected" : ""}>Limited</option></select></label>
          ${limited ? `<div class="folder-limits">${per}</div>` : ""}</div>`;
    };
    const layers = rows.length ? `
      <section class="integ-group"><h3>Channels</h3><div class="help">A channel can only narrow what its bots get. DMs follow the bot's own limit.</div>
        ${(d.channels || []).map(c => limitRow("rooms", c.id, "#" + c.name, d.rooms?.[c.id])).join("")}</section>
      <section class="integ-group"><h3>Bots</h3>
        ${(d.team || []).map(b => limitRow("bots", b.id, `${b.name} (@${b.handle})`, d.bots?.[b.id])).join("")}</section>` : "";
    const broken = d.damaged ? `<div class="folder-warn">This workspace's folders.json is damaged, so bots get no folders and changes are refused. Fix the file by hand or delete it, then reload.</div>` : "";
    box.innerHTML = `<section class="integ-group">${broken}${folderRows}${add}</section>${layers}`;
    const call = async (route, body) => {
      try { await jpost(`/api/folders/${route}`, { ws, ...body }); await draw(); }
      catch (err) { toast("✗ " + err.message); await draw(); }
    };
    box.querySelectorAll("[data-access]").forEach(s => s.onchange = () => call("access", { id: s.dataset.access, access: s.value }));
    box.querySelectorAll("[data-open]").forEach(b => b.onclick = () => jpost("/api/folders/open", { ws, id: b.dataset.open }).catch(err => toast("✗ " + err.message)));
    box.querySelectorAll("[data-unlink]").forEach(b => b.onclick = () => {
      if (!b.dataset.armed) { b.dataset.armed = "1"; b.textContent = "Click again to unlink"; return; }
      call("remove", { id: b.dataset.unlink });
    });
    const form = box.querySelector(".folder-add");
    form.querySelector("[data-choose]")?.addEventListener("click", async e => {
      e.target.disabled = true;
      try { const out = await jpost("/api/local-folder/pick", {}); if (!out.cancelled && out.path) form.elements.path.value = out.path; }
      catch (err) { toast("✗ " + err.message); }
      e.target.disabled = false;
    });
    form.onsubmit = e => {
      e.preventDefault();
      const path = form.elements.path.value.trim();
      if (!path) return toast("Type or choose a folder first");
      call("add", { path, access: form.elements.access.value });
    };
    const layerOf = (kind, key) => {
      const out = {};
      box.querySelectorAll(`[data-limit-folder][data-kind="${kind}"][data-key="${CSS.escape(key)}"]`).forEach(s => { if (s.value) out[s.dataset.limitFolder] = s.value; });
      return out;
    };
    box.querySelectorAll("[data-limit-mode]").forEach(s => s.onchange = () => {
      const { kind, key } = s.dataset;
      // Limited starts from every folder at its own access: nothing changes until you narrow it.
      const start = Object.fromEntries(rows.map(r => [r.id, r.access]));
      call("limit", { kind, key, limit: s.value === "all" ? null : start });
    });
    box.querySelectorAll("[data-limit-folder]").forEach(s => s.onchange = () =>
      call("limit", { kind: s.dataset.kind, key: s.dataset.key, limit: layerOf(s.dataset.kind, s.dataset.key) }));
  };
  draw();
}

function highlight(){
  renderCanvasNav();
  const mark=(a,on)=>{a.classList.toggle("active",on);if(on)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");};
  const canvasOpen = !document.getElementById("app")?.classList.contains("canvas-hidden");
  document.querySelectorAll("#rail [data-view]").forEach(a=>mark(a,canvasOpen&&STATE.view===a.dataset.view&&!STATE.project&&!STATE.profile&&!STATE.channelGuidelines));
  document.querySelectorAll("#rail [data-room]").forEach(a=>mark(a,a.dataset.room===window.ChatRooms?.current()));
  const inSettings = STATE.view === "settings";
  document.querySelectorAll("#ws-bar [data-ws]").forEach(b=>b.classList.toggle("active",!inSettings&&b.dataset.ws===_WS));
  document.getElementById("ws-settings")?.classList.toggle("active",inSettings);
  requestAnimationFrame(()=>document.querySelector("#rail [aria-current='page']")?.scrollIntoView({block:"nearest"}));
}
function selectGlobal(v){ navigate(`#/${v}`); }
function selectSection(project,section){ OPEN.projects.add(project); saveOpen(); navigate(`#/project/${project}/${section}`); }
function selectProfile(slug, chanFilter){ navigate(`#/profile/${slug}`, chanFilter?{chanFilter}:{}); }
function selectChannelGuidelines(slug){
  const parentProf=_TREE.flatMap(p=>p.profiles).find(pr=>pr.channels.some(c=>c.slug===slug));
  const parentProj=_TREE.find(p=>p.profiles.some(pr=>pr.channels.some(c=>c.slug===slug)));
  if(parentProf){OPEN.profiles.add(parentProf.slug);} if(parentProj){OPEN.projects.add(parentProj.slug);}
  if(parentProf||parentProj) saveOpen();
  STATE={view:"channelGuidelines",project:null,section:null,profile:null,channelGuidelines:slug}; highlight(); renderChannelGuidelines(slug); }

// Re-render the rail + the current center view (used after the chat agent
// mutates entities via osctl, so new/changed items appear without a reload).
async function refreshViews(){
  await renderRail();
  const v = STATE.view;
  if (v === "content") return renderContent(STATE.tab);
  if (v === "calendar") return renderTimeline();
  if (v === "day") return renderDay(STATE.day);
  if (v === "operations") return renderOperations();
  if (v === "team") return renderTeam();
  if (v === "approvals") return renderApprovals();
  if (v === "settings") return renderSettings(STATE.tab);
  if (v === "folders") return renderFolders();
  if (v === "files") return renderFiles();
  if (v === "section") return renderProjectSection(STATE.project, STATE.section);
  if (v === "profile") return onProfileList(STATE.profile) ? renderProfile(STATE.profile) : undefined;
  if (v === "profileSetup") return renderProfileSetup(STATE.profile);
  if (v === "channelGuidelines") return renderChannelGuidelines(STATE.channelGuidelines);
  return renderNeeds();
}

async function renderNeeds(){
  const stillOn = routeGuard();
  const [tl] = await Promise.all([api("/api/timeline").catch(()=>[]), ensureIdRegistry()]);
  if (!stillOn()) return;
  const iso=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  const today=iso(new Date());
  const rows=[];
  const push=(prio,r,meta,go)=>{
    const act = r.kind==="activity"&&r.status!=="done" ? "Mark done" : r.kind==="post" ? "Open post" : "Open";
    rows.push({prio,kind:r.kind,ic:kindIcon(r.kind),t:r.title||r.kind,m:meta,act,go}); };
  const goFor = r => r.kind==="post"&&r.ref_id ? ()=>navigate(`#/post/${r.ref_id}`,{profileSlug:r.entity_slug})
    : r.kind==="activity" ? ()=>navigate("#/operations")
    : r.kind==="experiment" ? ()=>navigate(`#/project/${r.entity_slug}/experiments`)
    : ()=>navigate("#/calendar");
  // everything scheduled for today
  tl.filter(r=>r.date===today).forEach(r=>push(0,r,`${r.entity_name||r.kind} · today${r.status?` · ${esc(r.status)}`:""}`,goFor(r)));
  // activities that slipped past their date
  tl.filter(r=>r.kind==="activity"&&r.status!=="done"&&r.date&&r.date<today).forEach(r=>push(1,r,`${r.entity_name||"Portfolio"} · overdue since ${r.date}`,goFor(r)));
  rows.sort((a,b)=>a.prio-b.prio);
  const dateLabel=new Date().toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric"});
  const body = rows.length ? `<div class="list">${rows.map((r,i)=>`
    <div class="arow tone-${esc(r.kind)}" data-need="${i}"><span class="stripe"></span>
      <span class="ig">${r.ic}</span>
      <span class="body"><span class="t">${esc(r.t)}</span><span class="m">${esc(r.m)}</span></span>
      <span class="act">${esc(r.act)}</span></div>`).join("")}</div>`
    : `<div class="sec-empty"><p>Nothing planned for today.</p><p class="sec-hint">Posts, activities, and milestones scheduled for today show up here.</p></div>`;
  $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs">${esc(dateLabel)}</div><h1 class="title">Tasks of the day</h1></div></div>
    <div class="scroll"><p class="lead">${rows.length?`Due today or overdue: ${plural(rows.length,"item")}.`:"Everything planned for today."}</p>${body}</div>`;
  rows.forEach((r,i)=>{ const el=$("#main").querySelector(`[data-need="${i}"]`); if(el) el.onclick=r.go; });
}

async function renderOperations(){
  const stillOn = routeGuard();
  const all = await api("/api/timeline");
  if (!stillOn()) return;
  const items = all.filter(r=>r.kind==="activity");
  const statuses=["planned","running","blocked","done"];
  let FILT="active";
  function draw(){
    const list = FILT==="active" ? items.filter(r=>r.status!=="done") : FILT==="done" ? items.filter(r=>r.status==="done") : items;
    const projectOpts = _TREE.map(p=>`<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join("");
    const rows = list.length ? list.map(r=>{
      const done=r.status==="done";
      return `<div class="post" style="${done?"opacity:.5":""}">
        <span class="stp ${done?"sched":"idea"}">${esc(r.status||"planned")}</span>
        <div class="t">${esc(r.title)}<small>${esc([r.entity_name||r.entity_slug, r.date].filter(Boolean).join(" · "))}</small></div>
        ${!done?`<button class="go" data-done-title="${esc(r.title)}" data-done-entity="${esc(r.entity_slug||"")}">Mark done</button>`:""}
        <button class="more" data-ev='${esc(JSON.stringify({...r,title:(r.title||"").slice(0,80)}))}'>Edit</button>
      </div>`;
    }).join("") : `<div class="empty">Nothing here.</div>`;
    const active=items.filter(r=>r.status!=="done").length;
    $("#ops-list").innerHTML=`
      <div class="filters" style="margin-bottom:14px">
        <button type="button" class="chip${FILT==="active"?" on":""}" data-of="active" aria-pressed="${FILT==="active"}">Active <span class="n">${active}</span></button>
        <button type="button" class="chip${FILT==="all"?" on":""}" data-of="all" aria-pressed="${FILT==="all"}">All <span class="n">${items.length}</span></button>
        <button type="button" class="chip${FILT==="done"?" on":""}" data-of="done" aria-pressed="${FILT==="done"}">Done <span class="n">${items.filter(r=>r.status==="done").length}</span></button>
      </div>
      <div class="rowc">${rows}</div>`;
    $("#ops-list").querySelectorAll(".chip").forEach(c=>c.onclick=()=>{ FILT=c.dataset.of; draw(); });
    $("#ops-list").querySelectorAll("[data-done-title]").forEach(b=>b.onclick=async()=>{
      try{ await jpost("/api/activity/done",{title:b.dataset.doneTitle,entity:b.dataset.doneEntity}); toast("Done ✓"); renderOperations(); }
      catch(e){ toast("✗ "+e.message); }
    });
    $("#ops-list").querySelectorAll("[data-ev]").forEach(el=>el.onclick=e=>{ e.stopPropagation(); try{toggleEvDetail(el,JSON.parse(el.dataset.ev));}catch(_){} });
  }
  $("#main").innerHTML=`
    <div class="topbar"><div><div class="crumbs">Across everything</div><h1 class="title">Operations</h1></div>
      <div style="margin-left:auto"><button class="btn primary" id="newOpBtn">${icon("plus")} Activity</button></div></div>
    <div class="scroll"><div id="ops-list"></div></div>`;
  draw();
  $("#newOpBtn").onclick=()=>navigate("#/activity/new");
}
// ── Team: bots as teammates, org chart + on/off. A switched-off bot's skills
// roll up to its manager (server decides); Chief of Staff is always on.
// People in this workspace (core.people) above its bots: roles, invites, leave.
const EXPIRY_LABEL = { "1d": "1 day", "7d": "7 days", "30d": "30 days" };
function peopleSection(pv, link){
  if (!pv) return "";
  const when = ts => ts ? new Date(ts * 1000).toLocaleDateString() : "";
  const rows = pv.people.map(r => `<div class="post person-row">
      <span class="av person-av" aria-hidden="true">${esc((r.name || "?")[0].toUpperCase())}</span>
      <div class="t"><b>${esc(r.name)}</b>${r.you ? ` <small class="inline">you</small>` : ""} <span class="tag">${esc(r.role)}</span>
        ${r.joined ? `<small>Joined ${esc(when(r.joined))}</small>` : ""}</div>
      ${r.removable ? `<button class="more" data-person-rm="${esc(r.id)}" data-you="${r.you ? 1 : 0}">${r.you ? "Leave" : "Remove"}</button>` : ""}
    </div>`).join("");
  let invite = "";
  if (pv.can_invite.length) {
    invite = pv.public_url ? `<div class="invite-box">
        <label>Role <select id="inv-role">${pv.can_invite.map(r => `<option value="${r}"${r === "member" ? " selected" : ""}>${r}</option>`).join("")}</select></label>
        <label>Expires after <select id="inv-exp">${Object.entries(EXPIRY_LABEL).map(([k, v]) => `<option value="${k}"${k === "7d" ? " selected" : ""}>${v}</option>`).join("")}</select></label>
        <button class="btn primary" id="inv-go">Create invite link</button>
      </div>
      ${link ? `<div class="invite-link"><input readonly value="${esc(link)}" aria-label="Invite link"><button class="btn" id="inv-copy">Copy</button>
        <small>Works once. Send it to one person; they open it, type their name and join.</small></div>` : ""}`
      : `<div class="help">To invite people, ${myRole() === "owner" ? "set the sharing address in Settings, Access first" : "the owner has to set a sharing address first"}.</div>`;
    const pend = pv.invites || [];
    if (pend.length) invite += `<div class="invite-pending">${pend.map(i => `<div class="invite-row"><span class="tag">${esc(i.role)}</span>
        <small>expires ${esc(when(i.expires))}</small><button class="btn sm ghost" data-inv-rm="${esc(i.id)}">Revoke</button></div>`).join("")}</div>`;
  }
  return `<section class="team-sec"><h3>People</h3>${invite}<div class="rowc">${rows}</div></section><section class="team-sec"><h3>Bots</h3>`;
}
function sharingSection(url){
  return `<section class="team-sec"><h3>Sharing address</h3>
    <div class="help">The https address a tunnel gives this machine (Tailscale Serve or Funnel, Cloudflare Tunnel), like https://name.example (no path). Invite links use it. Setup: docs/sharing.md. The terminal stays on this machine only.</div>
    <form class="share-form" id="share-form"><input name="url" value="${esc(url || "")}" placeholder="https://" aria-label="Sharing address" autocomplete="off">
      <button class="btn" type="submit">Save</button></form></section>`;
}

async function renderTeam(){
  const stillOn = routeGuard();
  let costs = { bots: {}, total: {}, days: 30 };
  let integ = { configured: false, connected: [] };
  let pv = null, link = "";
  const person = isPerson();
  const opened = new Set();  // bots whose details are open: kept across redraws
  const draw = () => {
    const wsApps = Object.keys(integ.picked?.[_WS] || {}).sort();  // apps with an account ticked here
    const appName = slug => integ.apps_meta?.[slug]?.name || slug;
    const grantsOf = b => integ.grants?.[_WS]?.[b.id] || {};
    // One quiet line per bot; skills, app access and memory wait behind Details.
    const onRow = b => {
      const boss = b.reports_to === "ceo" ? "You" : (botById(b.reports_to)?.name || b.reports_to);
      const c = costs.bots[b.id];
      const apps = wsApps.filter(s => grantsOf(b)[s]).map(appName);
      const meta = [`Reports to ${esc(boss)}`,
        !person && c?.turns ? `${c.turns} ${c.turns === 1 ? "reply" : "replies"}${c.cost ? `, ${fmtCost(c.cost)}` : ""}` : "",
        apps.length ? esc(apps.join(", ")) : ""].filter(Boolean).join(" · ");
      const own = new Set(b.skills);
      const covered = b.effective_skills.filter(s => !own.has(s));
      const open = opened.has(b.id);
      return `<div class="post team-row" data-bot="${esc(b.id)}">
        ${botAvatar(b)}
        <div class="t"><b>${esc(b.name)}</b> <span class="bot-handle">@${esc(b.handle)}</span>
          <small class="team-about">${esc(b.about)}</small><small class="team-meta">${meta}</small></div>
        <button class="more team-more" data-bot-more="${esc(b.id)}" aria-expanded="${open}">Details</button>
        ${person ? "" : b.locked ? `<span class="team-fixed" title="Other bots roll up to it">Always on</span>` : `<button class="more" data-bot-toggle="${esc(b.id)}" data-on="1">Switch off</button>`}
        <div class="team-detail"${open ? "" : " hidden"}>
          <div class="td-sec"><h4>Skills</h4><div class="team-skills">${b.skills.map(s => `<span class="tag">${esc(s)}</span>`).join("")}${covered.map(s => `<span class="tag covered">${esc(s)}</span>`).join("")}</div>
            ${b.covering.length ? `<small>Outlined ones it covers for ${b.covering.map(x => esc(botById(x)?.name || x)).join(", ")}, switched off.</small>` : ""}</div>
          ${!person && integ.configured && wsApps.length ? `<div class="td-sec"><h4>Apps</h4><div class="app-grants">${wsApps.map(slug => {
            const scope = grantsOf(b)[slug];
            return `<label>${appLogoHtml(slug, integ.apps_meta?.[slug])}${esc(appName(slug))} <select data-grant-bot="${esc(b.id)}" data-grant-app="${esc(slug)}">
              ${["", "read", "write"].map(s => `<option value="${s}"${(scope || "") === s ? " selected" : ""}>${grantLabel(s || undefined)}</option>`).join("")}</select></label>`;
          }).join("")}</div><small>Read + write still asks you before anything that isn't a read.</small></div>` : ""}
          ${person ? "" : `<div class="td-sec"><h4>Memory</h4><div class="mem-box" hidden></div></div>`}
        </div>
      </div>`;
    };
    const offRow = b => `<div class="post team-row off">
        ${botAvatar(b)}
        <div class="t"><b>${esc(b.name)}</b> <small class="team-meta">${esc(botById(b.manager)?.name || "Chief of Staff")} handles its skills${b.recommended ? " · recommended here" : ""}</small></div>
        ${person ? "" : `<button class="go" data-bot-toggle="${esc(b.id)}" data-on="0">Switch on</button>`}
      </div>`;
    const on = _BOTS.filter(b => b.active), off = _BOTS.filter(b => !b.active);
    const t = costs.total || {};
    const noApps = !person && integ.configured && (integ.connected || []).length && !wsApps.length
      ? `<div class="help">No app account is ticked for this workspace yet. Tick one in Settings, Integrations.</div>` : "";
    $("#team-list").innerHTML = peopleSection(pv, link) + noApps
      + `${!person && t.turns ? `<div class="team-total">${_WS === "hq" ? "All workspaces, last" : "Last"} ${costs.days} days: ${t.turns} ${t.turns === 1 ? "reply" : "replies"} · ${fmtUsage(t.tokens, t.cost)} (est.)</div>` : ""}
        <div class="rowc">${on.map(onRow).join("")}</div>
        ${off.length ? `<h4 class="team-sub">Switched off · ${off.length}</h4><div class="rowc">${off.map(offRow).join("")}</div>` : ""}${pv ? "</section>" : ""}`;
    wirePeople();
    $("#team-list").querySelectorAll("[data-bot-more]").forEach(btn => btn.onclick = () => {
      const row = btn.closest(".team-row"), detail = row.querySelector(".team-detail"), id = btn.dataset.botMore;
      const open = detail.hidden;
      detail.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      open ? opened.add(id) : opened.delete(id);
      const mem = row.querySelector(".mem-box");
      if (mem && open && mem.hidden) toggleMemory(row, id);
    });
    // Details left open survive a redraw: refill their memory.
    $("#team-list").querySelectorAll(".team-row[data-bot]").forEach(row => {
      if (opened.has(row.dataset.bot) && row.querySelector(".mem-box")) toggleMemory(row, row.dataset.bot);
    });
    $("#team-list").querySelectorAll("[data-grant-bot]").forEach(sel => sel.onchange = async () => {
      const bot = sel.dataset.grantBot;
      // No prototype: an app slug is only ever a plain key here.
      const apps = Object.assign(Object.create(null), integ.grants?.[_WS]?.[bot] || {});
      if (sel.value) apps[sel.dataset.grantApp] = sel.value; else delete apps[sel.dataset.grantApp];
      try {
        const r = await jpost("/api/integrations/grants", { ws: _WS, bot, apps });
        // New objects with own keys: a name like __proto__ can't reach Object.prototype.
        integ.grants = { ...integ.grants, [_WS]: { ...integ.grants?.[_WS], [bot]: r.apps } };
      } catch (e) { toast("✗ " + e.message); draw(); }
    });
    $("#team-list").querySelectorAll("[data-bot-toggle]").forEach(btn => btn.onclick = async () => {
      try {
        const d = await jpost(`/api/bots/${btn.dataset.botToggle}/active`, { ws: _WS, active: btn.dataset.on !== "1" });
        _BOTS = d.bots || _BOTS; draw(); renderRail().catch(() => {});
      } catch (e) { toast("✗ " + e.message); }
    });
  };
  const wirePeople = () => {
    const box = $("#team-list");
    const go = box.querySelector("#inv-go");
    if (go) go.onclick = async () => {
      go.disabled = true;
      try {
        const out = await jpost("/api/invites", { ws: _WS, role: $("#inv-role").value, expiry: $("#inv-exp").value });
        pv = out; link = out.link; draw();
      } catch (e) { toast("✗ " + e.message); go.disabled = false; }
    };
    const copy = box.querySelector("#inv-copy");
    if (copy) copy.onclick = async () => {
      try { await navigator.clipboard.writeText(link); toast("Invite link copied ✓"); }
      catch { box.querySelector(".invite-link input").select(); }
    };
    box.querySelectorAll("[data-inv-rm]").forEach(b => b.onclick = async () => {
      try { pv = await jpost("/api/invites/revoke", { ws: _WS, id: b.dataset.invRm }); link = ""; draw(); }
      catch (e) { toast("✗ " + e.message); }
    });
    box.querySelectorAll("[data-person-rm]").forEach(b => b.onclick = async () => {
      const you = b.dataset.you === "1";
      if (!b.classList.contains("armed")) { b.classList.add("armed"); b.textContent = you ? "Click again to leave" : "Click again to remove"; return; }
      try {
        const out = await jpost("/api/people/remove", { ws: _WS, id: b.dataset.personRm });
        if (out.left) { location.href = "/"; return; }
        pv = out; draw(); toast("Removed ✓");
      } catch (e) { toast("✗ " + e.message); }
    });
  };
  $("#main").innerHTML = `
    <div class="topbar"><div><div class="crumbs">${esc(wsById(_WS)?.name || "HQ")} · ${esc(_WS === "hq" ? "holding" : wsById(_WS)?.kind || "")}</div><h1 class="title">Team</h1></div></div>
    <div class="scroll"><div class="help">${person
      ? "Who works in this workspace: people and bots. Tag a bot with @handle in chat."
      : `${_WS === "hq" ? "HQ's bots. People join project workspaces, never HQ." : "People you invited and this workspace's bots."} Tag a teammate with @handle in chat. A switched-off bot's skills go to its manager.`}</div><div id="team-list"></div>${person ? `<div id="team-runners"></div>` : ""}</div>`;
  if (person) drawRunners($("#team-runners"), stillOn);
  $("#team-list").addEventListener("error", e => { if (e.target.matches?.(".integ-logo img")) e.target.remove(); }, true);
  if (!_BOTS.length) _BOTS = (await api(`/api/bots?ws=${encodeURIComponent(_WS)}`).catch(() => ({}))).bots || [];
  pv = _WS === "hq" ? null : await api(`/api/people?ws=${encodeURIComponent(_WS)}`).catch(() => null);
  if (!person) {
    costs = await api(`/api/costs?ws=${encodeURIComponent(_WS)}`).catch(() => costs);
    integ = await api("/api/integrations").catch(() => integ);
  }
  if (stillOn()) draw();
}

// ── Approvals inbox: proposals bots are waiting on you for (HQ: every company).
function relAge(ts){
  const d = Date.now() / 1000 - (ts || 0);
  return d < 60 ? "now" : d < 3600 ? `${Math.floor(d / 60)}m` : d < 86400 ? `${Math.floor(d / 3600)}h` : `${Math.floor(d / 86400)}d`;
}
async function renderApprovals(){
  const items = _APPROVALS;
  const rows = items.length ? items.map(a => {
    const b = botById(a.bot) || { id: a.bot, name: a.bot };
    const room = (a.room || "").split("/")[1] || "general";
    const where = `${wsById(a.ws)?.name || a.ws} · ${room.startsWith("dm-") ? "DM" : "#" + room.replace(/^t-/, "")}`;
    if (isAppAction(a)) return `<div class="post approval-row app-row" data-app-id="${esc(a.id)}">
      ${botAvatar(b)}
      <div class="t"><small>${esc(b.name)} · ${esc(where)} · ${relAge(a.created)}</small>
        <div class="approval-bar app-card">${appCardHtml(a)}</div></div>
      <button class="go" data-ap-go="${esc(a.id)}">Open</button>
    </div>`;
    return `<div class="post approval-row">
      ${botAvatar(b)}
      <div class="t">${esc(a.summary)}<small>${esc(b.name)} · ${esc(where)} · ${relAge(a.created)}</small></div>
      <button class="go" data-ap-go="${esc(a.id)}">Open</button>
      <button class="go primary-go" data-ap-ok="${esc(a.id)}">Approve</button>
      <button class="more" data-ap-no="${esc(a.id)}">Dismiss</button>
    </div>`;
  }).join("") : `<div class="empty">Nothing waiting on you.</div>`;
  $("#main").innerHTML = `
    <div class="topbar"><div><div class="crumbs">${esc(_WS === "hq" ? "Every workspace" : wsById(_WS)?.name || "")}</div><h1 class="title">Approvals</h1></div></div>
    <div class="scroll"><div class="help">Saves your bots proposed and are waiting on. Approve sends "yes" in that thread; the bot then writes it. App actions (an email, a post, an event) run only when you approve them here or in the thread.</div><div class="rowc">${rows}</div></div>`;
  const find = id => items.find(a => a.id === id);
  $("#main").querySelectorAll("[data-ap-go]").forEach(b => b.onclick = () => window.ChatRooms?.openThread(find(b.dataset.apGo)));
  $("#main").querySelectorAll("[data-ap-ok]").forEach(b => b.onclick = () => window.ChatRooms?.openThread(find(b.dataset.apOk), "approved"));
  $("#main").querySelectorAll("[data-ap-no]").forEach(b => b.onclick = async () => {
    const a = find(b.dataset.apNo);
    if (isAppAction(a)) return;  // app actions answer only through their own card
    try { await jpost("/api/approvals/dismiss", { chat: a.chat }); await renderRail(); renderApprovals(); }
    catch (e) { toast("✗ " + e.message); }
  });
  $("#main").querySelectorAll("[data-app-id]").forEach(row =>
    wireAppCard(row.querySelector(".app-card"), find(row.dataset.appId)));
}

// ── Connected apps: held app actions (cards) and the Integrations panel.
function isAppAction(a){
  return !!a && a.kind === "app_action";
}
function grantLabel(scope){
  return scope === "write" ? "Read + write" : scope === "read" ? "Read" : "Off";
}
function appStatusLabel(app, connected){
  const n = connected === true ? 1 : +connected || 0;
  return n > 1 ? `${n} accounts` : n ? "Connected" : app && app.auth === "own_app" ? "Needs setup" : "Connect";
}
// The app's logo through this server (no outside image host), over its first letter.
function appLogoHtml(slug, meta){
  const m = meta || {};
  const letter = String(m.name || slug || "?").trim().charAt(0).toUpperCase() || "?";
  return `<span class="integ-logo" aria-hidden="true">${esc(letter)}${m.logo
    ? `<img src="/api/integrations/logo/${encodeURIComponent(slug)}" alt="" loading="lazy">` : ""}</span>`;
}
function acctPicked(picked, ws, slug, id){
  return ((picked?.[ws]?.[slug]) || []).includes(id);
}
function appCardHtml(a){
  const p = a.preview || { title: a.action?.slug || "App action", fields: [] };
  return `<div class="app-card-h"><b>${esc(p.title)}</b><small>Not run yet. It runs only when you approve.</small></div>
    <dl class="app-fields">${(p.fields || []).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <div class="app-btns"><button class="btn sm primary" data-app="approve">Approve</button>
      <button class="btn sm" data-app="edit">Edit</button>
      <button class="btn sm ghost" data-app="reject">No</button></div>`;
}
function wireAppCard(el, a){
  if (!el || !a) return;
  const lock = on => el.querySelectorAll("button").forEach(b => b.disabled = on);
  const answer = async (verb, body) => {
    lock(true);
    try {
      const r = await jpost(`/api/approvals/${encodeURIComponent(a.id)}/${verb}`, body || {});
      _APPROVALS = _APPROVALS.filter(x => x.id !== a.id);
      el.innerHTML = `<div class="app-result">${esc(r.status === "rejected" ? "Not sent." : r.result || r.status)}</div>`;
      renderRail().catch(() => {});
    } catch (e) { toast("✗ " + e.message); lock(false); }
  };
  el.querySelector('[data-app="approve"]').onclick = () => answer("approve");
  el.querySelector('[data-app="reject"]').onclick = () => answer("reject");
  el.querySelector('[data-app="edit"]').onclick = e => {
    const args = a.action?.arguments || {};
    const form = document.createElement("form");
    form.className = "app-edit";
    form.innerHTML = Object.entries(args).map(([k, v]) => {
      const field = typeof v !== "string"
        ? `<textarea data-k="${esc(k)}" data-json="1" rows="4">${esc(JSON.stringify(v, null, 2))}</textarea>`
        : v.length > 80 || v.includes("\n") ? `<textarea data-k="${esc(k)}" rows="6">${esc(v)}</textarea>`
        : `<input data-k="${esc(k)}" value="${esc(v)}">`;
      return `<label><span>${esc(k)}</span>${field}</label>`;
    }).join("") + `<button class="btn sm primary" type="submit">Approve edited</button>`;
    form.onsubmit = ev => {
      ev.preventDefault();
      const out = {};
      try { form.querySelectorAll("[data-k]").forEach(f => out[f.dataset.k] = f.dataset.json ? JSON.parse(f.value) : f.value); }
      catch { return toast("✗ One field is not valid JSON"); }
      answer("approve", { arguments: out });
    };
    el.querySelector(".app-fields").replaceWith(form);
    e.currentTarget.remove();
  };
}

// A person's own Mac as a runner: their messages run there, on their own login.
async function drawRunners(box, stillOn = () => true){
  const d = await api("/api/runners").catch(() => ({ runners: [] }));
  if (!stillOn() || !box.isConnected) return;
  box.innerHTML = `<section class="integ-group"><h3>Use your own Mac</h3>
    <div class="help">Connect your Mac and your messages here run on it, on your own Claude login. Bots there can only use Leo Bot: no files, no shell.</div>
    ${(d.runners || []).map(r => `<div class="post"><div class="t"><b>${esc(r.name)}</b><small>${r.online ? "online" : "offline"}</small></div><button class="more" data-runner-off="${esc(r.id)}">Remove</button></div>`).join("")}
    <button class="btn sm" data-runner-add>Connect a Mac</button><div class="runner-new"></div></section>`;
  box.querySelector("[data-runner-add]").onclick = async () => {
    try {
      const r = await jpost("/api/runners", { name: "My Mac" });
      const cmd = `(umask 077; printf %s '${r.token}' > ~/.leobot-runner-token); chmod 600 ~/.leobot-runner-token\npython3 -m dashboard.runner --server ${location.origin} --token-file ~/.leobot-runner-token`;
      await drawRunners(box, stillOn);
      box.querySelector(".runner-new").innerHTML = `<div class="help">On your Mac, inside a leo-bot checkout, with Claude Code logged in. This is shown once.</div><pre class="runner-cmd"></pre>`;
      box.querySelector(".runner-cmd").textContent = cmd;
    } catch (err) { toast("✗ " + err.message); }
  };
  box.querySelectorAll("[data-runner-off]").forEach(b => b.onclick = async () => {
    try { await jpost("/api/runners/revoke", { id: b.dataset.runnerOff }); drawRunners(box, stillOn); } catch (err) { toast("✗ " + err.message); }
  });
}

// Where this Leo Bot runs, and how to move it to a server of your own (docs/server.md).
function deploymentSection(where){
  const cmd = text => `<pre class="runner-cmd">${esc(text)}</pre>`;
  if (where === "server") return `<section class="integ-group"><h3>Where Leo Bot runs</h3>
    <div class="help">On your own server, so bots keep working when your laptop is closed. To update, run the setup script again from your laptop. To watch its log, run this on the server. Guide: docs/server.md.</div>
    ${cmd("journalctl -u leobot -f")}</section>`;
  return `<section class="integ-group"><h3>Where Leo Bot runs</h3>
    <div class="help">On this computer, so bots stop when it sleeps or quits. To keep them always on, run Leo Bot on a server of your own. Replace your-server, then run these from a leo-bot checkout here. They install Leo Bot, open it and copy your data across. The second keeps running until Ctrl-C: stop it before the third, then run it again to reopen Leo Bot. Bots on the server need a Claude login there or an API key. Guide: docs/server.md.</div>
    ${cmd(SERVER_STEPS.join("\n"))}</section>`;
}
const SERVER_STEPS = [
  `ssh root@your-server "LEOBOT_REPO=$(git remote get-url origin) bash -s" < scripts/server-setup.sh`,
  "scripts/leobot-connect.sh root@your-server",
  "scripts/sync-to-server.sh root@your-server",
];
// Simple original marks in each provider's colour, drawn inline so nothing loads from the network.
const ENGINE_MARKS = {
  claude: ["#D97757", `<g stroke="#fff" stroke-width="2.2" stroke-linecap="round"><path d="M12 4v16M4 12h16M6.3 6.3l11.4 11.4M17.7 6.3L6.3 17.7"/></g>`],
  anthropic: ["#C96442", `<path d="M7 18l5-12 5 12M9 14h6" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`],
  openai: ["#10A37F", `<g fill="none" stroke="#fff" stroke-width="1.8"><ellipse cx="12" cy="12" rx="3" ry="7.2"/><ellipse cx="12" cy="12" rx="3" ry="7.2" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="3" ry="7.2" transform="rotate(120 12 12)"/></g>`],
  openrouter: ["#6366F1", `<g fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h5c3 0 3 8 6 8h5M4 16h5c3 0 3-8 6-8h5"/><path d="M18 6l2 2-2 2M18 14l2 2-2 2"/></g>`],
  ollama: ["#3B3B3B", `<g fill="#fff"><circle cx="12" cy="13" r="5.5"/><path d="M7.6 9.5L7 4.5l3.2 3zM16.4 9.5L17 4.5l-3.2 3z"/></g><circle cx="10" cy="12.6" r="1" fill="#3B3B3B"/><circle cx="14" cy="12.6" r="1" fill="#3B3B3B"/>`],
  codex: ["linear-gradient(135deg,#8A8FFF,#3F3FE6)", `<path d="M7.5 18.5a4.2 4.2 0 0 1-.7-8.3 5.6 5.6 0 0 1 10.8-.9A4.7 4.7 0 0 1 17 18.5z" fill="#fff"/><path d="M9.6 11.2l2.4 2.2-2.4 2.2M13.4 15.7h2.6" fill="none" stroke="#4E52EE" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`],
  gemini: ["#4C7BF4", `<path d="M12 3c.6 5 4 8.4 9 9-5 .6-8.4 4-9 9-.6-5-4-8.4-9-9 5-.6 8.4-4 9-9z" fill="#fff"/>`],
  opencode: ["#2F6F6A", `<path d="M9 8l-4 4 4 4M15 8l4 4-4 4" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`],
  custom: ["#B08968", `<path d="M12 4l1.9 5.1L19 11l-5.1 1.9L12 18l-1.9-5.1L5 11l5.1-1.9z" fill="#fff"/>`],
};
function engineLogo(id, size){
  const key = id === "claude-code" ? "claude" : id === "codex" ? "codex" : String(id || "").replace(/^(api|acp):/, "");
  const [bg, glyph] = ENGINE_MARKS[key] || ENGINE_MARKS.custom;
  return `<span class="eng-logo" style="--s:${size || 40}px;background:${bg}"><svg viewBox="0 0 24 24" aria-hidden="true">${glyph}</svg></span>`;
}
// Settings: only what applies to the whole machine, across every workspace.
// What belongs to one project (Linked folders, Team) stays in that project.
const SETTINGS_TABS = [["engines", "Engines"], ["bots", "Bots"], ["integrations", "Integrations"], ["access", "Access"]];
const settingsTop = tab => `<div class="topbar settings-top"><div><h1 class="title">Settings</h1>
    <nav class="doc-tabs">${SETTINGS_TABS.map(([k, label]) => `<a href="#/settings/${k}"${k === tab ? ' class="on" aria-current="page"' : ""}>${label}</a>`).join("")}</nav></div></div>`;
function renderSettings(tab){
  return tab === "integrations" ? renderIntegrations() : tab === "access" ? renderAccess() : renderEngines(tab);
}

// Settings, Access: how people reach this Leo Bot, and where it runs.
async function renderAccess(){
  const stillOn = routeGuard();
  $("#main").innerHTML = `${settingsTop("access")}<div class="scroll eng-page" id="access"></div>`;
  const [share, eng] = await Promise.all([api("/api/sharing").catch(() => ({})), api("/api/engines").catch(() => ({}))]);
  if (!stillOn()) return;
  const box = $("#access");
  box.innerHTML = sharingSection(share.public_url || "") + deploymentSection(eng.deployment);
  const form = box.querySelector("#share-form");
  form.onsubmit = async e => {
    e.preventDefault();
    try { form.url.value = (await jpost("/api/sharing", { url: form.url.value })).public_url || ""; toast("Sharing address saved ✓"); }
    catch (err) { toast("✗ " + err.message); }
  };
}

// Settings, Engines: what can run the bots. Settings, Bots: who runs on what.
// Claude Code on this machine's login, or an API key. Invited people only ever run on an API key.
async function renderEngines(tab = "engines"){
  const stillOn = routeGuard();
  $("#main").innerHTML = `
    ${settingsTop(tab)}
    <div class="scroll eng-page"><div class="help">${tab === "bots"
      ? "Your own messages can use this machine's Claude Code login or an API key. Invited people's messages only ever run on an API key."
      : "Keys stay on this machine and no bot can read them."}</div><div id="eng"></div><div id="eng-runners"></div></div>`;
  let adding = null;
  const draw = async () => {
    const d = await api("/api/engines").catch(e => ({ error: e.message }));
    if (!stillOn()) return;
    const box = $("#eng");
    if (d.error) { box.innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
    const usable = (d.engines || []).filter(e => e.ok);
    const opts = (cur, list, blank) => `<option value="">${esc(blank)}</option>` + list.map(e =>
      `<option value="${esc(e.id)}"${e.id === cur ? " selected" : ""}>${esc(e.label)}</option>`).join("");
    const apiOnly = usable.filter(e => e.kind === "api");
    const rows = (d.engines || []).map(e => {
      const p = (d.providers || []).find(x => `api:${x.id}` === e.id);
      const models = p ? `<span class="eng-chip">fast · ${esc(p.models?.fast || "-")}</span><span class="eng-chip">strong · ${esc(p.models?.strong || "-")}</span>` : "";
      const note = e.folder_edit === false ? `<span class="eng-chip warm" title="Bots on this engine can read your linked folders but never change files in them. Leo Bot can't enforce its never-touch list (git, editor and secret files) on this engine's own tools.">Can read linked folders, can't edit them</span>` : "";
      return `<div class="eng-card">${engineLogo(e.id, 44)}
        <div class="eng-body"><div class="eng-name"><b>${esc(e.label)}</b><span class="eng-state ${e.ok ? "on" : "off"}">${e.ok ? "Ready" : "Not ready"}</span></div>
          ${e.detail ? `<div class="eng-detail">${esc(e.detail)}</div>` : ""}
          ${models || note ? `<div class="eng-chips">${models}${note}</div>` : ""}</div>
        <div class="eng-actions"><button class="btn sm" data-test="${esc(e.id)}">Test</button>
          ${p ? `<button class="btn sm" data-edit="${esc(p.id)}">Edit</button><button class="btn sm ghost" data-remove="${esc(p.id)}">Remove</button>` : ""}
          ${e.acp ? `<button class="btn sm ghost" data-remove-acp="${esc(e.id.slice(4))}">Remove</button>` : ""}</div></div>`;
    }).join("");
    const presets = Object.entries(d.presets || {}).filter(([k]) => !(d.providers || []).some(x => x.id === k));
    if (tab === "engines" && d.deployment === "server") drawRunners($("#eng-runners"), stillOn);
    const acpMine = (d.acp_agents || []).map(a => a.id);
    const acpAdd = d.deployment === "server" ? "" : Object.entries(d.acp_presets || {}).filter(([k]) => !acpMine.includes(k))
      .map(([k, v]) => `<button class="eng-add" data-add-acp="${esc(k)}">${engineLogo("acp:" + k, 22)}<span>${esc(v.name)}</span></button>`).join("");
    const form = adding === null ? `<div class="add-prov">${presets.map(([k, v]) => `<button class="eng-add" data-add="${esc(k)}">${engineLogo("api:" + k, 22)}<span>${esc(v.name)}</span></button>`).join("")}<button class="eng-add" data-add="custom">${engineLogo("custom", 22)}<span>Other (any OpenAI-compatible address)</span></button>${acpAdd}</div>`
      : (() => {
        const old = (d.providers || []).find(x => x.id === adding) || {};
        const pre = d.presets?.[adding] || {};
        const custom = adding === "custom";
        const f = { ...pre, ...old };
        return `<form class="integ-key eng-form">
          ${custom ? `<input name="id" placeholder="Short id (lowercase, e.g. deepseek)" aria-label="Short id" required>
          <input name="name" placeholder="Name" aria-label="Name">
          <select name="format" aria-label="Format"><option value="openai">OpenAI-compatible</option><option value="anthropic">Anthropic</option></select>
          <input name="base_url" placeholder="Base URL" aria-label="Base URL" required>` : `<b>${esc(f.name || adding)}</b>`}
          <input name="key" type="password" autocomplete="off" placeholder="${old.key_hint ? `Key ending ${esc(old.key_hint)} (leave empty to keep)` : f.no_key ? "No key needed" : "API key"}" aria-label="API key">
          <input name="fast" placeholder="Fast model" aria-label="Fast model" value="${esc(f.models?.fast || "")}">
          <input name="strong" placeholder="Strong model" aria-label="Strong model" value="${esc(f.models?.strong || "")}">
          <button class="btn sm primary" type="submit">Save</button><button class="btn sm ghost" type="button" data-cancel>Cancel</button></form>`;
      })();
    const wsCards = (d.bot_list || []).filter(g => g.bots.length).map(g => `<section class="eng-ws"><h4>${esc(g.ws_name)}</h4>
        ${g.ws === "hq" ? "" : `<div class="eng-choice eng-ws-all"><label><span>Whole workspace</span> <select data-choice="workspaces" data-scope="${esc(g.ws)}">${opts(d.workspaces?.[g.ws], usable, "Same as your messages")}</select></label></div>`}
        ${g.bots.map(b => `<div class="eng-choice"><label><span>${esc(b.icon || "")} ${esc(b.name)}</span>
          <select data-choice="bots" data-scope="${esc(g.ws)}/${esc(b.id)}">${opts(d.bots?.[`${g.ws}/${b.id}`], usable, g.ws === "hq" ? "Same as your messages" : "Same as its workspace")}</select></label></div>`).join("")}</section>`).join("");
    box.innerHTML = tab === "engines" ? `<section class="eng-intro"><h2>Pick the minds behind your team</h2>
        <p>Every bot can think with a different model. Connect a provider here, then choose who uses it in Bots.</p></section>
      <section class="integ-group"><h3>Your engines</h3><div class="eng-list">${rows}</div>
        <h3 class="eng-sub">Connect another</h3>${form}</section>` : `
      <section class="integ-group"><h3>Defaults</h3>
        <p class="eng-hint">Most specific wins: a bot's own pick, then its workspace, then your default.</p>
        <div class="eng-choice"><label><span>Your messages</span> <select data-choice="default">${opts(d.default, usable, "Automatic (Claude Code when installed)")}</select></label></div>
        <div class="eng-choice"><label><span>Invited people</span> <select data-choice="people">${opts(d.people, apiOnly, apiOnly.length ? "Off: bots don't answer them" : "Add an API key first")}</select></label>
          <small>Billed to that key. Also used in shared threads when your own engine can't run there.</small></div>
        <div class="eng-choice"><label><span>Terminal</span> <select data-choice="terminal">${["shell", "claude", "codex"].map(t => `<option value="${t}"${(d.terminal || "shell") === t ? " selected" : ""}>${t === "shell" ? "Your shell" : t}</option>`).join("")}</select></label></div>
      </section>
      <section class="integ-group"><h3>By workspace</h3><p class="eng-hint">Only bots switched on in that workspace. Switch them on or off in its Team.</p>${wsCards || `<div class="empty">No bots yet.</div>`}</section>`;
    box.querySelectorAll("[data-add]").forEach(b => b.onclick = () => { adding = b.dataset.add; draw(); });
    box.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => { adding = b.dataset.edit; draw(); });
    box.querySelector("[data-cancel]")?.addEventListener("click", () => { adding = null; draw(); });
    box.querySelectorAll("[data-remove]").forEach(b => b.onclick = async () => {
      if (!b.dataset.armed) { b.dataset.armed = "1"; b.textContent = "Click again to remove"; return; }
      try { await jpost("/api/engines/provider/remove", { id: b.dataset.remove }); draw(); } catch (err) { toast("✗ " + err.message); }
    });
    box.querySelectorAll("[data-add-acp]").forEach(b => b.onclick = async () => {
      try { await jpost("/api/engines/acp", { id: b.dataset.addAcp }); draw(); } catch (err) { toast("✗ " + err.message); }
    });
    box.querySelectorAll("[data-remove-acp]").forEach(b => b.onclick = async () => {
      try { await jpost("/api/engines/acp/remove", { id: b.dataset.removeAcp }); draw(); } catch (err) { toast("✗ " + err.message); }
    });
    box.querySelectorAll("[data-test]").forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = "Testing…";
      try {
        const r = await jpost("/api/engines/test", { id: b.dataset.test });
        toast(r.error ? "✗ " + r.error : "✓ Working");
      } catch (err) { toast("✗ " + err.message); }
      b.disabled = false; b.textContent = "Test";
    });
    box.querySelectorAll("[data-choice]").forEach(sel => sel.onchange = async () => {
      try { await jpost("/api/engines/choice", { name: sel.dataset.choice, value: sel.value, scope: sel.dataset.scope || null }); }
      catch (err) { toast("✗ " + err.message); draw(); }
    });
    const formEl = box.querySelector(".eng-form");
    if (formEl) formEl.onsubmit = async e => {
      e.preventDefault();
      const v = n => formEl.elements[n]?.value || "";
      const custom = adding === "custom";
      try {
        await jpost("/api/engines/provider", { id: custom ? v("id") : adding, key: v("key"),
          ...(custom ? { name: v("name"), format: v("format"), base_url: v("base_url"), no_key: !v("key") } : {}),
          models: { fast: v("fast"), strong: v("strong") } });
        adding = null; draw();
      } catch (err) { toast("✗ " + err.message); }
    };
  };
  draw();
}

async function renderIntegrations(){
  const stillOn = routeGuard();
  $("#main").innerHTML = `
    ${settingsTop("integrations")}
    <div class="scroll"><div class="help">Connect your own apps through your Composio account. Bots read freely; anything else waits for your Approve. Switch apps on per bot in Team.</div><div id="integ"></div></div>`;
  let polls = 0, replacing = false;
  // A logo that fails to load leaves its letter showing (error events do not bubble).
  $("#integ").addEventListener("error", e => { if (e.target.matches?.(".integ-logo img")) e.target.remove(); }, true);
  const draw = async () => {
    const d = await api("/api/integrations").catch(e => ({ error: e.message }));
    if (!stillOn()) return;
    const box = $("#integ");
    if (d.error) { box.innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
    const keyForm = `<form class="integ-key"><input name="k" type="password" autocomplete="off" placeholder="Composio project API key (ak_…)" aria-label="Composio project API key">
      <button class="btn sm primary" type="submit">Save</button>
      <a class="link-btn" href="https://docs.composio.dev/reference/authenticating-to-composio" target="_blank" rel="noopener">Where to find it</a>
      <small class="integ-key-hint">Composio dashboard, Platform, your project, Settings, API Keys. Not the consumer MCP key (ck_).</small></form>`;
    const keyRow = d.configured && !replacing
      ? `<div class="post integ-keyrow"><div class="t"><b>Composio</b><small>${d.key_refused ? `<span class="integ-bad">${esc(d.key_refused)}</span>` : `Configured · saved ${esc(new Date((d.saved || 0) * 1000).toLocaleDateString())}${d.offline ? " · can't reach Composio right now" : ""}`}</small></div>
          <button class="go" data-key="replace">Replace</button><button class="more" data-key="remove">Remove</button></div>`
      : keyForm;
    const accts = slug => (d.accounts || []).filter(a => a.toolkit === slug);
    const acctRow = (slug, a) => `<div class="integ-acct">
        <span class="integ-acct-name" data-rename="${esc(a.id)}" title="Rename" tabindex="0">${esc(a.label || a.id)}</span>
        <span class="integ-ws">${(d.workspaces || []).map(w => `<label><input type="checkbox" data-pick="${esc(a.id)}" data-ws="${esc(w.id)}"${acctPicked(d.picked, w.id, slug, a.id) ? " checked" : ""}> ${esc(w.name)}</label>`).join("")}</span>
        <button class="btn sm ghost integ-disc" data-disconnect="${esc(a.id)}">Disconnect</button></div>`;
    const appRow = (slug, meta) => {
      const m = meta || d.apps_meta?.[slug] || { slug, name: slug };
      const mine = accts(slug);
      const n = mine.length;
      return `<div class="integ-app"><div class="integ-row">${appLogoHtml(slug, m)}<span class="t"><b>${esc(m.name || slug)}</b>${slug === "google_maps" ? `<small>Google bills Maps per request.</small>` : ""}</span>
        ${n || m.auth === "own_app" ? `<span class="tag">${esc(appStatusLabel(m, n))}</span>` : ""}
        <button class="btn sm" data-connect="${esc(slug)}">${n ? "Add account" : m.auth === "own_app" ? "Set up" : "Connect"}</button></div>
        ${mine.map(a => acctRow(slug, a)).join("")}</div>`;
    };
    box.innerHTML = keyRow + (d.configured ? `
      <div class="help">Each account is connected once. Tick the workspaces that may use it: a project sees only the accounts ticked for it. Disconnect removes it from Composio and from every workspace.</div>
      ${(d.pinned || []).map(g => `<section class="integ-group"><h3>${esc(g.group)}</h3>${g.apps.map(s => appRow(s)).join("")}</section>`).join("")}
      <section class="integ-group"><h3>All apps</h3><input class="integ-q" type="search" placeholder="Search Composio apps" aria-label="Search Composio apps"><div class="integ-hits"></div></section>` : "");
    const keyEl = box.querySelector(".integ-key");
    if (keyEl) keyEl.onsubmit = async e => {
      e.preventDefault();
      try { await jpost("/api/integrations/key", { api_key: keyEl.k.value }); keyEl.k.value = ""; replacing = false; draw(); }
      catch (err) { toast("✗ " + err.message); }
    };
    box.querySelector('[data-key="replace"]')?.addEventListener("click", () => { replacing = true; draw(); });
    box.querySelector('[data-key="remove"]')?.addEventListener("click", async () => {
      try { await api("/api/integrations/key", { method: "DELETE" }); draw(); } catch (err) { toast("✗ " + err.message); }
    });
    const wireConnect = root => root.querySelectorAll("[data-connect]").forEach(b => b.onclick = async () => {
      try {
        const slug = b.dataset.connect, before = accts(slug).length;
        const res = await jpost("/api/integrations/connect", { toolkit: slug });
        const row = b.closest(".integ-row");
        if (res.needs_own_app) {
          const name = esc(row.querySelector(".t b")?.textContent || slug);
          row.parentElement.querySelector(".integ-setup")?.remove();
          row.insertAdjacentHTML("afterend", `<div class="integ-setup"><b>${name} needs your own developer app</b>
            <p>Composio has no shared login for ${name}, so you register one app, once.</p>
            <ol><li>On ${name}'s developer site, create an app and add this redirect URL:<code>${esc(res.redirect_uri)}</code></li>
              <li><a href="${esc(res.auth_configs_page)}" target="_blank" rel="noopener">Open Auth Configs in Composio</a>, create one for ${name} with your own credentials, and paste the client ID and secret.</li>
              <li>Come back and press Set up again.</li></ol></div>`);
          return;
        }
        const { url, account_id } = res;
        if (!/^https:\/\//i.test(String(url || ""))) throw new Error("the connect link is not a secure web address");
        window.open(url, "_blank", "noopener");
        b.textContent = "Waiting…"; b.disabled = true;
        // The Mac window can block a popup opened after a network wait: always offer a real link.
        b.insertAdjacentHTML("beforebegin", `<a class="link-btn" href="${esc(url)}" target="_blank" rel="noopener">Open sign-in page</a>`);
        polls = 0;
        const tick = async () => {
          if (!stillOn()) return;
          if (++polls > 100) return draw();  // 3 s × 100 = 5 min
          const s = await api("/api/integrations").catch(() => null);
          const now = (s?.accounts || []).filter(a => a.toolkit === slug);
          if (account_id ? now.some(a => a.id === account_id) : now.length > before) return draw();
          setTimeout(tick, 3000);
        };
        setTimeout(tick, 3000);
      } catch (err) { toast("✗ " + err.message); }
    });
    const wireAccounts = root => {
      // Two clicks: the first arms the button for a few seconds, the second disconnects.
      root.querySelectorAll("[data-disconnect]").forEach(b => b.onclick = async () => {
        if (!b.dataset.armed) {
          b.dataset.armed = "1"; b.textContent = "Click again to disconnect"; b.classList.add("danger");
          setTimeout(() => { if (b.isConnected && !b.disabled) { delete b.dataset.armed; b.textContent = "Disconnect"; b.classList.remove("danger"); } }, 4000);
          return;
        }
        b.disabled = true; b.textContent = "Disconnecting…";
        try {
          await jpost("/api/integrations/disconnect", { account: b.dataset.disconnect });
          draw();
        } catch (err) {
          toast("✗ " + err.message);
          b.disabled = false; delete b.dataset.armed; b.textContent = "Disconnect"; b.classList.remove("danger");
        }
      });
      root.querySelectorAll("[data-pick]").forEach(cb => cb.onchange = async () => {
        cb.disabled = true;
        try {
          const r = await jpost("/api/integrations/accounts", { ws: cb.dataset.ws, account: cb.dataset.pick, on: cb.checked });
          (d.picked ||= {})[r.ws] = r.picked;
        } catch (err) { cb.checked = !cb.checked; toast("✗ " + err.message); }
        cb.disabled = false;
      });
      root.querySelectorAll("[data-rename]").forEach(el => {
        const edit = () => {
          const id = el.dataset.rename, old = el.textContent;
          const input = document.createElement("input");
          input.className = "integ-acct-input";
          input.value = old; input.maxLength = 40; input.setAttribute("aria-label", "Account name");
          let done = false;
          const finish = async save => {
            if (done) return;
            done = true;
            const name = input.value.trim();
            if (save && name && name !== old) {
              try {
                const r = await jpost("/api/integrations/rename", { account: id, alias: name });
                const a = (d.accounts || []).find(x => x.id === id);
                if (a) a.label = r.label;
                el.textContent = r.label;
              } catch (err) { toast("✗ " + err.message); }
            }
            input.replaceWith(el);
          };
          input.onkeydown = e => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); };
          input.onblur = () => finish(true);
          el.replaceWith(input);
          input.focus(); input.select();
        };
        el.onclick = edit;
        el.onkeydown = e => { if (e.key === "Enter") edit(); };
      });
    };
    wireAccounts(box);
    wireConnect(box);
    const q = box.querySelector(".integ-q");
    if (q) {
      let timer;
      q.oninput = () => { clearTimeout(timer); timer = setTimeout(async () => {
        const r = await api(`/api/integrations/catalog?q=${encodeURIComponent(q.value)}`).catch(() => ({ apps: [] }));
        const hits = box.querySelector(".integ-hits");
        hits.innerHTML = r.apps.map(a => appRow(a.slug, a)).join("") || `<div class="empty">No app matches.</div>`;
        wireConnect(hits);
        wireAccounts(hits);
      }, 250); };
    }
  };
  draw();
}

async function toggleMemory(row, botId){
  const box = row.querySelector(".mem-box");
  if (!box.hidden) { box.hidden = true; return; }
  // A project workspace has two: your DMs' private one, and what the team's threads can see.
  const scopes = _WS === "hq" ? [["", "", "Add a fact this bot should remember here…"]] : [
    ["", "Private: your DMs and channels with no people. Nobody you invite sees this.", "Add a private fact…"],
    ["team", "Team: channels with people, and invited people's own DMs. Anyone there can get the bot to repeat this.", "Add a fact for the team…"],
  ];
  box.innerHTML = scopes.map((_, i) => `<div class="mem-scope" data-i="${i}"></div>`).join("");
  const draw = (el, [scope, head, hint], lines) => {
    el.innerHTML = `${head ? `<div class="mem-head">${esc(head)}</div>` : ""}${lines.length ? lines.map(l => `<div class="mem-line"><span>${esc(l.replace(/^- /, ""))}</span><button class="cl-del" data-mem-del="${esc(l)}" title="Forget">✕</button></div>`).join("") : `<div class="mem-empty">Nothing remembered here yet.</div>`}
      <form class="mem-add"><input name="text" placeholder="${esc(hint)}" maxlength="300"><button class="btn" type="submit">Add</button></form>`;
    const redraw = async (url, extra) => {
      try { draw(el, [scope, head, hint], (await jpost(url, { ws: _WS, bot: botId, scope, ...extra })).lines); } catch (e) { toast("✗ " + e.message); }
    };
    el.querySelectorAll("[data-mem-del]").forEach(b => b.onclick = () => redraw("/api/memory/delete", { line: b.dataset.memDel }));
    el.querySelector(".mem-add").onsubmit = e => {
      e.preventDefault();
      const text = e.target.text.value.trim(); if (!text) return;
      redraw("/api/memory", { text });
    };
  };
  box.hidden = false;
  await Promise.all(scopes.map(async (sc, i) => {
    try {
      const d = await api(`/api/memory?ws=${encodeURIComponent(_WS)}&bot=${encodeURIComponent(botId)}&scope=${encodeURIComponent(sc[0])}`);
      draw(box.querySelector(`[data-i="${i}"]`), sc, d.lines || []);
    } catch (e) { toast("✗ " + e.message); }
  }));
}


function latestMemo(project, type){
  return (project.memos || []).filter(m => m.type === type).sort((a, b) => b.version - a.version)[0] || null;
}

// Profile/channel tab ids (…pf1.sec00, …ch1.sec00) add no discrimination when the
// entity is already named in nav — drop only that suffix, never shared-prefix stripping.
const PROFILE_TAB_ID_RE = /^((?:pr\d+\.)+pf\d+)\.sec\d{2}$/;
const CHANNEL_TAB_ID_RE = /^((?:pr\d+\.)+pf\d+\.ch\d+)\.sec\d{2}$/;

function entityId(id){
  if (!id) return "";
  let m = id.match(PROFILE_TAB_ID_RE);
  if (m) return m[1];
  m = id.match(CHANNEL_TAB_ID_RE);
  if (m) return m[1];
  return id;
}

function idDisplay(id, opts={}){
  if (!id) return "";
  let s = opts.dropTabSuffix ? entityId(id) : id;
  // Tab subsections: show pr2.sec06.ss1 not pr2.sec06.doc1.ss1 (doc = file layer, redundant in UI).
  if (/\.doc\d+\.ss\d+/i.test(s)) s = s.replace(/\.doc\d+(\.ss\d+)/i, "$1");
  return s;
}

function sectionIdChip(id, opts=null){
  if (!id) return "";
  const o = (opts && typeof opts === "object") ? opts : {};
  const shown = idDisplay(id, o);
  if (!shown) return "";
  return `<span class="sec-id" role="button" tabindex="0" data-os-id="${esc(id)}" title="Copy @mention">${esc(shown)}</span>`;
}

function renderArtifactHead(title, id, trailing=""){
  const chip = sectionIdChip(id);
  if (!chip && !trailing) return `<h4>${esc(title)}</h4>`;
  return `<div class="sec-artifact-head"><h4>${esc(title)}</h4><div class="artifact-head-end">${chip}${trailing}</div></div>`;
}

function metaPlain(label, value){
  if(value==null||value==="") return "";
  return `<span class="meta-plain"><b>${esc(label)}:</b> ${esc(value)}</span>`;
}

function titledWithId(title, id=null, opts={}){
  const label = esc(title);
  if (!id) return label;
  const chip = sectionIdChip(id, {dropTabSuffix:true, ...opts});
  if (!chip) return label;
  return `${label} <span class="titled-id">${chip}</span>`;
}

function fallbackCopyText(text){
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta); ta.focus(); ta.select();
  let copied = false;
  try { copied = document.execCommand("copy") !== false; } catch { /* ignore */ }
  finally { document.body.removeChild(ta); }
  return copied;
}

function copyText(text, doneMessage){
  const done = () => toast(doneMessage);
  const failed = () => toast("Copy failed");
  const fallback = () => fallbackCopyText(text) ? done() : failed();
  if (navigator.clipboard && window.isSecureContext) {
    try { navigator.clipboard.writeText(text).then(done, fallback); }
    catch { fallback(); }
  } else { fallback(); }
}

function copyMentionId(id){
  const token = "@" + id;
  copyText(token, "Copied " + token);
}

function wireIdChips(){
  /* delegated — see boot() */
}

document.addEventListener("click", e => {
  const el = e.target.closest(".sec-id[data-os-id]");
  if (!el) return;
  e.preventDefault(); e.stopPropagation();
  copyMentionId(el.dataset.osId);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const el = e.target.closest(".sec-id[data-os-id]");
  if (!el) return;
  e.preventDefault();
  copyMentionId(el.dataset.osId);
});

function sectionEmptyState(sectionKey){
  const hints = sectionSkills(sectionKey).map(s => `/${s}`);
  const hint = hints.length
    ? `<p class="sec-hint">Fill via chat: ${hints.map(h => `<code>${esc(h)}</code>`).join(" ")}</p>`
    : "";
  return `<div class="sec-empty"><p>Nothing here yet.</p>${hint}</div>`;
}

/** Composed IDs only (pr2.sec04.mm1) — never show lookup keys (memo:proj:…) in UI. */
function composedIdOnly(...candidates){
  for (const id of candidates) {
    if (id && /^pr\d+\./.test(String(id))) return id;
  }
  return "";
}

function renderSecGroup(label, inner, id=null, actions=""){
  const chip = sectionIdChip(composedIdOnly(id));
  const act = actions ? `<div class="sec-group-actions">${actions}</div>` : "";
  const head = chip || act
    ? `<div class="sec-group-head"><h3>${esc(label)}</h3><div class="sec-group-end">${chip}${act}</div></div>`
    : `<h3>${esc(label)}</h3>`;
  return `<div class="sec-group">${head}${inner}</div>`;
}

function docArtifactId(projectSlug, artifact){
  const p = artifact?.path || "";
  let key = "";
  if (p.endsWith("strategy/intake.md")) key = "intake";
  else if (p.endsWith("technical.md")) key = "technical";
  else if (p.endsWith("project.md")) key = "project";
  return composedIdOnly(artifact?.id, key ? OSID.doc(projectSlug, key) : "");
}

function parseMdSections(text){
  if (!text) return [];
  const sections = [];
  let cur = null;
  for (const line of String(text).split("\n")) {
    if (line.startsWith("## ")) {
      if (cur) sections.push(cur);
      cur = { title: line.slice(3).trim(), body: "" };
    } else if (cur) {
      cur.body += (cur.body ? "\n" : "") + line;
    }
  }
  if (cur) sections.push(cur);
  return sections;
}

function projectDocSubsections(p, docKey){
  const fromProj = p?.subsections?.docs?.[docKey];
  if (fromProj && fromProj.length) return fromProj;
  const fromMeta = _SCHEMAS?.subsections?.docs?.[docKey]?.default_subsections;
  if (fromMeta && fromMeta.length) return fromMeta;
  const fromApi = _SCHEMAS?.[docKey]?.default_subsections || _SCHEMAS?.[docKey]?.sections;
  if (fromApi && fromApi.length) return fromApi;
  if (docKey === "intake") return ["What it is", "Stage & evidence", "Market", "Resources", "Goals", "Evidence log"];
  if (docKey === "technical") return ["Stack", "Architecture", "Infrastructure", "APIs & integrations", "Data & storage", "Deployment", "Open questions"];
  return ["Now", "Next", "Later / Ideas", "Shipped"];
}

function validationTabSubsections(p){
  const fromProj = p?.subsections?.validation_tab;
  if (fromProj && fromProj.length) return fromProj;
  const fromMeta = _SCHEMAS?.subsections?.validation_tab_default;
  if (fromMeta && fromMeta.length) return fromMeta;
  const fromApi = _SCHEMAS?.intake?.validation_tab_sections;
  if (fromApi && fromApi.length) return fromApi;
  return ["Stage & evidence", "Market", "Resources", "Goals", "Evidence log"];
}

function roadmapSectionOrder(p){
  const titles = p ? projectDocSubsections(p, "roadmap") : [];
  const order = titles.length
    ? titles.map(title => [roadmapSectionKey(title), title])
    : ROADMAP_SECTION_ORDER_FALLBACK;
  return order;
}

function docSubsectionId(projectSlug, p, docKey, title){
  const fromApi = p?.subsection_ids?.[docKey]?.[title];
  if (fromApi && /^pr\d+\./i.test(String(fromApi))) return String(fromApi).toLowerCase();
  return composedIdOnly(OSID.docSubsection(projectSlug, docKey, title));
}

function renderMdSubsections(projectSlug, p, docKey, text, opts = {}){
  const order = opts.titles || projectDocSubsections(p, docKey);
  const parsed = Object.fromEntries(parseMdSections(text || "").map(s => [s.title, s.body]));
  let html = "";
  order.forEach(title => {
    const body = (parsed[title] || "").trim();
    const inner = body ? formatMdDoc(body) : `<p class="memo-empty">Empty — click Edit to add content.</p>`;
    const subId = docSubsectionId(projectSlug, p, docKey, title);
    const titleKey = OSID.slugKey(title);
    const editBtn = opts.editable
      ? `<button type="button" class="btn sm" data-edit-doc-sub="${esc(docKey)}" data-sub-title="${esc(title)}" data-sub-key="${esc(titleKey)}">${icon("compose")} Edit</button>`
      : "";
    html += renderSecGroup(title, `<div class="pcard"><div class="sec-body memo-body">${inner}</div></div>`, subId, editBtn);
  });
  return html;
}

function wireDocSubsectionEditButtons(projectSlug){
  $("#main").querySelectorAll("[data-edit-doc-sub]").forEach(btn => {
    btn.onclick = () => navigate(
      `#/project/${projectSlug}/${btn.dataset.editDocSub}/subsection/edit/${btn.dataset.subKey}`,
      { subTitle: btn.dataset.subTitle },
    );
  });
}

function memoArtifactId(projectSlug, memo){
  return composedIdOnly(memo.id, OSID.memo(projectSlug, memo.type, memo.version));
}

function memoCardActions(slug, memo){
  if (!memo) return "";
  return `<button type="button" class="btn" data-edit-memo="${esc(slug)}" data-memo-type="${esc(memo.type)}" data-memo-version="${esc(memo.version)}" style="padding:4px 10px;font-size:11px">Edit</button>` +
         `<button type="button" class="btn danger-btn" data-del-memo="${esc(slug)}" data-memo-type="${esc(memo.type)}" data-memo-version="${esc(memo.version)}" style="padding:4px 10px;font-size:11px">Delete</button>`;
}

function wireMemoCardButtons(){
  $("#main").querySelectorAll("[data-edit-memo]").forEach(btn => {
    btn.onclick = () => navigate(`#/project/${btn.dataset.editMemo}/memo/${btn.dataset.memoType}/${btn.dataset.memoVersion}/edit`);
  });
  $("#main").querySelectorAll("[data-del-memo]").forEach(btn => {
    btn.onclick = () => renderConfirmDeleteMemo(btn.dataset.delMemo, btn.dataset.memoType, btn.dataset.memoVersion);
  });
}

function memoBackSection(memoType){
  return memoType === "problem-validation" ? "validation" : memoType === "assessment" ? "overview" : "pricing";
}

async function renderEditMemo(projectSlug, memoType, version){
  await ensureSchemas();
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const label = memoTypeLabel(memoType);
  const p = await api(`/api/project/${projectSlug}`);
  const memo = (p.memos || []).find(m => m.type === memoType && String(m.version) === String(version));
  if (!memo) { $("#main").innerHTML = `<div class="scroll"><p class="memo-empty">Memo not found.</p></div>`; return; }
  const fields = schemaFields("memo", memoType);
  const values = memo.body || {};
  const formHtml = fields.map(f => renderSchemaField(f, values)).join("");
  $("#main").innerHTML = `${pageHeader(`Edit · ${label}`, projName, `<button class="btn primary" id="em2-save">Save</button>`, memoArtifactId(projectSlug, memo))}
    <div class="scroll"><div class="fpage">${formHtml}</div></div>`;
  document.getElementById("em2-save").onclick = async () => {
    try {
      await jpost(`/api/project/${projectSlug}/memo/${memoType}/${version}/update`, formVals($("#main")));
      toast("Saved ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/${memoBackSection(memoType)}`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderConfirmDeleteMemo(projectSlug, memoType, version){
  const label = memoTypeLabel(memoType);
  confirmPage(`Delete ${label}`, `Delete this ${label.toLowerCase()} memo (v${version})? This cannot be undone.`, async () => {
    try {
      await jpost(`/api/project/${projectSlug}/memo/${memoType}/${version}/delete`, {});
      toast("Deleted ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/${memoBackSection(memoType)}`);
    } catch (e) { toast("✗ " + e.message); }
  });
}

const ROADMAP_SECTION_ORDER_FALLBACK = [
  ["now", "Now"],
  ["next", "Next"],
  ["later", "Later / Ideas"],
  ["shipped", "Shipped"],
];
function roadmapSectionKey(sec){
  const s = String(sec || "").toLowerCase();
  if (s.includes("now") || s.includes("building")) return "now";
  if (s.includes("next") || s.includes("planned")) return "next";
  if (s.includes("later") || s.includes("ideas")) return "later";
  if (s.includes("shipped")) return "shipped";
  return "other";
}

function featureStatusPill(status){
  const k = ({ shipped: "sched", building: "draft", planned: "idea", idea: "idea" })[status] || "draft";
  return `<span class="stp ${k}">${esc(status || "idea")}</span>`;
}

function renderMemoCard(projectSlug, memo, opts = {}){
  const b = memo.body || {};
  const title = opts.title || `${memoTypeLabel(memo.type)} v${memo.version}`;
  const id = memoArtifactId(projectSlug, memo);
  let body = renderMemoBody(b, memo.type || "");
  if (!body) body = `<p>${esc(memo.status || EMPTY)}</p>`;
  if (opts.headless) {
    return `<div class="pcard"><div class="sec-body memo-body">${body}</div></div>`;
  }
  return `<div class="pcard">${renderArtifactHead(title, id)}<div class="sec-body memo-body">${body}</div></div>`;
}

function renderOverviewSection(slug, p, sec){
  const e = p.entity || {};
  const pv = latestMemo(p, "problem-validation");
  const as = latestMemo(p, "assessment");
  const vb = pv && pv.body || {};
  const ab = as && as.body || {};
  const kv = (k, v) => `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`;
  let html = "";
  html += renderSecGroup("Project snapshot", `<div class="grid2">
    ${kv("Stage", e.status || "—")}${kv("Priority", e.priority || "—")}${kv("Hours/week", e.hours_per_week ?? "—")}
    ${kv("Validation", vb.validation_status || "—")}${kv("Pace", ab.pace_recommendation || "—")}
    ${kv("Profiles", (p.profiles || []).map(c => c.name).join(", ") || "—")}</div>`);
  html += renderSecGroup("GTM assessment",
    (as && ab.riskiest_assumption) ? renderMemoCard(slug, as, { title: "GTM assessment", headless: true })
      : `<div class="sec-missing">No assessment memo · <span class="sec-hint"><code>/gtm-assessment</code></span></div>`,
    (as && ab.riskiest_assumption) ? memoArtifactId(slug, as) : null,
    (as && ab.riskiest_assumption) ? memoCardActions(slug, as) : "");
  return html;
}

function renderValidationSection(slug, p, sec){
  const intake = (sec.artifacts || []).find(a => a.kind === "file" && (a.path || "").endsWith("strategy/intake.md"));
  const pv = latestMemo(p, "problem-validation");
  let html = "";
  html += intake
    ? `<div class="sec-subsections">${renderMdSubsections(slug, p, "intake", intake.text, { editable: true, titles: validationTabSubsections(p) })}</div>`
    : renderSecGroup("Venture intake", `<div class="sec-missing">No validation intake yet · <span class="sec-hint"><code>/venture-intake</code></span></div>`, docArtifactId(slug, intake));
  html += renderSecGroup("Problem validation",
    pv ? renderMemoCard(slug, pv, { headless: true }) : `<div class="sec-missing">No memo yet · <span class="sec-hint"><code>/problem-validation</code></span></div>`,
    pv ? memoArtifactId(slug, pv) : null,
    pv ? memoCardActions(slug, pv) : "");
  return html;
}

function renderTechnicalSection(slug, p, sec){
  const tech = (sec.artifacts || []).find(a => a.kind === "file" && (a.path || "").endsWith("technical.md"));
  if (!tech) return sectionEmptyState("technical");
  return `<div class="sec-subsections">${renderMdSubsections(slug, p, "technical", tech.text, { editable: true })}</div>`;
}

function renderExperimentCard(slug, x){
  const stem = x.stem || (x.file_path || "").split("/").pop().replace(/\.json$/, "") || x.id;
  const id = composedIdOnly(x.id, OSID.experiment(slug, stem));
  const b = x.body || {};
  let body = `<div><b>Status</b> · ${esc(x.status || "?")}`;
  if (x.decision) body += ` · decision <b>${esc(x.decision)}</b>`;
  if (x.duration_days) body += ` · ${esc(x.duration_days)}d`;
  if (x.started_on) body += ` · started ${esc(x.started_on)}`;
  body += `</div>`;
  if (x.result) body += `<div>${"<b>Result</b> · "}${esc(x.result)}</div>`;
  // success / kill lines → paired call-out blocks (mockup experiment record)
  if (b.success_criteria || b.kill_criteria){
    body += `<div class="crit-grid">`
      + (b.success_criteria ? `<div class="crit ok"><span class="crit-h">✓ Success looks like</span><p>${esc(b.success_criteria)}</p></div>` : "")
      + (b.kill_criteria ? `<div class="crit kill"><span class="crit-h">✕ Kill if</span><p>${esc(b.kill_criteria)}</p></div>` : "")
      + `</div>`;
  }
  return `<div class="pcard">${renderArtifactHead(x.assumption || stem, id, experimentCardActions(slug, stem))}<div class="sec-body">${body}</div></div>`;
}

function experimentCardActions(slug, stem){
  return `<button type="button" class="btn" data-edit-exp="${esc(slug)}" data-exp-stem="${esc(stem)}" style="padding:4px 10px;font-size:11px">Edit</button>` +
         `<button type="button" class="btn danger-btn" data-del-exp="${esc(slug)}" data-exp-stem="${esc(stem)}" style="padding:4px 10px;font-size:11px">Delete</button>`;
}

function wireExperimentCardButtons(){
  $("#main").querySelectorAll("[data-edit-exp]").forEach(btn => {
    btn.onclick = () => navigate(`#/project/${btn.dataset.editExp}/experiment/${btn.dataset.expStem}/edit`);
  });
  $("#main").querySelectorAll("[data-del-exp]").forEach(btn => {
    btn.onclick = () => renderConfirmDeleteExperiment(btn.dataset.delExp, btn.dataset.expStem);
  });
}

async function renderEditExperiment(projectSlug, stem){
  await ensureSchemas();
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const p = await api(`/api/project/${projectSlug}`);
  const exp = (p.experiments || []).find(x => (x.stem || "") === stem);
  if (!exp) { $("#main").innerHTML = `<div class="scroll"><p class="memo-empty">Experiment not found.</p></div>`; return; }
  const values = exp.body || {};
  const formHtml = schemaFields("experiment").map(f => renderSchemaField(f, values)).join("");
  $("#main").innerHTML = `${pageHeader("Edit experiment", projName, `<button class="btn primary" id="ee-save">Save</button>`, composedIdOnly(exp.id, OSID.experiment(projectSlug, stem)))}
    <div class="scroll"><div class="fpage">${formHtml}</div></div>`;
  document.getElementById("ee-save").onclick = async () => {
    const data = formVals($("#main"));
    if (!data.assumption?.trim()) return toast("Assumption is required");
    try {
      await jpost(`/api/project/${projectSlug}/experiment/${stem}/update`, data);
      toast("Saved ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/experiments`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderConfirmDeleteExperiment(projectSlug, stem){
  confirmPage("Delete experiment", "Delete this experiment? This cannot be undone.", async () => {
    try {
      await jpost(`/api/project/${projectSlug}/experiment/${stem}/delete`, {});
      toast("Deleted ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/experiments`);
    } catch (e) { toast("✗ " + e.message); }
  });
}

function renderExperimentsSection(slug, p, sec){
  const exps = p.experiments || [];
  if (!exps.length) return sectionEmptyState("experiments");
  const cards = exps.map(x => renderExperimentCard(slug, x)).join("");
  return renderSecGroup("Experiments", cards);
}

// ---- GTM (sec07): phases (time-boxed bets) → strategies (channel-motions) --- #
const GTM_STRAT_STATUS = { proposed:"proposed", active:"active", paused:"paused", killed:"killed" };

function projectChannels(slug){
  const p = (_TREE || []).find(x => x.slug === slug);
  const out = [];
  for (const prof of (p?.profiles || []))
    for (const c of (prof.channels || [])) out.push(c);
  return out;
}

function gtmPill(status){
  const s = esc(status || "");
  return s ? `<span class="prio-tag gtm-pill gtm-${s}">${s}</span>` : "";
}

function strategyCard(slug, st){
  const id = composedIdOnly(st.id);
  const star = st.primary ? `<span class="gtm-star" title="Primary channel-motion">★</span>` : "";
  const chan = st.channel_slug
    ? `<span class="meta-plain"><b>channel:</b> ${esc(st.channel_slug)}</span>` : "";
  const exps = (st.experiments || []).map(x =>
    `<button class="gtm-exp" data-gtm-exp="${esc(slug)}" title="${esc(x.assumption || x.stem)}">${icon("flask")} ${esc(x.stem)}${
      x.status ? ` · ${esc(x.status)}` : ""}</button>`).join("");
  const lens = exps
    ? `<div class="gtm-lens"><span class="gtm-lens-h">Experiments</span>${exps}</div>`
    : `<div class="gtm-lens gtm-lens-empty">No experiments tagged yet · <code>osctl create-experiment --strategy ${esc(st.stem)}</code></div>`;
  const acts = `<button class="btn" data-edit-strat="${esc(slug)}" data-strat-stem="${esc(st.stem)}" style="padding:4px 10px;font-size:11px">Edit</button>`
    + `<button class="btn danger-btn" data-del-strat="${esc(slug)}" data-strat-stem="${esc(st.stem)}" style="padding:4px 10px;font-size:11px">Delete</button>`;
  const head = `<div class="sec-artifact-head"><h4>${star}${esc(st.name)}</h4>`
    + `<div class="artifact-head-end">${gtmPill(st.status)}${sectionIdChip(id)}${acts}</div></div>`;
  const hyp = st.hypothesis ? `<div class="gtm-hyp">${esc(st.hypothesis)}</div>` : "";
  return `<div class="pcard gtm-strat">${head}<div class="sec-body">${chan}${hyp}${lens}</div></div>`;
}

function phaseGroup(slug, ph){
  const id = composedIdOnly(ph.id);
  const meta = [ph.timebox, ph.goal ? `goal: ${ph.goal}` : ""].filter(Boolean).join(" · ");
  const acts = `<button class="btn" data-edit-phase="${esc(slug)}" data-phase-id="${esc(ph.phase_id)}" style="padding:4px 10px;font-size:11px">Edit</button>`
    + `<button class="btn danger-btn" data-del-phase="${esc(slug)}" data-phase-id="${esc(ph.phase_id)}" style="padding:4px 10px;font-size:11px">Delete</button>`;
  const strategies = (ph.strategies || []).length
    ? ph.strategies.map(st => strategyCard(slug, st)).join("")
    : `<div class="sec-missing">No strategies in this phase yet · <span class="sec-hint"><code>/channel-strategy</code></span></div>`;
  const head = `<div class="sec-group-head gtm-phase-head"><h3>${esc(ph.name)}${
      meta ? `<span class="gtm-phase-meta">${esc(meta)}</span>` : ""}</h3>`
    + `<div class="sec-group-end">${gtmPill(ph.status)}${sectionIdChip(id)}${acts}</div></div>`;
  return `<div class="sec-group gtm-phase">${head}${strategies}</div>`;
}

function renderGtmSection(slug, p){
  const gtm = p.gtm || { phases: [], unphased: [] };
  const memoTypes = sectionMemoTypes("gtm");
  let memoHtml = "";
  memoTypes.forEach(type => {
    const m = latestMemo(p, type);
    if (m) memoHtml += renderSecGroup(memoTypeLabel(type), renderMemoCard(slug, m, { headless: true }),
      memoArtifactId(slug, m), memoCardActions(slug, m));
  });
  const hasAny = (gtm.phases || []).length || (gtm.unphased || []).length || memoHtml;
  if (!hasAny) return sectionEmptyState("gtm");
  let html = (gtm.phases || []).map(ph => phaseGroup(slug, ph)).join("");
  if ((gtm.unphased || []).length){
    const cards = gtm.unphased.map(st => strategyCard(slug, st)).join("");
    html += `<div class="sec-group gtm-phase"><div class="sec-group-head gtm-phase-head"><h3>Unphased strategies</h3></div>${cards}</div>`;
  }
  return html + memoHtml;
}

function wireGtmButtons(slug){
  const m = $("#main");
  m.querySelectorAll("[data-edit-phase]").forEach(b =>
    b.onclick = () => navigate(`#/project/${b.dataset.editPhase}/gtm/phase/${b.dataset.phaseId}/edit`));
  m.querySelectorAll("[data-del-phase]").forEach(b =>
    b.onclick = () => renderConfirmDeletePhase(b.dataset.delPhase, b.dataset.phaseId));
  m.querySelectorAll("[data-edit-strat]").forEach(b =>
    b.onclick = () => navigate(`#/project/${b.dataset.editStrat}/gtm/strategy/${b.dataset.stratStem}/edit`));
  m.querySelectorAll("[data-del-strat]").forEach(b =>
    b.onclick = () => renderConfirmDeleteStrategy(b.dataset.delStrat, b.dataset.stratStem));
  m.querySelectorAll("[data-gtm-exp]").forEach(b =>
    b.onclick = () => navigate(`#/project/${b.dataset.gtmExp}/experiments`));
}

const PRICING_SKILL_HINT = {
  positioning: "positioning",
  pricing: "pricing-strategy",
  competitors: "competitor-scan",
  icp: "icp-research",
  channels: "channel-strategy",
};

function renderPricingSection(slug, p){
  const types = sectionMemoTypes("pricing");
  let html = "";
  types.forEach(type => {
    const m = latestMemo(p, type);
    const label = memoTypeLabel(type);
    const skill = PRICING_SKILL_HINT[type] || type;
    const inner = m
      ? renderMemoCard(slug, m, { headless: true })
      : `<div class="sec-missing">No ${esc(label.toLowerCase())} memo yet · <span class="sec-hint"><code>/${esc(skill)}</code></span></div>`;
    html += renderSecGroup(label, inner, m ? memoArtifactId(slug, m) : null, m ? memoCardActions(slug, m) : "");
  });
  return html;
}

function renderFeatureCard(prod, f){
  const fid = composedIdOnly(f.id, OSID.feat(prod.slug, OSID.slugKey(f.title)));
  const badges = `<div class="artifact-badges">${featureStatusPill(f.status)}${
    f.priority ? `<span class="prio-tag">${esc(f.priority)}</span>` : ""}${
    f.target_date ? `<span class="prio-tag">${esc(f.target_date)}</span>` : ""}</div>`;
  const body = f.why
    ? `<div class="sec-body">${esc(f.why)}</div>`
    : `<div class="sec-body memo-empty">No description — add after title in roadmap: <code>Title — one-line why</code></div>`;
  return `<div class="pcard">${renderArtifactHead(f.title, fid, featureCardActions(prod.slug, f))}${badges}${body}</div>`;
}

function featureCardActions(productSlug, feature){
  const fid = OSID.slugKey(feature.title);
  return `<button type="button" class="btn" data-edit-feat="${esc(productSlug)}" data-feat-id="${esc(fid)}" style="padding:4px 10px;font-size:11px">Edit</button>` +
         `<button type="button" class="btn danger-btn" data-del-feat="${esc(productSlug)}" data-feat-id="${esc(fid)}" style="padding:4px 10px;font-size:11px">Delete</button>`;
}

function wireFeatureCardButtons(projectSlug){
  $("#main").querySelectorAll("[data-edit-feat]").forEach(btn => {
    btn.onclick = () => navigate(`#/product/${btn.dataset.editFeat}/feature/${btn.dataset.featId}/edit`, { projectSlug });
  });
  $("#main").querySelectorAll("[data-del-feat]").forEach(btn => {
    btn.onclick = () => renderConfirmDeleteFeature(btn.dataset.delFeat, btn.dataset.featId, projectSlug);
  });
}

async function renderEditFeature(productSlug, featureId, projectSlug){
  await ensureSchemas();
  const back = projectSlug ? `#/project/${projectSlug}/product` : "#/calendar";
  let fields = schemaFields("feature");
  let p = null;
  if (projectSlug) {
    try { p = await api(`/api/project/${projectSlug}`); if (p.feature?.length) fields = p.feature; } catch (_) { /* global schema defaults */ }
  }
  const feat = (p?.features || []).find(f => OSID.slugKey(f.title) === featureId && f.product_slug === productSlug);
  if (!feat) { $("#main").innerHTML = `<div class="scroll"><p class="memo-empty">Feature not found.</p></div>`; return; }
  const values = { title: feat.title, why: feat.why || "", section: feat.roadmap_section || "", priority: feat.priority || "" };
  const formHtml = fields.map(f => renderSchemaField(f, values)).join("");
  $("#main").innerHTML = `${pageHeader("Edit feature", productSlug, `<button class="btn primary" id="ef-save">Save</button>`, composedIdOnly(feat.id, OSID.feat(productSlug, featureId)))}
    <div class="scroll"><div class="fpage">${formHtml}</div></div>`;
  document.getElementById("ef-save").onclick = async () => {
    const data = formVals($("#main"));
    if (!data.title?.trim()) return toast("Title is required");
    try {
      await jpost(`/api/product/${productSlug}/feature/${featureId}/update`, data);
      toast("Saved ✓");
      await renderRail(); navigate(back);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderConfirmDeleteFeature(productSlug, featureId, projectSlug){
  const back = projectSlug ? `#/project/${projectSlug}/product` : "#/calendar";
  confirmPage("Delete feature", "Delete this feature from the roadmap? This cannot be undone.", async () => {
    try {
      await jpost(`/api/product/${productSlug}/feature/${featureId}/delete`, {});
      toast("Deleted ✓");
      await renderRail(); navigate(back);
    } catch (e) { toast("✗ " + e.message); }
  });
}

function renderProductSection(slug, p){
  let html = "";
  const products = p.products || [];
  if (!products.length) return html + sectionEmptyState("product");
  products.forEach(prod => {
    const feats = (p.features || []).filter(f => f.product_slug === prod.slug);
    const prodId = composedIdOnly(OSID.prod(prod.slug));
    const addBtn = `<button class="btn sm" type="button" data-add-feat="${esc(prod.slug)}">${icon("plus")} Feature</button>`;
    const sectionOrder = roadmapSectionOrder(p);
    const buckets = Object.fromEntries(sectionOrder.map(([k]) => [k, []]));
    buckets.other = [];
    feats.forEach(f => {
      const k = roadmapSectionKey(f.roadmap_section);
      (buckets[k] || buckets.other).push(f);
    });
    let inner = `<p class="help">${feats.length} feature${feats.length !== 1 ? "s" : ""} in roadmap</p>`;
    sectionOrder.forEach(([key, label]) => {
      const group = buckets[key] || [];
      if (!group.length) return;
      inner += renderSecGroup(label, `<div class="rowc">${group.map(f => renderFeatureCard(prod, f)).join("")}</div>`);
    });
    if (buckets.other.length) {
      inner += renderSecGroup("Roadmap", `<div class="rowc">${buckets.other.map(f => renderFeatureCard(prod, f)).join("")}</div>`);
    }
    if (!feats.length) inner += `<div class="sec-missing">No features yet.</div>`;
    html += `<div class="pcard" style="margin-bottom:14px">${renderArtifactHead(prod.name, prodId, addBtn)}${inner}</div>`;
  });
  return html;
}

function wireProductFeatureButtons(slug){
  $("#main").querySelectorAll("[data-add-feat]").forEach(btn => {
    btn.onclick = () => navigate(`#/product/${btn.dataset.addFeat}/feature/new`, { projectSlug: slug });
  });
}

function hasProjectIntake(p){
  return ((p.sections || {}).validation?.artifacts || []).some(
    a => a.kind === "file" && (a.path || "").endsWith("strategy/intake.md"));
}

function hasProjectTechnical(p){
  return ((p.sections || {}).technical?.artifacts || []).some(
    a => a.kind === "file" && (a.path || "").endsWith("technical.md"));
}

function sectionAddButtons(slug, section, p){
  const out = [];
  if (section === "validation") {
    if (!hasProjectIntake(p)) out.push({ label: "Intake", route: `#/project/${slug}/intake/new` });
    out.push({ label: "Memo", route: `#/project/${slug}/memo/new/problem-validation` });
  } else if (section === "experiments") {
    out.push({ label: "Experiment", route: `#/project/${slug}/experiment/new` });
  } else if (section === "pricing") {
    out.push({ label: "Memo", route: `#/project/${slug}/memo/new/positioning`, pickType: true });
  } else if (section === "product") {
    out.push({ label: "Product", route: `#/project/${slug}/product/new` });
    if ((p.products || []).length === 1)
      out.push({ label: "Feature", route: `#/product/${p.products[0].slug}/feature/new`, projectSlug: slug });
    else if ((p.products || []).length > 1)
      out.push({ label: "Feature", route: `#/project/${slug}/product`, pickProduct: true });
  } else if (section === "technical") {
    if (!hasProjectTechnical(p)) out.push({ label: "Technical", route: `#/project/${slug}/technical/new` });
    else out.push({ label: "Subsection", route: `#/project/${slug}/technical/subsection/new` });
  } else if (section === "gtm") {
    out.push({ label: "Memo", route: `#/project/${slug}/memo/new/channels`, pickType: true });
    out.push({ label: "Phase", route: `#/project/${slug}/gtm/phase/new` });
    out.push({ label: "Strategy", route: `#/project/${slug}/gtm/strategy/new` });
  }
  return out;
}

async function renderProjectSection(slug, section){
  const stillOn = routeGuard();
  const [p] = await Promise.all([api(`/api/project/${slug}`), refreshIdRegistry()]);
  if (!stillOn()) return;
  const title = (SECTIONS.find(s => s.key === section) || {}).label || section;
  const sec = (p.sections || {})[section] || {};
  const renderers = {
    overview: () => renderOverviewSection(slug, p, sec),
    validation: () => renderValidationSection(slug, p, sec),
    experiments: () => renderExperimentsSection(slug, p, sec),
    pricing: () => renderPricingSection(slug, p),
    product: () => renderProductSection(slug, p),
    technical: () => renderTechnicalSection(slug, p, sec),
    gtm: () => renderGtmSection(slug, p),
  };
  const body = (renderers[section] || (() => `<div class="empty">Unknown section.</div>`))();
  const adds = sectionAddButtons(slug, section, p);
  const addHtml = adds.map((a, i) =>
    `<button class="btn${i === adds.length - 1 ? " primary" : ""}" data-sec-add="${i}">${icon("plus")} ${esc(a.label)}</button>`
  ).join("");
  const tabId = composedIdOnly(OSID.tabProj(slug, section));
  $("#main").innerHTML = `<div class="topbar"><div><div class="crumbs">${esc(p.entity.name)} · <b>${esc(title)}</b></div>
      <div class="sec-meta" style="margin-top:6px"><h1 class="title" style="margin:0">${esc(title)}</h1>${sectionIdChip(tabId)}</div></div>
    ${addHtml ? `<div style="margin-left:auto;display:flex;gap:8px">${addHtml}</div>` : ""}</div>
    <div class="scroll">${body}</div>`;
  adds.forEach((a, i) => {
    const btn = $("#main").querySelector(`[data-sec-add="${i}"]`);
    if (!btn) return;
    btn.onclick = () => {
      if (a.pickType) {
        const types = sectionMemoTypes(section);
        const pick = prompt(`Memo type:\n${types.map((t, n) => `${n + 1}. ${memoTypeLabel(t)}`).join("\n")}\n\nEnter number or type slug:`);
        if (!pick) return;
        const n = parseInt(pick, 10);
        const mtype = (!isNaN(n) && n >= 1 && n <= types.length) ? types[n - 1] : pick.trim();
        if (!types.includes(mtype)) return toast("Unknown memo type");
        navigate(`#/project/${slug}/memo/new/${mtype}`);
        return;
      }
      if (a.pickProduct) {
        const prods = p.products || [];
        const pick = prompt(prods.map((pr, n) => `${n + 1}. ${pr.name}`).join("\n") + "\n\nProduct number:");
        const n = parseInt(pick, 10);
        if (isNaN(n) || n < 1 || n > prods.length) return;
        navigate(`#/product/${prods[n - 1].slug}/feature/new`, { projectSlug: slug });
        return;
      }
      navigate(a.route, a.projectSlug ? { projectSlug: a.projectSlug } : {});
    };
  });
  if (section === "product") wireProductFeatureButtons(slug);
  if (section === "product") wireFeatureCardButtons(slug);
  if (section === "technical" || section === "validation") wireDocSubsectionEditButtons(slug);
  if (section === "experiments") wireExperimentCardButtons();
  if (section === "gtm") wireGtmButtons(slug);
  if (["overview", "validation", "pricing", "gtm"].includes(section)) wireMemoCardButtons();
  wireIdChips($("#main"));
}

let CAL = (function(){ const d=new Date(); return {y:d.getFullYear(), m:d.getMonth(), filter:"all", mode:"month"}; })();
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
function calShift(delta){ let m=CAL.m+delta,y=CAL.y; if(m<0){m=11;y--;} if(m>11){m=0;y++;} CAL={...CAL,y,m}; }
function calToday(){ const d=new Date(); CAL={...CAL,y:d.getFullYear(),m:d.getMonth()}; }

function evDone(r){
  return (r.kind==="post"&&r.status==="published")||(r.kind==="activity"&&r.status==="done");
}

function evDetail(r){
  // Returns an .ev-detail element for inline expansion in calendar cells and ops list.
  const d=document.createElement("div"); d.className="ev-detail"; d.dataset.forEv=JSON.stringify(r);
  const meta=[r.kind, r.entity_name||r.entity_slug, r.date, r.status].filter(Boolean).join(" · ");
  let acts="";
  if(r.kind==="activity"&&r.status!=="done")
    acts+=`<button class="btn primary" data-ev-done>Mark done ✓</button>`;
  if(r.kind==="post")
    acts+=`<button class="btn primary" data-ev-go>Open post →</button>`;
  if(r.kind==="activity")
    acts+=`<button class="btn danger-btn" data-ev-del>Delete</button>`;
  if(r.kind==="milestone"&&r.ref_id){
    acts+=`<button class="btn primary" data-ev-edit>Edit</button>`;
    acts+=`<button class="btn danger-btn" data-ev-del>Delete</button>`;
  }
  d.innerHTML=`<div class="ev-meta">${esc(meta)}</div><div class="ev-acts">${acts}</div>`;
  const doneBtn=d.querySelector("[data-ev-done]");
  if(doneBtn) doneBtn.onclick=async()=>{
    try{ await jpost("/api/activity/done",{title:r.title,entity:r.entity_slug}); toast("Marked done ✓"); refreshViews(); }
    catch(e){ toast("✗ "+e.message); }
  };
  const goBtn=d.querySelector("[data-ev-go]");
  if(goBtn) goBtn.onclick=()=>navigate(`#/post/${r.ref_id}`,{profileSlug:r.entity_slug});
  const delBtn=d.querySelector("[data-ev-del]");
  if(delBtn) delBtn.onclick=async()=>{
    d.remove();
    if(r.kind==="activity"){
      try{
        await jpost("/api/activity/delete",{title:r.title,entity:r.entity_slug||""});
        undoToast(`Activity "${r.title}" deleted`, async()=>{
          await jpost("/api/activity/new",{title:r.title,entity:r.entity_slug,date:r.date,time:r.time||"",time_end:r.time_end||"",type:r.type||"task"});
          refreshViews();
        });
        refreshViews();
      }catch(e){ toast("✗ "+e.message); }
    } else if(r.kind==="milestone"&&r.ref_id){
      try{
        await jpost(`/api/milestone/${r.ref_id}/delete`,{});
        undoToast(`Milestone "${r.title}" deleted`, async()=>{
          await jpost("/api/milestone/new",{title:r.title,date:r.date,entity:r.entity_slug,notes:r.notes||""});
          refreshViews();
        });
        refreshViews();
      }catch(e){ toast("✗ "+e.message); }
    }
  };
  const editBtn=d.querySelector("[data-ev-edit]");
  if(editBtn) editBtn.onclick=()=>navigate(`#/milestone/${r.ref_id}/edit`,{title:r.title,date:r.date,date_end:r.date_end||""});
  return d;
}
function toggleEvDetail(el, r){
  const existing=el.parentNode.querySelector(".ev-detail");
  if(existing&&existing.dataset.forEv===JSON.stringify(r)){ existing.remove(); return; }
  if(existing) existing.remove();
  el.parentNode.insertBefore(evDetail(r), el.nextSibling);
}

// The calendar across every workspace, or (Content view's Calendar tab) only
// the rows of `scope` (entity slugs) under the given `head`.
async function renderTimeline({ scope = null, head = null } = {}){
  const again = () => renderTimeline({ scope, head });
  const stillOn = routeGuard();
  const all = (await api("/api/timeline")).filter(r => !scope || scope.has(r.entity_slug));
  if (!stillOn()) return;
  const kinds=["post","activity","milestone","experiment","feature"];
  const filtered = CAL.filter==="all" ? all : all.filter(r=>r.kind===CAL.filter);
  const byDay={}; filtered.forEach(r=>{ if(r.date){ (byDay[r.date]=byDay[r.date]||[]).push(r); } });
  const iso=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  const today=iso(new Date());
  const first=new Date(CAL.y,CAL.m,1), lead=(first.getDay()+6)%7, start=new Date(CAL.y,CAL.m,1-lead);
  let cells="";
  for(let i=0;i<42;i++){
    const d=new Date(start.getFullYear(),start.getMonth(),start.getDate()+i), k=iso(d), out=d.getMonth()!==CAL.m;
    const evs=(byDay[k]||[]).map(r=>{
      const cls = r.kind==="post" ? `ev post-${STATUS_PILL_CLASS[r.status]||"idea"}` : `ev k-${esc(r.kind)}`;
      const title=r.title||r.kind, mark=kindIcon(r.kind), label=`${KIND_NAME[r.kind]||r.kind}: ${r.time?r.time+" ":""}${title}`;
      return `<div class="${cls}" role="button" tabindex="0" aria-label="${esc(label)}" title="${esc((r.time?`${r.time}-${r.time_end||""} `:"")+title)}" data-ev='${esc(JSON.stringify({...r,title:title.slice(0,80)}))}'><span class="ev-mark" aria-hidden="true">${mark}</span><span class="ev-title">${esc(title)}</span></div>`;
    }).join("");
    cells+=`<div class="day${out?" out":""}${k===today?" today":""}" data-day="${k}" title="Double-click to open this day"><div class="n">${d.getDate()}</div>${evs}</div>`;
    if(i>=34&&d.getMonth()!==CAL.m&&(i+1)%7===0) break;
  }
  const counts=k=>all.filter(r=>r.kind===k).length;
  const kindLabels={post:"Posts",activity:"Activities",milestone:"Milestones",experiment:"Experiments",feature:"Features"};
  const chips=[["all","All",all.length],...kinds.map(k=>[k,kindLabels[k]||k,counts(k)])]
    .map(([k,l,n])=>`<button type="button" class="kchip k-${k}${CAL.filter===k?" on":""}" data-kf="${k}" aria-pressed="${CAL.filter===k}">${l} <span style="opacity:.6">${n}</span></button>`).join("");
  const dow=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(x=>`<div class="dow">${x}</div>`).join("");
  const projectOpts = _TREE.map(p=>`<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join("");
  // agenda (list) view — items grouped by week, mockup Timeline style
  const dated = filtered.filter(r=>r.date).sort((a,b)=>a.date.localeCompare(b.date));
  const wkKey=ds=>{ const d=new Date(ds+"T00:00:00"); const off=(d.getDay()+6)%7; d.setDate(d.getDate()-off); return iso(d); };
  const wkLabel=mk=>{ const mo=new Date(mk+"T00:00:00"); const su=new Date(mo); su.setDate(mo.getDate()+6);
    const f=x=>x.toLocaleDateString(undefined,{month:"short",day:"numeric"}); return `${f(mo)} – ${f(su)}`; };
  const dayLabel=ds=>new Date(ds+"T00:00:00").toLocaleDateString(undefined,{weekday:"long",month:"short",day:"numeric"});
  const grp={}; dated.forEach(r=>{ (grp[r.date]=grp[r.date]||[]).push(r); });
  const listHtml = Object.keys(grp).sort().map(dk=>{
    const rws = grp[dk].map(r=>{
      return `<div class="post postcard calcard" data-ev='${esc(JSON.stringify({...r,title:(r.title||"").slice(0,80)}))}'>
        <div class="pc-top"><span class="plat tone-${esc(r.kind)}">${esc(KIND_NAME[r.kind]||r.kind)}</span>${r.status?`<span class="when">${esc(r.status)}</span>`:""}</div>
        <div class="t">${r.time?`<span class="cal-time">${esc(r.time)}-${esc(r.time_end||"")}</span>`:""}${esc(r.title||r.kind)}</div>
        <div class="pc-sub">${esc(r.entity_name||r.kind)}</div></div>`;
    }).join("");
    return `<div class="wk">${esc(dayLabel(dk))}</div><div class="cards postcards calcards">${rws}</div>`;
  }).join("") || `<div class="sec-empty"><p>Nothing scheduled yet.</p></div>`;
  const isList = CAL.mode==="list";
  const seg = `<div class="seg" style="margin:0"><button type="button" class="${isList?"":"on"}" data-mode="month" aria-pressed="${!isList}">Month</button><button type="button" class="${isList?"on":""}" data-mode="list" aria-pressed="${isList}">List</button></div>`;
  const tools = `${seg}<button class="btn" id="newActBtn">${icon("plus")} Activity</button><button class="btn" id="newMsBtn">${icon("plus")} Milestone</button>`;
  const body = `<div class="cal-filters">${chips}</div>
      ${CAL_LEGEND}
      ${isList ? listHtml : `<div class="cal-head"><span class="mlabel">${MONTHS[CAL.m]} ${CAL.y}</span>
        <div class="cal-nav"><button id="cprev" aria-label="Previous month">‹</button><button id="ctoday">Today</button><button id="cnext" aria-label="Next month">›</button></div></div>
      <div class="cal">${dow}${cells}</div>`}`;
  $("#main").innerHTML = head
    ? `<div class="scroll"><div class="doc">${head}<div class="cal-tools">${tools}</div>${body}</div></div>`
    : `<div class="topbar"><div><div class="crumbs">Across everything</div><h1 class="title">Calendar</h1></div>
      <div class="topbar-end">${tools}</div></div>
    <div class="scroll">${body}</div>`;
  $("#main").querySelectorAll("[data-mode]").forEach(s=>s.onclick=()=>{ CAL.mode=s.dataset.mode; again(); });
  if(!isList){ $("#cprev").onclick=()=>{ calShift(-1); again(); }; $("#cnext").onclick=()=>{ calShift(1); again(); }; $("#ctoday").onclick=()=>{ calToday(); again(); }; }
  $("#main").querySelectorAll(".kchip").forEach(c=>c.onclick=()=>{ CAL.filter=c.dataset.kf; again(); });
  $("#main").querySelectorAll("[data-ev]").forEach(el=>el.onclick=e=>{ e.stopPropagation(); try{showEventPopup(JSON.parse(el.dataset.ev));}catch(_){} });
  $("#main").querySelectorAll("[data-ev]").forEach(el=>el.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); el.click(); } });
  $("#main").querySelectorAll(".day[data-day]").forEach(el=>el.ondblclick=()=>navigate(`#/day/${el.dataset.day}`));
  $("#newActBtn").onclick=()=>navigate("#/activity/new",{date:iso(new Date()),from:"calendar"});
  $("#newMsBtn").onclick=()=>navigate("#/milestone/new");
}

// Pipeline rules (buckets, pills, labels, next step) live in content-view.js.
const { STAGE_GROUP, STATUS_PILL: STATUS_PILL_CLASS, statusLabel: plainStatus, nextActionFor } = ContentView;

// Carry out a post's next step. Review and Schedule only open the post (or its
// date editor); the rest change it on the server and resolve true so the
// caller re-renders.
async function runNextAction(post, profileSlug){
  const na = nextActionFor(post); if (!na) return false;
  const id = post.id;
  if (na.kind === "review") { navigate(`#/post/${id}`, {profileSlug}); return false; }
  if (na.kind === "schedule") { navigate(`#/post/${id}/edit`, {profileSlug}); return false; }
  try {
    if (na.kind === "brief") {
      toast("Writing via claude -p… (a few seconds)", true);
      await api(postUrl(id, profileSlug, "/brief"), {method:"POST"});
      toast("Draft ready ✓");
    } else {
      const status = na.kind === "publish" ? "published" : "planned";
      await jpost(postUrl(id, profileSlug, "/status"), {status});
      toast("✓ " + plainStatus(status));
    }
    return true;
  } catch (e) { toast("✗ " + e.message); return false; }
}

const PLATFORM_ICON = {instagram:"instagram",tiktok:"tiktok",x:"x-brand",linkedin:"linkedin",youtube:"youtube",facebook:"facebook"};
const platIcon = platform => icon(PLATFORM_ICON[platform] || "hash");
const PLAT_NAME = {instagram:"Instagram",tiktok:"TikTok",x:"X",linkedin:"LinkedIn",youtube:"YouTube",facebook:"Facebook"};
const KIND_NAME = {post:"Post",experiment:"Experiment",milestone:"Milestone",activity:"Activity",feature:"Feature"};
const KIND_ICON = {post:"doc",experiment:"flask",milestone:"flag",activity:"clock",feature:"layers"};
const kindIcon = kind => icon(KIND_ICON[kind] || "doc");
// one calendar/day card, shared by the List view and the per-day view
function calCard(r){
  const isPost = r.kind==="post";
  const tone = isPost ? STATUS_PILL_CLASS[r.status]||"idea" : r.kind;
  const label = isPost ? plainStatus(r.status) : (KIND_NAME[r.kind]||r.kind);
  return `<div class="post postcard calcard" data-ev='${esc(JSON.stringify({...r,title:(r.title||"").slice(0,80)}))}'>
    <div class="pc-top"><span class="plat tone-${esc(tone)}">${esc(label)}</span></div>
    <div class="t">${r.time?`<span class="cal-time">${esc(r.time)}-${esc(r.time_end||"")}</span>`:""}${esc(r.title||r.kind)}</div>
    <div class="pc-sub">${esc(r.entity_name||r.kind)}</div></div>`;
}
const CAL_LEGEND = `<div class="cal-legend">
  <span class="lg-grp">Posts</span>
  <span class="lg"><i class="tone-idea"></i>Idea</span>
  <span class="lg"><i class="tone-draft"></i>Draft</span>
  <span class="lg"><i class="tone-ready"></i>Ready</span>
  <span class="lg"><i class="tone-published"></i>Published</span>
  <span class="lg-sep"></span>
  <span class="lg-grp">Type</span>
  <span class="lg"><i class="tone-experiment"></i>Experiment</span>
  <span class="lg"><i class="tone-milestone"></i>Milestone</span>
  <span class="lg"><i class="tone-activity"></i>Activity</span>
  <span class="lg"><i class="tone-feature"></i>Feature</span>
</div>`;
// full-screen list of a single day's scheduled items (double-click a calendar day).
// Rows navigate straight to the item — no intermediate detail card.
function dayItemGo(r){
  if(r.kind==="post"&&r.ref_id) return ()=>navigate(`#/post/${r.ref_id}`,{profileSlug:r.entity_slug});
  if(r.kind==="experiment") return ()=>navigate(`#/project/${r.entity_slug}/experiments`);
  if(r.kind==="feature") return ()=>navigate(`#/project/${r.entity_slug}/product`);
  if(r.kind==="activity") return ()=>navigate("#/operations");
  return ()=>navigate("#/calendar");
}
// clicking a calendar event opens a popup sheet (not an inline card)
function showEventPopup(r){
  const openLabel = r.kind==="post" ? "Open post →" : r.kind==="experiment" ? "Open experiment →"
    : r.kind==="feature" ? "Open roadmap →" : r.kind==="activity" ? "Open operations →" : null;
  const bg=document.createElement("div"); bg.className="popup-bg";
  bg.innerHTML=`<div class="popup" id="ev-popup">
    <button class="popup-close" data-close>×</button>
    <h3>${esc(r.title||r.kind)}</h3>
    <div class="meta">
      <span class="mk">Kind</span><span class="mv">${esc(KIND_NAME[r.kind]||r.kind)}</span>
      ${r.entity_name?`<span class="mk">Where</span><span class="mv">${esc(r.entity_name)}</span>`:""}
      ${r.date?`<span class="mk">Date</span><span class="mv">${esc(r.date)}</span>`:""}
      ${r.status?`<span class="mk">Status</span><span class="mv">${esc(r.status)}</span>`:""}
    </div>
    <div class="pacts">${openLabel?`<button class="btn primary" data-go>${openLabel}</button>`:""}<button class="btn" data-close2>Close</button></div>
  </div>`;
  document.body.appendChild(bg);
  const close=()=>bg.remove();
  bg.onclick=e=>{ if(e.target===bg) close(); };
  bg.querySelector("#ev-popup").onclick=e=>e.stopPropagation();
  bg.querySelector("[data-close]").onclick=close;
  bg.querySelector("[data-close2]").onclick=close;
  const go=bg.querySelector("[data-go]");
  if(go){ const nav=dayItemGo(r); go.onclick=()=>{ close(); nav(); }; }
  document.addEventListener("keydown", function esc(e){ if(e.key==="Escape"){ close(); document.removeEventListener("keydown",esc); } });
}
async function renderDay(dateStr){
  const stillOn = routeGuard();
  const all = await api("/api/timeline").catch(()=>[]);
  if (!stillOn()) return;
  const items = all.filter(r=>r.date===dateStr).sort((a,b)=>(a.kind||"").localeCompare(b.kind||""));
  const label = new Date(dateStr+"T00:00:00").toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric",year:"numeric"});
  const body = items.length
    ? `<div class="list">${items.map((r,i)=>{
        return `<div class="arow tone-${esc(r.kind)}" data-day-row="${i}"><span class="stripe"></span>
          <span class="ig">${kindIcon(r.kind)}</span>
          <span class="body"><span class="t">${esc(r.title||r.kind)}</span><span class="m">${esc(KIND_NAME[r.kind]||r.kind)}${r.entity_name?` · ${esc(r.entity_name)}`:""}${r.status?` · ${esc(r.status)}`:""}</span></span>
          <span class="act">${r.kind==="post"?"Open post":"Open"}</span></div>`;
      }).join("")}</div>`
    : `<div class="sec-empty"><p>Nothing scheduled for this day.</p></div>`;
  $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs"><a id="dayBack" class="text-link">Calendar</a> &nbsp;›&nbsp; <b>${esc(label)}</b></div>
      <h1 class="title" style="margin-top:6px">${esc(label)}</h1></div>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn" id="dayAct">${icon("plus")} Activity</button>
        <button class="btn" id="dayBackBtn">← Calendar</button></div></div>
    <div class="scroll"><p class="lead">${items.length} item${items.length===1?"":"s"} scheduled for this day.</p>${body}</div>`;
  $("#dayBack").onclick=()=>navigate("#/calendar");
  $("#dayBackBtn").onclick=()=>navigate("#/calendar");
  $("#dayAct").onclick=()=>navigate("#/activity/new",{date:dateStr,from:"calendar"});
  items.forEach((r,i)=>{ const el=$("#main").querySelector(`[data-day-row="${i}"]`); if(el) el.onclick=dayItemGo(r); });
}
const PROFILE_SCROLL = {};  // slug -> last scrollTop of the post list, so back-navigation restores position
const PROFILE_FILTER = {};  // slug -> selected post filter, so async refreshes do not reset the active tab
const PROFILE_CHAN = {};    // slug -> selected channel filter, same reason
// Profile filter chips: [key, label]. Scheduled/Unscheduled cut by date; the
// rest are ContentView buckets.
const PROFILE_FILTERS = [["scheduled", "Scheduled"], ["all", "All"], ["ideas", "Ideas"], ["drafts", "Drafts"],
  ["ready", "Ready"], ["published", "Published"], ["unscheduled", "Unscheduled"]];
const unscheduled = p => p.status !== "published" && !p.date;
const inProfileFilter = (p, f) => f === "unscheduled" ? unscheduled(p)
  : f === "scheduled" ? !!p.date
  : f === "all" || STAGE_GROUP[p.status] === f;

// The full profile page: one profile card (header with every profile action,
// channel filters, status filters) over the shared post rows, with bulk select.
async function renderProfile(slug, initChanFilter){
  CURRENT_PROFILE_SLUG = slug;
  const stillOn = routeGuard();
  const [posts, profData] = await Promise.all([
    api(`/api/profile/${slug}/posts`),
    api(`/api/profile/${slug}`),
    ensureIdRegistry(),
  ]);
  if (!stillOn()) return;
  const profile = contentProjects().flatMap(p => p.profiles).find(pr => pr.slug === slug)
    || { slug, name: profData.name, channels: [] };
  const channels = profile.channels || [];
  let FILTER = PROFILE_FILTER[slug] || "all";
  let CHAN_FILTER = initChanFilter || PROFILE_CHAN[slug] || null;
  const matchFilter = p => inProfileFilter(p, FILTER) && (!CHAN_FILTER || (p.channels || []).includes(CHAN_FILTER));
  const SELECTED = new Set();  // post ids ticked for bulk actions
  const actions = `<button class="btn sm" id="addIdea">${icon("plus")} Add idea</button>
    <button class="btn sm primary" id="genIdeas">${icon("zap")} Generate ideas</button>
    <details class="profile-more"><summary class="btn sm ghost" title="More profile actions" aria-label="More profile actions">${icon("more")}</summary>
      <div class="profile-more-menu"><button id="setupBtn">Setup</button><button id="logPosted">Log posted</button><button id="writeAll">Write all ideas</button></div></details>`;
  const chanPills = channels.map(ch => `<span class="chan-pill-wrap${CHAN_FILTER === ch.slug ? " on" : ""}">
      <button class="chan-pill${CHAN_FILTER === ch.slug ? " on" : ""}" data-cf="${esc(ch.slug)}">${platIcon(ch.platform)}${titledWithId(PLAT_NAME[ch.platform] || ch.platform)}${ch.handle ? `<span class="chan-handle">${esc(ch.handle)}</span>` : ""}</button>
      <button class="chan-gear" data-cg="${esc(ch.slug)}" title="Channel setup" aria-label="Channel setup">${icon("gear")}</button></span>`).join("");
  const count = f => posts.filter(p => inProfileFilter(p, f)).length;
  $("#main").innerHTML = `<div class="scroll"><div class="doc">
    <section class="pf pf-page">
      ${profileHeadHtml(profile, { chips: false, sub: profData.topic ? `<span class="profile-topic">${esc(profData.topic)}</span>` : "", end: `<div class="profile-actions">${actions}</div>` })}
      <div class="pf-tools">
        <div class="profile-channels">${chanPills}<button class="profile-text-btn" id="addChanBtn">${icon("plus")} Add channel</button></div>
        <div class="profile-list-head"><span class="profile-count">${plural(posts.length, "post")}</span><div class="filters">
          ${PROFILE_FILTERS.map(([k, label]) => `<button type="button" class="chip${FILTER === k ? " on" : ""}" data-f="${k}" aria-pressed="${FILTER === k}">${label} <span class="n">${count(k)}</span></button>`).join("")}
        </div></div>
        <div id="selbar"></div>
      </div>
      <div id="list"></div>
    </section></div></div>`;

  function drawSelBar(){
    const bar = $("#selbar"); if(!bar) return;
    const visible = posts.filter(matchFilter);
    // prune selections no longer visible so the count is honest
    [...SELECTED].forEach(id=>{ if(!visible.some(p=>p.id===id)) SELECTED.delete(id); });
    if(!SELECTED.size){ bar.innerHTML=""; return; }
    bar.innerHTML=`<div class="selbar"><b>${SELECTED.size} selected</b>
      <button class="btn sm" id="selAll">Select all ${visible.length}</button>
      <button class="btn sm ghost" id="selClear">Clear</button>
      <button class="btn sm danger-btn" id="selDel">Delete ${SELECTED.size}</button></div>`;
    $("#selAll").onclick=()=>{ visible.forEach(p=>SELECTED.add(p.id)); drawList(); };
    $("#selClear").onclick=()=>{ SELECTED.clear(); drawList(); };
    $("#selDel").onclick=()=>renderConfirmBulkDelete([...SELECTED],slug);
  }

  function drawList(){
    const list = ContentView.sortPosts(posts.filter(matchFilter));
    const el = $("#list");
    drawSelBar();
    el.innerHTML = list.map(p => postRowHtml(p, channels, { selectable: true, selected: SELECTED.has(p.id) })).join("")
      || `<div class="empty">Nothing here. Add an idea or generate a batch.</div>`;
    el.querySelectorAll("[data-sel]").forEach(b=>b.onclick=e=>{ e.stopPropagation();
      b.checked ? SELECTED.add(b.dataset.sel) : SELECTED.delete(b.dataset.sel);
      b.closest(".post-row")?.classList.toggle("sel",b.checked); drawSelBar(); });
    el.querySelectorAll("[data-act]").forEach(b=>b.onclick=()=>doNext(b.dataset.act));
    el.querySelectorAll("[data-edit]").forEach(b=>b.onclick=()=>navigate(`#/post/${b.dataset.edit}/edit`,{profileSlug:slug}));
    el.querySelectorAll("[data-view]").forEach(b=>b.onclick=()=>navigate(`#/post/${b.dataset.view}`,{profileSlug:slug}));
  }
  function byId(id){ return posts.find(p=>p.id===id)||{}; }
  async function doNext(id){
    if(!await runNextAction(byId(id), slug)) return;
    if(onProfileList(slug)) renderProfile(slug); renderRail(); }

  $("#main").querySelectorAll(".chip").forEach(c=>c.onclick=()=>{ FILTER=c.dataset.f; PROFILE_FILTER[slug] = FILTER;
    $("#main").querySelectorAll(".chip").forEach(x=>{ const on=x===c; x.classList.toggle("on",on); x.setAttribute("aria-pressed",String(on)); }); drawList(); });
  $("#main").querySelectorAll(".chan-pill").forEach(p=>p.onclick=()=>{
    CHAN_FILTER = PROFILE_CHAN[slug] = CHAN_FILTER===p.dataset.cf ? null : p.dataset.cf;
    $("#main").querySelectorAll(".chan-pill").forEach(x=>x.classList.toggle("on",x.dataset.cf===CHAN_FILTER));
    $("#main").querySelectorAll(".chan-pill-wrap").forEach(x=>x.classList.toggle("on",x.querySelector(".chan-pill")?.dataset.cf===CHAN_FILTER));
    drawList(); });
  $("#main").querySelectorAll(".chan-gear").forEach(btn=>btn.onclick=e=>{
    e.stopPropagation(); navigate(`#/channel/${btn.dataset.cg}/setup`,{profileSlug:slug});
  });
  $("#writeAll").onclick=()=>writeAllIdeas(slug);
  $("#addChanBtn").onclick=e=>{ e.stopPropagation(); openNewChannel(slug); };
  $("#setupBtn").onclick=()=>navigate(`#/profile/${slug}/setup`);
  $("#addIdea").onclick=()=>navigate(`#/profile/${slug}/add`);
  $("#logPosted").onclick=()=>navigate(`#/profile/${slug}/log`);
  $("#genIdeas").onclick=()=>navigate(`#/profile/${slug}/generate`);
  drawList();

  const scrollEl = $("#main .scroll");
  if(scrollEl){
    if(PROFILE_SCROLL[slug]!=null) scrollEl.scrollTop = PROFILE_SCROLL[slug];
    scrollEl.addEventListener("scroll", ()=>{ PROFILE_SCROLL[slug]=scrollEl.scrollTop; });
  }
}

// Write every idea-stage post into a full brief, ONE AT A TIME, refreshing the
// list after each so drafts appear as they land (instead of all at the end).
// Each brief is a Sonnet `claude -p` job (~10-15s); the endpoint re-indexes per
// call, so a re-render reflects the new Draft immediately.
let _writingAll = false;
async function writeAllIdeas(slug){
  if(_writingAll) return;
  const isIdea = p => p.status==="planned" || p.status==="approved_slot";
  let done = 0;
  _writingAll = true;
  try{
    while(true){
      const posts = await api(`/api/profile/${slug}/posts`);
      const left = posts.filter(isIdea);
      const next = left[0];
      if(!next){ toast(done? `All ideas written ✓ (${done})` : "No ideas to write — generate some first"); break; }
      toast(`✍ Writing briefs… ${left.length} left (Sonnet, ~15s each)`, true);
      try{ await api(postUrl(next.id,slug,"/brief"),{method:"POST"}); }
      catch(e){ toast("✗ stopped: "+e.message); break; }
      done++;
      if(onProfileList(slug)) await renderProfile(slug);     // the just-written post now shows as a Draft
    }
  } finally {
    _writingAll = false;
    renderRail();
  }
}

function selectProfileSetup(slug){ navigate(`#/profile/${slug}/setup`); }

async function renderProfileSetup(slug){
  CURRENT_PROFILE_SLUG = slug;
  const [profData, specsRes, voicesRes, platformsRes] = await Promise.all([
    api(`/api/profile/${slug}`),
    api(`/api/profile/${slug}/brief-specs`),
    api(`/api/profile/${slug}/voices`),
    api(`/api/profile/${slug}/platforms`),
    ensureIdRegistry(),
  ]);
  const profName = profData.name||slug;
  const setupTabId = composedIdOnly(OSID.tabProf(slug, "setup"));
  const platformOpts = ["all", ...(platformsRes.platforms||[])];

  function row(kind, item){ // kind: "voice" | "brief"
    const composedId = kind==="voice" ? composedIdOnly(OSID.profVoice(slug, item.id)) : composedIdOnly(OSID.profBriefSpec(slug, item.id));
    // The dropdown only lists single platforms + "all", but the backend allows
    // a comma list (e.g. "instagram,tiktok"). If the stored tag isn't one of
    // the listed options, add it as its own option so Save can't silently
    // downgrade it to "all" just because the user never touched the dropdown.
    const knownOpts = item.platforms && platformOpts.includes(item.platforms) ? platformOpts : [...platformOpts, item.platforms].filter(Boolean);
    const opts = knownOpts.map(p=>`<option value="${esc(p)}" ${item.platforms===p?"selected":""}>${esc(p)}</option>`).join("");
    return `<div class="setup-row" data-kind="${kind}" data-id="${esc(item.id)}">
      <div class="setup-row-head">
        ${flabel(item.id, composedId)}
        <div class="setup-row-actions">
          <select class="setup-platform">${opts}</select>
          <button class="btn danger-btn setup-delete">Delete</button>
        </div>
      </div>
      <textarea class="setup-text">${esc(item.text||"")}</textarea>
    </div>`;
  }

  $("#main").innerHTML = `${pageHeader("Profile setup", profName, `<button class="btn danger-btn" id="delProfBtn">Delete profile</button><button class="btn primary" id="saveProfBtn">Save</button>`, setupTabId || OSID.prof(slug))}
    <div class="scroll">
      <div class="form-shell">
        <div class="setup-profile-grid">
          <div>
            ${flabel("Display name")}
            <input class="setup-input" id="ps-name" value="${esc(profName)}">
          </div>
          <div>
            ${flabel("Topic / niche")}
            <input class="setup-input" id="ps-topic" value="${esc(profData.topic||"")}" placeholder="e.g. Weekly tips for home cooks">
          </div>
        </div>

        <div class="setup-section-head">
          <div>${flabel("Brand voice & tone")}</div>
          <button class="btn" id="addVoiceBtn">+ Add voice</button>
        </div>
        <p class="setup-help">Describe how this brand speaks. Add a second voice if you want a distinct one for specific platforms — nothing auto-switches between them, you pick which to use each time you generate.</p>
        <div id="voiceRows">${voicesRes.voices.map(v=>row("voice", v)).join("")}</div>

        <div class="setup-section-head spaced">
          <div>${flabel("Post brief spec")}</div>
          <button class="btn" id="addBriefBtn">+ Add brief-spec</button>
        </div>
        <p class="setup-help">Per-profile output rules for new posts. Changing one does not alter briefs already written. Add a second one if you want separate rules for a platform — selection is always manual.</p>
        <div id="briefRows">${specsRes.specs.map(s=>row("brief", s)).join("")}</div>
      </div>
    </div>`;
  wireIdChips($("#main"));

  $("#addVoiceBtn").onclick = async()=>{
    try{ await jpost(`/api/profile/${slug}/voices`, {text:"", platforms:"all"}); renderProfileSetup(slug); }
    catch(e){ toast("✗ "+e.message); }
  };
  $("#addBriefBtn").onclick = async()=>{
    try{ await jpost(`/api/profile/${slug}/brief-specs`, {text:"", platforms:"all"}); renderProfileSetup(slug); }
    catch(e){ toast("✗ "+e.message); }
  };
  $$(".setup-delete").forEach(btn=>{
    btn.onclick = async()=>{
      const rowEl = btn.closest(".setup-row");
      const kind = rowEl.dataset.kind, id = rowEl.dataset.id;
      const path = kind==="voice" ? `voices/${id}/delete` : `brief-specs/${id}/delete`;
      try{ await jpost(`/api/profile/${slug}/${path}`, {}); renderProfileSetup(slug); }
      catch(e){ toast("✗ "+e.message); }
    };
  });

  $("#saveProfBtn").onclick = async()=>{
    const profile={name:$("#ps-name").value, topic:$("#ps-topic").value};
    try{
      await jpost(`/api/profile/${slug}/update`, profile);
      for(const rowEl of $$(".setup-row")){
        const kind = rowEl.dataset.kind, id = rowEl.dataset.id;
        const text = rowEl.querySelector(".setup-text").value;
        const plat = rowEl.querySelector(".setup-platform").value;
        const path = kind==="voice" ? `voices/${id}/update` : `brief-specs/${id}/update`;
        await jpost(`/api/profile/${slug}/${path}`, {text, platforms:plat});
      }
      toast("Saved ✓"); renderRail();
    }
    catch(e){ toast("✗ "+e.message); }
  };
  $("#delProfBtn").onclick = ()=>renderConfirmDeleteProfile(slug);
}

async function renderChannelGuidelines(slug){
  $("#main").innerHTML = `${pageHeader("Channel guidelines", slug, `<button class="btn" id="refineBtn">${icon("zap")} Refine with AI</button><button class="btn primary" id="saveBtn">Save</button>`, OSID.chan(slug))}
    <div class="help">Injected into every generation for this channel</div>
    <div class="scroll">
      <p class="help">Use <code>## General</code> + per-platform sections. These guidelines are injected into every generation for this channel.</p>
      <textarea id="glText" class="code-editor" rows="22" placeholder="Add guidelines here…"></textarea>
    </div>`;
  try{
    const d = await api(`/api/channel/${slug}/guidelines`);
    const t = $("#glText"); if(t) t.value = d.text||"";
  }catch(e){ toast("Could not load guidelines: "+e.message); }
  $("#saveBtn").onclick = async()=>{
    const text = $("#glText").value;
    try{ await api(`/api/channel/${slug}/guidelines`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})}); toast("Guidelines saved ✓"); }
    catch(e){ toast("✗ "+e.message); }
  };
  $("#refineBtn").onclick = async()=>{
    const btn = $("#refineBtn"), t = $("#glText");
    btn.textContent = "refining…"; btn.disabled = true;
    try{
      const d = await api(`/api/channel/${slug}/guidelines/refine`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t.value})});
      t.value = d.refined;
      toast("Refined — review and Save when ready");
    }catch(e){ toast("✗ "+e.message); }
    finally{ btn.innerHTML = `${icon("zap")} Refine with AI`; btn.disabled = false; }
  };
}

// ── Brief content renderer (shared by post detail + revise pages) ───────────

// "cover_overlay" → "Cover overlay". Field labels come from the brief itself.
function humanizeKey(k){ return String(k).replace(/[_-]+/g," ").replace(/^\w/,c=>c.toUpperCase()); }

function sentenceCase(s){
  const t=String(s||"").trim();
  if(!t) return t;
  const words=t.split(/\s+/), first=words[0]||"";
  if(first.length>=2&&first.length<=4&&first===first.toUpperCase()&&/^[A-Z]+$/.test(first)){
    const rest=words.slice(1).join(" ").toLowerCase();
    return rest?first+" "+rest:first;
  }
  return t.charAt(0).toUpperCase()+t.slice(1).toLowerCase();
}

function isSlideOverlayItem(o){
  return o&&typeof o==="object"&&("overlay" in o||"slide" in o);
}

function renderSlideOverlayList(items, postId, opts = {}){
  const rows = items.map((o, i) => {
    const n = o.slide ?? (i + 1);
    const text = o.overlay ?? "";
    const fld = OSID.briefFld(postId, `slide-${n}`);
    return `<div style="display:flex;align-items:flex-start;gap:8px">`
      + `<span class="slide-n">${esc(String(n))}</span>`
      + `<div style="flex:1">`
      + `<div style="margin-bottom:4px">${sectionIdChip(fld)}</div>`
      + `<div class="pre-box">${esc(text)}</div>`
      + `</div></div>`;
  }).join("");
  const addBtn = opts.showAdd
    ? `<button type="button" class="btn" id="pd-add-slide">${icon("plus")} Slide</button>`
    : "";
  return `<div style="display:flex;flex-direction:column;gap:8px">${rows}${addBtn}</div>`;
}

function renderGenPromptList(prompts, postId){
  return `<div style="display:flex;flex-direction:column;gap:10px">` + prompts.map((text, i) => {
    const n = i + 1;
    const fld = `gen-prompt-${n}`;
    const chipId = OSID.briefFld(postId, fld);
    return `<div style="display:flex;gap:10px;align-items:flex-start">`
      + `<span class="slide-n">${esc(String(n))}</span>`
      + `<div style="flex:1">`
      + `<div style="margin-bottom:4px">${sectionIdChip(chipId)}</div>`
      + `<div class="pre-box">${esc(text)}</div>`
      + `</div></div>`;
  }).join("") + `</div>`;
}

// Render ONE brief value by its shape — no hardcoded field names. This is what
// makes each profile's brief render its OWN output: whatever fields that
// profile's brief-spec produced get shown, in authored order, nothing assumed.
function renderBriefValue(v){
  if(v==null||v==="") return "";
  if(Array.isArray(v)){
    if(!v.length) return "";
    if(typeof v[0]==="object"&&v[0]!==null){
      if(v.every(isSlideOverlayItem)) return renderSlideOverlayList(v);
      return `<div style="display:flex;flex-direction:column;gap:6px">`+v.map(o=>
        `<div class="pre-box">`
        +Object.entries(o).filter(([,val])=>val!=null&&val!=="")
          .map(([kk,val])=>`<b>${esc(humanizeKey(kk))}:</b> ${esc(Array.isArray(val)?val.join(" · "):val)}`).join("&nbsp; · &nbsp;")
        +`</div>`).join("")+`</div>`;
    }
    return `<div class="accent-text">${v.map(esc).join(" · ")}</div>`;
  }
  if(typeof v==="object"){                              // nested object, e.g. visual_brief
    return Object.entries(v).filter(([,val])=>val!=null&&val!=="").map(([kk,val])=>
      `<div style="font-size:12.5px;line-height:1.55;margin-bottom:4px"><b>${esc(humanizeKey(kk))}:</b> ${renderBriefValue(val)}</div>`
    ).join("");
  }
  return `<div class="pre-box">${esc(v)}</div>`;
}

// Identity fields stored on the brief JSON but shown elsewhere in the post UI.
const BRIEF_UI_SKIP = new Set(["id", "channels"]);

function briefSect(h, b, fldId){
  const chip = fldId ? sectionIdChip(fldId) : "";
  return `<div style="margin-top:16px">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
      <div class="eyebrow">${esc(h)}</div>${chip}
    </div>${b}</div>`;
}

function renderBriefEditField(key, value, postId){
  const label = humanizeKey(key);
  const fldId = OSID.briefFld(postId, key);
  if (key === "slide_overlays" && Array.isArray(value) && value.every(isSlideOverlayItem)) {
    return value.map((o, i) => {
      const n = o.slide ?? (i + 1);
      return `${flabel(`${label} ${n}`)}${fta(
        `brief.slide.${i}`, o.overlay || "", 4,
        `data-brief-field="slide_overlays" data-brief-idx="${i}"`,
        OSID.briefFld(postId, `slide-${n}`),
      )}`;
    }).join("");
  }
  if (key === "gen_prompts" && Array.isArray(value) && value.length && typeof value[0] === "string") {
    return value.map((text, i) => `${flabel(`${label} ${i + 1}`)}${fta(
      `brief.gen.${i}`, text, 5,
      `data-brief-field="gen_prompts" data-brief-idx="${i}"`,
      OSID.briefFld(postId, `gen-prompt-${i + 1}`),
    )}`).join("");
  }
  if (typeof value === "string" || value == null || typeof value === "number") {
    const text = value == null ? "" : String(value);
    const rows = text.length > 140 ? 6 : text.includes("\n") ? 5 : 3;
    return `${flabel(label)}${fta(`brief.${key}`, text, rows, `data-brief-field="${esc(key)}"`, fldId)}`;
  }
  if (Array.isArray(value) && value.every(v => typeof v === "string")) {
    return `${flabel(label)}${fta(
      `brief.${key}`, value.join("\n"), Math.max(3, value.length + 1),
      `data-brief-field="${esc(key)}" data-brief-type="lines"`, fldId,
    )}`;
  }
  return `${flabel(label)}${fta(
    `brief.${key}`, JSON.stringify(value, null, 2), 8,
    `data-brief-field="${esc(key)}" data-brief-type="json"`, fldId,
  )}`;
}

function renderBriefEditSection(brief, postId){
  if (!brief || brief._error) return "";
  const fields = Object.entries(brief)
    .filter(([k]) => !k.startsWith("_") && !BRIEF_UI_SKIP.has(k))
    .map(([k, v]) => renderBriefEditField(k, v, postId))
    .join("");
  if (!fields) return "";
  const brId = sectionIdChip(OSID.brief(postId));
  return `<div class="section-split">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px">
      <div class="eyebrow">Brief</div>${brId}
    </div>${fields}</div>`;
}

function briefFromEditForm(root, originalBrief){
  const out = JSON.parse(JSON.stringify(originalBrief || {}));
  root.querySelectorAll("[data-brief-field]:not([data-brief-idx])").forEach(el => {
    const key = el.dataset.briefField;
    const type = el.dataset.briefType;
    if (type === "json") {
      const raw = el.value.trim();
      if (!raw) { delete out[key]; return; }
      try { out[key] = JSON.parse(raw); }
      catch { throw new Error(`Invalid JSON in ${humanizeKey(key)}`); }
      return;
    }
    if (type === "lines") {
      const lines = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      if (lines.length) out[key] = lines;
      else delete out[key];
      return;
    }
    const v = el.value.trim();
    if (v) out[key] = v;
    else delete out[key];
  });
  if (Array.isArray(out.slide_overlays)) {
    const slides = out.slide_overlays.map((item, i) => ({ ...item }));
    root.querySelectorAll('[data-brief-field="slide_overlays"]').forEach(el => {
      const i = +el.dataset.briefIdx;
      if (!slides[i] || typeof slides[i] !== "object") return;
      slides[i] = { ...slides[i], overlay: el.value };
    });
    out.slide_overlays = slides;
  }
  if (Array.isArray(out.gen_prompts)) {
    const prompts = [...out.gen_prompts];
    root.querySelectorAll('[data-brief-field="gen_prompts"]').forEach(el => {
      const i = +el.dataset.briefIdx;
      if (i < 0 || i >= prompts.length) return;
      prompts[i] = el.value;
    });
    out.gen_prompts = prompts;
  }
  return out;
}

// Schema-agnostic: render exactly the fields the profile's brief contains.
function renderBriefBody(slot, brief, n, postId){
  let body="";
  if(slot.concept&&!brief) body+=briefSect("Concept",`<div style="font-size:13px;line-height:1.6">${esc(slot.concept)}</div>`);
  if(slot.notes) body+=briefSect("Notes",`<div style="font-size:13px;line-height:1.6;white-space:pre-wrap">${esc(slot.notes)}</div>`);
  if(slot.working_title&&!brief) body+=briefSect("Working title",`<div style="font-size:13px;line-height:1.6">${esc(slot.working_title)}</div>`);
  if(brief&&brief._error) body+=briefSect("Brief",`<div class="error-text">${esc(brief._error)}</div>`);
  else if(brief){
    const brId = postId ? OSID.brief(postId) : null;
    if(brId) body+=`<div style="display:flex;align-items:center;gap:8px;margin-top:16px;margin-bottom:8px;flex-wrap:wrap">
      <div class="eyebrow">Brief</div>${sectionIdChip(brId)}</div>`;
    for(const [k,v] of Object.entries(brief)){
      if(k.startsWith("_")||BRIEF_UI_SKIP.has(k)) continue;
      const shown=k==="cover_overlay"?sentenceCase(v):v;
      let html;
      if(k==="slide_overlays"&&Array.isArray(shown)&&shown.every(isSlideOverlayItem))
        html=renderSlideOverlayList(shown, postId, { showAdd: true });
      else if(k==="gen_prompts"&&Array.isArray(shown)&&shown.length&&typeof shown[0]==="string")
        html=renderGenPromptList(shown, postId);
      else html=renderBriefValue(shown);
      const fldId = postId ? OSID.briefFld(postId, k) : null;
      if(html) body+=briefSect(humanizeKey(k), html, fldId);
    }
  } else {
    body+=`<div class="empty">Not written yet${n&&n.kind==="brief"?` — click <b>${esc(n.label)}</b> to generate.`:"."}</div>`;
  }
  return body;
}

// ── Post detail (replaces showDetail modal) ──────────────────────────────────
async function renderPostDetail(id, profileSlug){
  const stillOn = routeGuard();
  let detail;
  try{
    [detail] = await Promise.all([api(postUrl(id,profileSlug)), ensureIdRegistry()]);
  }catch(e){ return toast("✗ "+e.message); }
  if (!stillOn()) return;
  const slot=detail.slot||{}, brief=detail.brief||null;
  CURRENT_PROFILE_SLUG = profileSlug || detail.profile_slug || null;
  CURRENT_POST = { id, slot, brief };
  const st=slot.status||"planned", na=nextActionFor({...slot, status:st});
  const title=slot.working_title||slot.pillar||id;
  const canRevise=["planned","approved_slot","briefed","approved"].includes(st);
  const postContent=PostCopy.formatPostContent(slot, brief);
  const meta=[["Format",slot.format],["Pillar",slot.pillar],["Objective",slot.objective]]
    .filter(([,v])=>v!=null&&v!=="").map(([k,v])=>metaPlain(k,v)).join("");
  const channels=(_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===CURRENT_PROFILE_SLUG)||{}).channels||[];
  const platforms=[...new Set((slot.channels||[]).map(sl=>channels.find(c=>c.slug===sl)).filter(Boolean))];
  // A draft is approved only here, explicitly; every other status gets its next step.
  const btns=[
    st==="briefed"?`<button class="btn primary" id="approvePost">${icon("check")} Approve</button>`
      : na?`<button class="btn primary" id="pd-next">${esc(na.label)}</button>`:"",
    `<button class="btn" id="pd-edit">Edit</button>`,
    canRevise?`<button class="btn" id="pd-revise">Revise</button>`:"",
    postContent?`<button class="btn ghost" id="pd-copy">Copy post</button>`:"",
    `<details class="profile-more"><summary class="btn ghost" title="More post actions" aria-label="More post actions">${icon("more")}</summary><div class="profile-more-menu"><button class="danger-btn" id="pd-del">Delete</button></div></details>`,
  ].filter(Boolean).join("");
  $("#main").innerHTML=`<div class="scroll"><div class="doc post-page">
      <header class="pp-head">
        <div class="pr-top"><span class="stp ${STATUS_PILL_CLASS[st]||"idea"}">${esc(plainStatus(st))}</span>${channelChips(platforms)}
          <span class="when">${icon("clock")}${esc(slot.date||"No date")}</span></div>
        <h1 class="pp-title">${esc(title)}</h1>
        <div class="pp-meta">${meta}${sectionIdChip(OSID.post(id))}</div>
        <div class="pp-actions">${btns}</div>
      </header>
      ${renderBriefBody(slot,brief,na,id)}</div></div>`;
  renderCanvasNav();   // the breadcrumb now knows the post's profile
  wireIdChips($("#main"));
  const addSlide=document.getElementById("pd-add-slide");
  if(addSlide) addSlide.onclick=async()=>{
    const overlay=prompt("Slide overlay (3 lines: show name / season+year / one-line description):");
    if(!overlay||!overlay.trim()) return;
    try{
      const out=await jpost(postUrl(id,profileSlug,"/slide/new"),{overlay:overlay.trim()});
      toast(`Slide ${out.slide} added · ${out.field_id||""}`);
      navigate(`#/post/${id}`,{profileSlug});
    }catch(e){ toast("✗ "+e.message); }
  };
  document.getElementById("pd-del").onclick=()=>renderConfirmDelete(id,profileSlug);
  document.getElementById("pd-edit").onclick=()=>navigate(`#/post/${id}/edit`,{profileSlug});
  const copyBtn=document.getElementById("pd-copy");
  if(copyBtn) copyBtn.onclick=()=>copyText(postContent,"Post copied ✓");
  const rdBtn=document.getElementById("pd-revise"); if(rdBtn) rdBtn.onclick=()=>navigate(`#/post/${id}/revise`,{profileSlug});
  const ap=document.getElementById("approvePost"); if(ap) ap.onclick=async()=>{
    ap.disabled=true;
    try{ await jpost(postUrl(id,profileSlug,"/status"),{status:"approved"}); toast("✓ Ready");
      navigate(`#/post/${id}`,{profileSlug}); }
    catch(e){ ap.disabled=false; toast("✗ "+e.message); }
  };
  const nb=document.getElementById("pd-next"); if(nb) nb.onclick=async()=>{
    nb.disabled=true;
    if(await runNextAction({...slot, id, status:st}, profileSlug)) navigate(`#/post/${id}`,{profileSlug});
    else nb.disabled=false;
  };
}

// ── Edit post (replaces rowMenu modal) ───────────────────────────────────────
async function renderEditPost(id, profileSlug){
  let detail; try{ detail=await api(postUrl(id,profileSlug)); }catch(e){ return toast("✗ "+e.message); }
  const slot=detail.slot||{}, brief=detail.brief||null;
  CURRENT_PROFILE_SLUG = profileSlug || detail.profile_slug || null;
  CURRENT_POST = { id, slot, brief };
  const profNode=_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===(profileSlug||detail.profile_slug))||{channels:[]};
  const chanHint=(profNode.channels||[]).map(c=>c.slug).join(", ")||"e.g. my-profile-tiktok";
  const crumb=profNode.name||profileSlug||"Back";
  const briefSection = brief && !brief._error ? renderBriefEditSection(brief, id) : "";
  // a "logged post" is one recorded via Log posted: published with no brief.
  // It has no concept/objective/format — show only what applies, plus notes.
  const isLogged = slot.status==="published" && !brief;
  const notesField = `${flabel("Notes")}${fta("notes",slot.notes||"",3,'placeholder="anything worth remembering about this post"')}`;
  const slotFields = isLogged
    ? `${flabel("Title")}${finput("working_title",slot.working_title||"",'placeholder="short internal label"')}
      ${flabel("Date posted")}${finput("date",slot.date||"",'type="date"')}
      ${flabel("Channels")}${fchannels(profNode.channels,(slot.channels||[]))}
      ${notesField}`
    : `${flabel("Title")}${finput("working_title",slot.working_title||"",'placeholder="short internal label"')}
      ${flabel("Concept")}${fta("concept",slot.concept||"",3,'placeholder="what this post does and why"')}
      ${flabel("Date")}<div style="display:flex;gap:8px;align-items:center">${finput("date",slot.date||"",'type="date"')}${slot.status!=="published"?`<button type="button" class="btn" id="ep-clear-date" style="flex:none">Clear date</button>`:""}</div>
      ${flabel("Format")}${finput("format",slot.format||"carousel",'placeholder="carousel | reel | short"')}
      ${flabel("Pillar")}${finput("pillar",slot.pillar||"")}
      ${flabel("Objective")}${finput("objective",slot.objective||"")}
      ${flabel("Channels")}${fchannels(profNode.channels,(slot.channels||[]))}
      ${notesField}
      ${briefSection}`;
  $("#main").innerHTML=`${pageHeader("Edit post",crumb,`<button class="btn danger-btn" id="ep-del">Delete</button><button class="btn primary" id="ep-save">Save</button>`, OSID.post(id))}
    <div class="scroll"><div class="fpage">
      <div class="eyebrow">${isLogged?"Logged post":"Slot"}</div>
      ${slotFields}
    </div></div>`;
  wireIdChips($("#main"));
  document.getElementById("ep-save").onclick=async()=>{
    const data=formVals($("#main"));
    data.channels=checkedChannels($("#main"));
    const payload={...data};
    if (brief && !brief._error) {
      try{ payload.brief=briefFromEditForm($("#main"), brief); }
      catch(e){ return toast("✗ "+e.message); }
    }
    try{ await api(postUrl(id,profileSlug,"/update"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      toast("Saved ✓");
      // pop the edit page off history so the post detail's back button returns
      // to where you came from (profile/calendar), not back into edit.
      history.back(); }
    catch(e){ toast("✗ "+e.message); }
  };
  document.getElementById("ep-del").onclick=()=>renderConfirmDelete(id,profileSlug);
  const clearDateBtn=document.getElementById("ep-clear-date");
  if(clearDateBtn) clearDateBtn.onclick=()=>{ $("#main input[name=date]").value=""; };
}

// ── Revise with AI ───────────────────────────────────────────────────────────
async function renderRevise(id){
  let detail; try{ detail=await api(`/api/post/${id}`); }catch(e){ return toast("✗ "+e.message); }
  const slot=detail.slot||{}, brief=detail.brief||null;
  const kind=brief?"draft":"idea", title=slot.working_title||slot.pillar||id;
  const profileSlug=detail.profile_slug||"";
  CURRENT_PROFILE_SLUG = profileSlug || null;
  CURRENT_POST = { id, slot, brief };
  $("#main").innerHTML=`${pageHeader("Revise with AI",title,`<button class="btn primary" id="rv-go">Revise</button>`, OSID.post(id))}
    <div class="scroll"><div class="fpage">
      <p class="help">
        Describe what should change in this ${kind}. The AI will apply your instruction and preserve everything else.
      </p>
      ${flabel("Instruction")}${fta("instruction","",4,'placeholder="e.g. punchier hook, caption under 200 chars, focus on 90s thrillers" required')}
    </div></div>`;
  document.getElementById("rv-go").onclick=async()=>{
    const btn=document.getElementById("rv-go"), instruction=$("#main textarea[name=instruction]").value.trim();
    if(!instruction) return toast("Enter an instruction first");
    btn.disabled=true; btn.textContent="Revising…"; toast("Revising via claude -p…",true);
    try{ await jpost(`/api/post/${id}/revise`,{instruction}); toast("Revised ✓");
      history.back(); }
    catch(e){ btn.disabled=false; btn.textContent="Revise"; toast("✗ "+e.message); }
  };
}

// ── New / edit project ───────────────────────────────────────────────────────
function renderNewProject(){
  $("#main").innerHTML=`${pageHeader("New project","Projects",`<button class="btn primary" id="np-save">Create project</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Name")}${finput("name","",'placeholder="My Venture" required')}
      ${flabel("Slug (auto-filled, editable)")}${finput("slug","",'placeholder="my-venture" required')}
      ${flabel("Kind")}${fsel("kind",[["venture","Venture"],["brand","Brand"]],"venture")}
      ${flabel("Priority")}${fsel("priority",[["primary","Primary"],["secondary","Secondary"],["experiment","Experiment"]],"primary")}
      ${flabel("Status")}${fsel("status",[["idea","Idea"],["prototype","Prototype"],["live","Live"],["revenue","Revenue"]],"idea")}
      ${localFolderField()}
    </div></div>`;
  wireLocalFolderControls($("#main"));
  const n=$("#main input[name=name]"), s=$("#main input[name=slug]");
  n.oninput=()=>{ if(!s.dataset.manual) s.value=slugify(n.value); };
  s.oninput=()=>{ s.dataset.manual=s.value?"1":""; };
  document.getElementById("np-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ const out=await jpost("/api/project/new",data); toast("Project created ✓ · new workspace"); await window.Workspaces.switchTo(out.slug||data.slug); }
    catch(e){ toast("✗ "+e.message); }
  };
}

async function renderEditProject(slug){
  let e={}; try{ e=(await api(`/api/project/${slug}`)).entity||{}; }catch(_){}
  $("#main").innerHTML=`${pageHeader("Edit project",e.name||slug,`<button class="btn danger-btn" id="ep2-del">Delete project</button><button class="btn primary" id="ep2-save">Save</button>`, OSID.proj(slug))}
    <div class="scroll"><div class="fpage">
      ${flabel("Name")}${finput("name",e.name||"",'required')}
      ${flabel("Kind")}${fsel("kind",[["venture","Venture"],["brand","Brand"]],e.subtype||"venture")}
      ${flabel("Priority")}${fsel("priority",[["primary","Primary"],["secondary","Secondary"],["experiment","Experiment"]],e.priority||"primary")}
      ${flabel("Status")}${fsel("status",[["idea","Idea"],["prototype","Prototype"],["live","Live"],["revenue","Revenue"]],e.status||"idea")}
      ${flabel("Hours / week")}${finput("hours_per_week",String(e.hours_per_week??0),'type="number" min="0"')}
      ${flabel("Folders")}<p class="help"><a class="text-link" id="ep2-folders">Linked folders and access</a></p>
      ${flabel("Logo")}<p class="help"><a class="text-link" id="ep2-logo">Change logo</a></p>
      <p class="help">Slug <b>${esc(slug)}</b> is fixed — it's the identity used across files.</p>
    </div></div>`;
  document.getElementById("ep2-folders").onclick=()=>{ if(_WS!==slug) window.Workspaces.switchTo(slug).then(()=>navigate("#/folders")); else navigate("#/folders"); };
  document.getElementById("ep2-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ await jpost(`/api/project/${slug}/update`,data); toast("Project updated ✓"); await renderRail(); refreshViews(); history.back(); }
    catch(e){ toast("✗ "+e.message); }
  };
  document.getElementById("ep2-del").onclick=()=>renderConfirmDeleteProject(slug);
  document.getElementById("ep2-logo").onclick=()=>navigate(`#/workspace/${encodeURIComponent(slug)}/logo`);
}

// ── Workspace logo ───────────────────────────────────────────────────────────
// The picked image is redrawn on a 256px canvas and sent as PNG: small, square,
// and free of camera metadata (EXIF location) whatever the source file held.
const LOGO_PX = 256;
function logoPng(file){
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const c = document.createElement("canvas"); c.width = c.height = LOGO_PX;
      const k = Math.min(LOGO_PX / img.naturalWidth, LOGO_PX / img.naturalHeight);
      const w = img.naturalWidth * k, h = img.naturalHeight * k;
      c.getContext("2d").drawImage(img, (LOGO_PX - w) / 2, (LOGO_PX - h) / 2, w, h);
      resolve(c.toDataURL("image/png").split(",")[1]);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("not an image this browser can read")); };
    img.src = url;
  });
}

async function renderWsLogo(id){
  try { _WORKSPACES = (await api("/api/workspaces")).workspaces || _WORKSPACES; } catch {}
  const w = wsById(id);
  if (!w) { $("#main").innerHTML = `${pageHeader("Workspace logo", "Back")}<div class="scroll"><div class="fpage"><p class="help">Unknown workspace.</p></div></div>`; return; }
  const url = wsLogoUrl(w);
  $("#main").innerHTML = `${pageHeader("Workspace logo", w.name)}
    <div class="scroll"><div class="fpage">
      <div class="logo-edit">
        ${url ? `<img class="logo-preview" src="${esc(url)}" alt="">` : `<span class="logo-preview t-${hueFor(w.id)}" aria-hidden="true">${esc(wsBadge(w))}</span>`}
        <div class="logo-actions">
          <label class="btn primary">${icon("plus")} Upload image<input type="file" id="logo-file" accept="image/png,image/jpeg,image/webp" hidden></label>
          ${w.logo ? `<button class="btn" id="logo-remove">${w.id === "hq" ? "Use the Leo Bot logo" : "Remove"}</button>` : ""}
        </div>
      </div>
      <p class="help">PNG, JPEG or WebP. Shown in the workspace bar and at the top of the sidebar, resized to ${LOGO_PX}px square. ${w.id === "hq" ? "Without one, HQ shows the Leo Bot logo." : "Without one, the project shows its initials."}</p>
    </div></div>`;
  const done = async msg => { toast(msg); await renderRail(); renderWsLogo(id); };
  $("#logo-file").onchange = async e => {
    const file = e.target.files?.[0]; if (!file) return;
    try { await jpost("/api/workspace-logo", { ws: w.id, data: await logoPng(file) }); await done("Logo updated ✓"); }
    catch (err) { toast("✗ " + err.message); }
  };
  $("#logo-remove")?.addEventListener("click", async () => {
    try { await jpost("/api/workspace-logo/remove", { ws: w.id }); await done("Logo removed"); }
    catch (err) { toast("✗ " + err.message); }
  });
}

// ── Project section artifacts (files → reindex → IDs stay in sync) ───────────
function renderNewIntake(projectSlug){
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  $("#main").innerHTML = `${pageHeader("New intake", projName, `<button class="btn primary" id="ni-save">Create intake</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">
        Creates <code>strategy/intake.md</code> with starter headings. Edit in chat or your editor; IDs update on save via reindex.
      </p>
    </div></div>`;
  document.getElementById("ni-save").onclick = async () => {
    try {
      const out = await jpost(`/api/project/${projectSlug}/intake/new`, {});
      toast(`Intake created · ${out.path || ""}`);
      await renderRail(); navigate(`#/project/${projectSlug}/validation`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

function renderNewTechnical(projectSlug){
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  $("#main").innerHTML = `${pageHeader("New technical", projName, `<button class="btn primary" id="nt-save">Create technical</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">
        Creates <code>technical.md</code> with starter sections. Edit each with Edit on the Technical tab.
      </p>
    </div></div>`;
  document.getElementById("nt-save").onclick = async () => {
    try {
      const out = await jpost(`/api/project/${projectSlug}/technical/new`, {});
      toast(`Technical created · ${out.path || ""}`);
      await renderRail(); navigate(`#/project/${projectSlug}/technical`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderNewDocSubsection(projectSlug, docKey){
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const tab = docKey === "technical" ? "technical" : docKey;
  $("#main").innerHTML = `${pageHeader("New subsection", projName, `<button class="btn primary" id="ns-save">Add subsection</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Section title")}${finput("title", "", 'placeholder="e.g. Security" required')}
      <p class="help">Adds a heading to this project&apos;s ${esc(docKey)} list and <code>${esc(docKey)}.md</code>.</p>
    </div></div>`;
  document.getElementById("ns-save").onclick = async () => {
    const title = ($("#main input[name=title]").value || "").trim();
    if (!title) return toast("Title required");
    try {
      await jpost(`/api/project/${projectSlug}/subsections/add`, { doc: docKey, title });
      toast(`Subsection added · ${title}`);
      await renderRail(); navigate(`#/project/${projectSlug}/${tab}`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderEditDocSubsection(projectSlug, docKey, titleKey, extras = {}){
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const tab = docKey === "intake" ? "validation" : docKey;
  const p = await api(`/api/project/${projectSlug}`);
  const order = projectDocSubsections(p, docKey);
  const title = extras.subTitle
    || order.find(t => OSID.slugKey(t) === titleKey)
    || order.find(t => t.toLowerCase() === String(titleKey || "").replace(/-/g, " "));
  if (!title) {
    $("#main").innerHTML = `<div class="scroll"><p class="memo-empty">Subsection not found.</p></div>`;
    return;
  }
  const sec = (p.sections || {})[tab] || {};
  const art = (sec.artifacts || []).find(a => {
    if (a.kind !== "file") return false;
    const p = a.path || "";
    if (docKey === "intake") return p.endsWith("strategy/intake.md");
    return p.endsWith(`${docKey}.md`);
  });
  const parsed = Object.fromEntries(parseMdSections(art?.text || "").map(s => [s.title, s.body]));
  const body = (parsed[title] || "").trim();
  const subId = docSubsectionId(projectSlug, p, docKey, title);
  $("#main").innerHTML = `${pageHeader(`Edit · ${title}`, projName, `<button class="btn primary" id="es-save">Save</button>`, subId)}
    <div class="scroll"><div class="fpage">
      <p class="help">Markdown for <b>${esc(title)}</b> only.</p>
      <textarea id="es-body" class="doc-editor">${esc(body)}</textarea>
    </div></div>`;
  document.getElementById("es-save").onclick = async () => {
    try {
      await jpost(`/api/project/${projectSlug}/doc/${docKey}/section`, { title, body: $("#es-body").value });
      toast(`Saved · ${title}`);
      await renderRail(); navigate(`#/project/${projectSlug}/${tab}`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderNewMemo(projectSlug, memoType){
  await ensureSchemas();
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const label = memoTypeLabel(memoType);
  const fields = schemaFields("memo", memoType);
  const formHtml = fields.map(renderSchemaField).join("");
  $("#main").innerHTML = `${pageHeader("New memo", `${projName} · ${label}`, `<button class="btn primary" id="nm-save">Create memo</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">Next version: <code>${esc(memoType)}-vN.json</code> — same schema as chat/osctl writes.</p>
      ${formHtml}
    </div></div>`;
  document.getElementById("nm-save").onclick = async () => {
    const data = { ...formVals($("#main")), type: memoType };
    try {
      const out = await jpost(`/api/project/${projectSlug}/memo/new`, data);
      toast(`Memo created · ${out.id || ""}`);
      await renderRail();
      const back = (_SCHEMAS?.memo_section || {})[memoType] || "pricing";
      navigate(`#/project/${projectSlug}/${back}`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderNewExperiment(projectSlug){
  await ensureSchemas();
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const formHtml = schemaFields("experiment").map(renderSchemaField).join("");
  $("#main").innerHTML = `${pageHeader("New experiment", projName, `<button class="btn primary" id="ne-save">Create experiment</button>`)}
    <div class="scroll"><div class="fpage">${formHtml}</div></div>`;
  document.getElementById("ne-save").onclick = async () => {
    const data = formVals($("#main"));
    if (!data.assumption?.trim()) return toast("Assumption is required");
    try {
      const out = await jpost(`/api/project/${projectSlug}/experiment/new`, data);
      toast(`Experiment created · ${out.id || ""}`);
      await renderRail(); navigate(`#/project/${projectSlug}/experiments`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

// ---- GTM forms: phases + strategies -------------------------------------- #
function _projName(slug){ return (_TREE.find(p => p.slug === slug) || {}).name || slug; }

const PHASE_STATUS_OPTS = [["planned","planned"],["active","active"],["done","done"]];
const STRAT_STATUS_OPTS = [["proposed","proposed"],["active","active"],["paused","paused"],["killed","killed"]];

function renderNewPhase(projectSlug){
  $("#main").innerHTML = `${pageHeader("New GTM phase", _projName(projectSlug), `<button class="btn primary" id="np-save">Create phase</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">A time-boxed bet (e.g. "First 10 customers"). Strategies sit under it.</p>
      ${flabel("Name")}${finput("name","",'placeholder="First 10 customers" required')}
      ${flabel("Goal")}${finput("goal","",'placeholder="10 paying users"')}
      ${flabel("Timebox")}${finput("timebox","",'placeholder="Q3 2026 / 6 weeks"')}
      ${flabel("Status")}${fsel("status",PHASE_STATUS_OPTS,"planned")}
    </div></div>`;
  document.getElementById("np-save").onclick = async () => {
    const data = formVals($("#main"));
    if (!data.name?.trim()) return toast("Name is required");
    try {
      await jpost(`/api/project/${projectSlug}/phase/new`, data);
      toast("Phase created ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/gtm`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderEditPhase(projectSlug, phaseId){
  const p = await api(`/api/project/${projectSlug}`);
  const ph = ((p.gtm || {}).phases || []).find(x => x.phase_id === phaseId);
  if (!ph) { $("#main").innerHTML = `<div class="scroll"><p class="memo-empty">Phase not found.</p></div>`; return; }
  $("#main").innerHTML = `${pageHeader("Edit phase", _projName(projectSlug), `<button class="btn primary" id="ep-save">Save</button>`, composedIdOnly(ph.id))}
    <div class="scroll"><div class="fpage">
      ${flabel("Name")}${finput("name",ph.name,'required')}
      ${flabel("Goal")}${finput("goal",ph.goal||"")}
      ${flabel("Timebox")}${finput("timebox",ph.timebox||"")}
      ${flabel("Status")}${fsel("status",PHASE_STATUS_OPTS,ph.status||"planned")}
    </div></div>`;
  document.getElementById("ep-save").onclick = async () => {
    const data = formVals($("#main"));
    try {
      await jpost(`/api/project/${projectSlug}/phase/${phaseId}/update`, data);
      toast("Saved ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/gtm`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

function renderConfirmDeletePhase(projectSlug, phaseId){
  confirmPage("Delete phase", "Delete this phase? Strategies under it become unphased (not deleted).", async () => {
    try {
      await jpost(`/api/project/${projectSlug}/phase/${phaseId}/delete`, {});
      toast("Deleted ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/gtm`);
    } catch (e) { toast("✗ " + e.message); }
  });
}

function _phaseOpts(p){
  return [["","(unphased)"], ...(((p.gtm || {}).phases) || []).map(ph => [ph.phase_id, ph.name])];
}
function _channelOpts(slug){
  return [["","(no channel)"], ...projectChannels(slug).map(c => [c.slug, PLAT_NAME[c.platform] ? `${c.slug} · ${PLAT_NAME[c.platform]}` : c.slug])];
}

async function renderNewStrategy(projectSlug){
  const p = await api(`/api/project/${projectSlug}`);
  $("#main").innerHTML = `${pageHeader("New GTM strategy", _projName(projectSlug), `<button class="btn primary" id="ns-save">Create strategy</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">A channel-motion (e.g. "LinkedIn founder-led outbound"). Tag experiments to it to build the lens.</p>
      ${flabel("Name")}${finput("name","",'placeholder="LinkedIn founder-led outbound" required')}
      ${flabel("Phase")}${fsel("phase",_phaseOpts(p),"")}
      ${flabel("Channel")}${fsel("channel",_channelOpts(projectSlug),"")}
      ${flabel("Hypothesis")}${fta("hypothesis","",3,'placeholder="Founders reply to personalized DMs at >10%"')}
      <label class="flabel" style="display:flex;align-items:center;gap:8px;margin-top:12px"><input type="checkbox" id="ns-primary"> Primary channel-motion</label>
    </div></div>`;
  document.getElementById("ns-save").onclick = async () => {
    const data = formVals($("#main"));
    if (!data.name?.trim()) return toast("Name is required");
    data.primary = document.getElementById("ns-primary").checked;
    try {
      await jpost(`/api/project/${projectSlug}/strategy/new`, data);
      toast("Strategy created ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/gtm`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

function _findStrategy(p, stem){
  for (const ph of ((p.gtm || {}).phases || []))
    for (const st of (ph.strategies || [])) if (st.stem === stem) return st;
  return ((p.gtm || {}).unphased || []).find(st => st.stem === stem);
}

async function renderEditStrategy(projectSlug, stem){
  const p = await api(`/api/project/${projectSlug}`);
  const st = _findStrategy(p, stem);
  if (!st) { $("#main").innerHTML = `<div class="scroll"><p class="memo-empty">Strategy not found.</p></div>`; return; }
  const curPhase = ((p.gtm || {}).phases || []).find(ph => (ph.strategies || []).some(s => s.stem === stem));
  $("#main").innerHTML = `${pageHeader("Edit strategy", _projName(projectSlug), `<button class="btn primary" id="es-save">Save</button>`, composedIdOnly(st.id))}
    <div class="scroll"><div class="fpage">
      ${flabel("Name")}${finput("name",st.name,'required')}
      ${flabel("Phase")}${fsel("phase",_phaseOpts(p),curPhase ? curPhase.phase_id : "")}
      ${flabel("Channel")}${fsel("channel",_channelOpts(projectSlug),st.channel_slug||"")}
      ${flabel("Hypothesis")}${fta("hypothesis",st.hypothesis||"",3)}
      ${flabel("Status")}${fsel("status",STRAT_STATUS_OPTS,st.status||"proposed")}
      ${flabel("Notes")}${fta("notes",st.notes||"",2)}
      <label class="flabel" style="display:flex;align-items:center;gap:8px;margin-top:12px"><input type="checkbox" id="es-primary"${st.primary?" checked":""}> Primary channel-motion</label>
    </div></div>`;
  document.getElementById("es-save").onclick = async () => {
    const data = formVals($("#main"));
    data.primary = document.getElementById("es-primary").checked;
    try {
      await jpost(`/api/project/${projectSlug}/strategy/${stem}/update`, data);
      toast("Saved ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/gtm`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

function renderConfirmDeleteStrategy(projectSlug, stem){
  confirmPage("Delete strategy", "Delete this strategy? Tagged experiments keep living in Experiments (only the tag is dropped).", async () => {
    try {
      await jpost(`/api/project/${projectSlug}/strategy/${stem}/delete`, {});
      toast("Deleted ✓");
      await renderRail(); navigate(`#/project/${projectSlug}/gtm`);
    } catch (e) { toast("✗ " + e.message); }
  });
}

function renderNewProduct(projectSlug){
  const projName = (_TREE.find(p => p.slug === projectSlug) || {}).name || projectSlug;
  const typeOpts = [["app","App"],["physical","Physical"],["service","Service"]];
  $("#main").innerHTML = `${pageHeader("New product", projName, `<button class="btn primary" id="npd-save">Create product</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Name")}${finput("name","",'placeholder="Acme App" required')}
      ${flabel("Slug")}${finput("slug","",'placeholder="acme-app" required')}
      ${flabel("Type")}${fsel("type",typeOpts,"app")}
    </div></div>`;
  const n = $("#main input[name=name]"), s = $("#main input[name=slug]");
  n.oninput = () => { if (!s.dataset.manual) s.value = slugify(n.value); };
  s.oninput = () => { s.dataset.manual = s.value ? "1" : ""; };
  document.getElementById("npd-save").onclick = async () => {
    const data = formVals($("#main"));
    try {
      const out = await jpost(`/api/project/${projectSlug}/product/new`, data);
      toast(`Product created · ${out.id || ""}`);
      await renderRail(); navigate(`#/project/${projectSlug}/product`);
    } catch (e) { toast("✗ " + e.message); }
  };
}

async function renderNewFeature(productSlug, projectSlug){
  await ensureSchemas();
  const back = projectSlug ? `#/project/${projectSlug}/product` : "#/calendar";
  let fields = schemaFields("feature");
  if (projectSlug) {
    try {
      const proj = await api(`/api/project/${projectSlug}`);
      if (proj.feature?.length) fields = proj.feature;
    } catch (_) { /* global schema defaults */ }
  }
  const formHtml = fields.map(renderSchemaField).join("");
  $("#main").innerHTML = `${pageHeader("New feature", productSlug, `<button class="btn primary" id="nf-save">Add feature</button>`)}
    <div class="scroll"><div class="fpage">${formHtml}</div></div>`;
  document.getElementById("nf-save").onclick = async () => {
    const data = formVals($("#main"));
    if (!data.title?.trim()) return toast("Title is required");
    try {
      const out = await jpost(`/api/product/${productSlug}/feature/new`, data);
      toast(`Feature added · ${out.id || ""}`);
      await renderRail(); navigate(back);
    } catch (e) { toast("✗ " + e.message); }
  };
}

// ── New profile ──────────────────────────────────────────────────────────────
function renderNewProfile(projectSlug){
  const projName=(_TREE.find(p=>p.slug===projectSlug)||{}).name||projectSlug;
  $("#main").innerHTML=`${pageHeader("New profile",projName,`<button class="btn primary" id="nr-save">Create profile</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Name")}${finput("name","",'placeholder="My Brand" required')}
      ${flabel("Slug (auto-filled)")}${finput("slug","",'placeholder="my-brand" required')}
      ${flabel("Topic / niche")}${finput("topic","",'placeholder="e.g. sustainable fashion for Gen-Z"')}
    </div></div>`;
  const n=$("#main input[name=name]"), s=$("#main input[name=slug]");
  n.oninput=()=>{ if(!s.dataset.manual) s.value=slugify(n.value); };
  s.oninput=()=>{ s.dataset.manual=s.value?"1":""; };
  document.getElementById("nr-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ await jpost(`/api/project/${projectSlug}/profile/new`,data);
      OPEN.projects.add(projectSlug); if(data.slug) OPEN.profiles.add(data.slug); saveOpen();
      toast("Profile created ✓"); await renderRail(); navigate(`#/profile/${data.slug}`); }
    catch(e){ toast("✗ "+e.message); }
  };
}

// ── New channel ──────────────────────────────────────────────────────────────
function renderNewChannel(profileSlug){
  const profName=(_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===profileSlug)||{}).name||profileSlug;
  const platOpts=[["instagram","Instagram"],["tiktok","TikTok"],["x","X / Twitter"],["linkedin","LinkedIn"],["youtube","YouTube"],["facebook","Facebook"]];
  $("#main").innerHTML=`${pageHeader("New channel",profName,`<button class="btn primary" id="nc-save">Add channel</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Platform")}${fsel("platform",platOpts,"instagram")}
      ${flabel("Handle (optional)")}${finput("handle","",'placeholder="@handle"')}
    </div></div>`;
  document.getElementById("nc-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ await jpost(`/api/profile/${profileSlug}/channel/new`,data);
      OPEN.profiles.add(profileSlug); saveOpen(); toast("Channel created ✓"); await renderRail(); history.back(); }
    catch(e){ toast("✗ "+e.message); }
  };
}

// ── Channel setup (replaces gear modal) ─────────────────────────────────────
async function renderChannelSetup(channelSlug, profileSlug){
  let ch={}, gl={text:""};
  const profNode=_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===profileSlug)||{channels:[]};
  ch=(profNode.channels||[]).find(c=>c.slug===channelSlug)||{};
  try{ gl=await api(`/api/channel/${channelSlug}/guidelines`); }catch(_){}
  const platOpts=[["instagram","Instagram"],["tiktok","TikTok"],["x","X / Twitter"],["linkedin","LinkedIn"],["youtube","YouTube"],["facebook","Facebook"]];
  const crumb=profNode.name||profileSlug||"Back";
  $("#main").innerHTML=`${pageHeader(`${PLAT_NAME[ch.platform]||ch.platform||channelSlug} setup`,crumb,`<button class="btn danger-btn" id="cs-del">Delete channel</button><button class="btn primary" id="cs-save">Save</button>`, OSID.chan(channelSlug))}
    <div class="scroll"><div class="fpage">
      ${flabel("Platform")}${fsel("platform",platOpts,ch.platform||"instagram")}
      ${flabel("Handle (optional)")}${finput("handle",ch.handle||"",'placeholder="@handle"')}
      ${flabel("Voice & guidelines")}
      <p class="help">Injected into every generation for this channel.</p>
      ${fta("text",gl.text||"",10,'style="font-family:ui-monospace,Menlo,monospace;font-size:12px"')}
    </div></div>`;
  document.getElementById("cs-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ await jpost(`/api/channel/${channelSlug}/update`,{platform:data.platform,handle:data.handle});
      await jpost(`/api/channel/${channelSlug}/guidelines`,{text:data.text});
      toast("Saved ✓"); await renderRail(); history.back(); }
    catch(e){ toast("✗ "+e.message); }
  };
  document.getElementById("cs-del").onclick=()=>renderConfirmDeleteChannel(channelSlug,profileSlug);
}

// ── Add idea ─────────────────────────────────────────────────────────────────
async function renderAddIdea(slug){
  CURRENT_PROFILE_SLUG = slug;
  const profNode=_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===slug)||{channels:[]};
  const chanHint=(profNode.channels||[]).map(c=>c.slug).join(", ")||"e.g. my-profile-tiktok";
  const crumb=profNode.name||slug;
  $("#main").innerHTML=`${pageHeader("Add idea",crumb,`<button class="btn primary" id="ai-save">Add idea</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Title")}${finput("working_title","",'placeholder="short internal label"')}
      ${flabel("Concept")}${fta("concept","",3,'placeholder="what this post does and why"')}
      ${flabel("Date")}${finput("date","",'type="date"')}
      ${flabel("Pillar")}${finput("pillar","",'placeholder="e.g. Example Topic"')}
      ${flabel("Channels")}${fchannels(profNode.channels)}
    </div></div>`;
  document.getElementById("ai-save").onclick=async()=>{
    const data=formVals($("#main"));
    data.channels=checkedChannels($("#main"));
    try{ await api(`/api/profile/${slug}/posts`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
      toast("Idea added ✓"); await renderRail(); navigate(`#/profile/${slug}`); }
    catch(e){ toast("✗ "+e.message); }
  };
}

// ── Log posted ───────────────────────────────────────────────────────────────
// Manually record a post that already went out (never on the calendar): lands
// straight at published on the given date, skipping the idea→draft pipeline.
async function renderLogPosted(slug){
  CURRENT_PROFILE_SLUG = slug;
  const profNode=_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===slug)||{channels:[]};
  const chanHint=(profNode.channels||[]).map(c=>c.slug).join(", ")||"e.g. my-profile-tiktok";
  const crumb=profNode.name||slug;
  const isoDay=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  $("#main").innerHTML=`${pageHeader("Log posted",crumb,`<button class="btn primary" id="lp-save">Log posted</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">
        Record something you already posted (not on the calendar). It lands as
        Published on the date below.</p>
      ${flabel("Title")}${finput("working_title","",'placeholder="short internal label"')}
      ${flabel("Date posted")}${finput("date",isoDay(new Date()),'type="date" required')}
      ${flabel("Channels")}${fchannels(profNode.channels)}
      ${flabel("Notes")}${fta("notes","",3,'placeholder="anything worth remembering about this post"')}
    </div></div>`;
  document.getElementById("lp-save").onclick=async()=>{
    const data=formVals($("#main"));
    data.channels=checkedChannels($("#main"));
    if(!(data.date||"").trim()){ toast("✗ Date required"); return; }
    data.status="published";
    try{ await api(`/api/profile/${slug}/posts`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
      toast("Logged ✓"); await renderRail(); navigate(`#/profile/${slug}`); }
    catch(e){ toast("✗ "+e.message); }
  };
}

// ── Generate ideas ───────────────────────────────────────────────────────────
async function renderGenerateIdeas(slug){
  CURRENT_PROFILE_SLUG = slug;
  const profNode=_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===slug)||{channels:[]};
  const channels=profNode.channels||[];
  const crumb=profNode.name||slug;
  const isoDay=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  const start=new Date(), end=new Date(Date.now()+14*864e5);
  $("#main").innerHTML=`${pageHeader("Generate ideas",crumb,`<button class="btn primary" id="gi-save">${icon("zap")} Generate</button>`)}
    <div class="scroll"><div class="fpage">
      <p class="help">
        Claude will generate a batch of content ideas for this profile. Takes ~15–30s.
      </p>
      ${flabel("Period start")}${finput("period_start",isoDay(start),'type="date" required')}
      ${flabel("Period end")}${finput("period_end",isoDay(end),'type="date" required')}
      <div style="display:flex;align-items:center;gap:8px;margin:2px 0 16px">
        <input type="checkbox" id="gi-dates" style="width:16px;height:16px;cursor:pointer">
        <label for="gi-dates" class="chan-check">Assign dates to posts (spread across the period above)</label>
      </div>
      ${flabel("Platforms")}${finput("platforms",channels.map(c=>c.platform).join(","),'placeholder="tiktok,instagram"')}
      ${flabel("Cadence (posts per platform / week)")}${finput("cadence","",'placeholder="3"')}
      ${flabel("Focus (optional)")}${finput("focus","",'placeholder="push the launch"')}
    </div></div>`;
  document.getElementById("gi-save").onclick=async()=>{
    const btn=document.getElementById("gi-save"), data=formVals($("#main"));
    btn.disabled=true; btn.textContent="Generating…";
    const payload={period:`${data.period_start} to ${data.period_end}`,platforms:data.platforms,cadence:data.cadence,focus:data.focus};
    if(document.getElementById("gi-dates").checked) payload.dates = true;
    toast("⏳ Generating ideas via claude -p… (10–30s)",true);
    navigate(`#/profile/${slug}`);  // navigate away immediately; job runs in background
    try{ await api(`/api/profile/${slug}/plan`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      toast("Ideas generated ✓"); await renderRail(); if(onProfileList(slug)) await renderProfile(slug); }
    catch(e){ toast("✗ "+e.message); }
  };
}

// ── New activity / milestone / edit milestone ────────────────────────────────
async function renderNewActivity(extras={}){
  const pick=extras.entity||(_WS!=="hq"?_WS:"");
  const projectOpts=_TREE.map(p=>`<option value="${esc(p.slug)}"${pick===p.slug?" selected":""}>${esc(p.name)}</option>`).join("");
  const typeOpts=["task","meeting","call","review","launch"].map(t=>`<option>${t}</option>`).join("");
  // From the calendar a date is required (an undated activity lives in Operations, not on the calendar).
  const onCal=extras.from==="calendar";
  $("#main").innerHTML=`${pageHeader("New activity",onCal?"Calendar":"Operations",`<button class="btn primary" id="na-save">Add activity</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Title")}${finput("title","",'placeholder="e.g. Record intro video" required')}
      ${flabel("Project")}<select name="entity">${projectOpts}</select>
      ${flabel(onCal?"Date":"Date (leave empty for an undated task in Operations)")}${finput("date",extras.date||"",onCal?'type="date" required':'type="date"')}
      ${flabel("Start time (optional)")}${finput("time","",'type="time"')}
      ${flabel("End time")}${finput("time_end","",'type="time"')}
      <p class="field-help">One calendar across every workspace. Bots can't book over a timed slot; you can, with a warning.</p>
      ${flabel("Type")}<select name="type">${typeOpts}</select>
    </div></div>`;
  document.getElementById("na-save").onclick=async()=>{
    const data=formVals($("#main"));
    if(onCal&&!data.date) return toast("✗ Pick a date to put it on the calendar");
    try{
      const out=await jpost("/api/activity/new",data);
      const clash=(out.overlaps||[]).map(o=>`${o.time}-${o.time_end} ${o.title} (${o.entity})`).join(", ");
      toast(clash?`Added ✓ · overlaps ${clash}`:data.date?"Activity added ✓":"Added to Operations ✓ (no date, so not on the calendar)"); history.back();
    }
    catch(e){ toast("✗ "+e.message); }
  };
}

async function renderNewMilestone(extras={}){
  const projectOpts=_TREE.map(p=>`<option value="${esc(p.slug)}"${extras.entity===p.slug?" selected":""}>${esc(p.name)}</option>`).join("");
  $("#main").innerHTML=`${pageHeader("New milestone","Calendar",`<button class="btn primary" id="nm-save">Add milestone</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Title")}${finput("title","",'placeholder="e.g. Launch v1" required')}
      ${flabel("Date")}${finput("date",extras.date||"",'type="date" required')}
      ${flabel("Project")}<select name="entity">${projectOpts}</select>
      ${flabel("Notes")}${finput("notes",extras.notes||"",'placeholder="optional"')}
    </div></div>`;
  document.getElementById("nm-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ await jpost("/api/milestone/new",data); toast("Milestone added ✓"); history.back(); }
    catch(e){ toast("✗ "+e.message); }
  };
}

async function renderEditMilestone(ref_id, extras={}){
  $("#main").innerHTML=`${pageHeader("Edit milestone","Calendar",`<button class="btn primary" id="em-save">Save</button>`)}
    <div class="scroll"><div class="fpage">
      ${flabel("Title")}${finput("title",extras.title||"",'required')}
      ${flabel("Date")}${finput("date",extras.date||"",'type="date"')}
      ${flabel("End date (optional)")}${finput("date_end",extras.date_end||"",'type="date"')}
    </div></div>`;
  document.getElementById("em-save").onclick=async()=>{
    const data=formVals($("#main"));
    try{ await jpost(`/api/milestone/${ref_id}/update`,data); toast("Milestone updated ✓"); history.back(); }
    catch(e){ toast("✗ "+e.message); }
  };
}

// ── Confirm / delete pages ───────────────────────────────────────────────────
// A confirm as a popup overlay (like the calendar event sheet) — no navigation.
function confirmPage(title, msg, deleteFn){
  const bg=document.createElement("div"); bg.className="popup-bg";
  bg.innerHTML=`<div class="popup" id="cd-popup">
    <button class="popup-close" data-x>×</button>
    <h3>${esc(title)}</h3>
    <p class="lead">${esc(msg)}</p>
    <div class="pacts">
      <button class="btn danger-btn" id="cd-del" style="font-weight:600">Delete</button>
      <button class="btn" id="cd-cancel">Cancel</button>
    </div></div>`;
  document.body.appendChild(bg);
  function onKey(e){ if(e.key==="Escape") close(); }
  function close(){ bg.remove(); document.removeEventListener("keydown",onKey); }
  bg.onclick=e=>{ if(e.target===bg) close(); };
  bg.querySelector("#cd-popup").onclick=e=>e.stopPropagation();
  bg.querySelector("[data-x]").onclick=close;
  document.getElementById("cd-cancel").onclick=close;
  document.getElementById("cd-del").onclick=async()=>{ close(); await deleteFn(); };
  document.addEventListener("keydown",onKey);
}

async function renderConfirmDelete(id, profileSlug){
  let title=id; try{ const d=await api(postUrl(id,profileSlug)); title=(d.slot||{}).working_title||(d.slot||{}).pillar||id; }catch(_){}
  confirmPage("Delete post","Delete this post and its written content? This cannot be undone.",async()=>{
    try{ await api(postUrl(id,profileSlug,"/delete"),{method:"POST"}); toast("Deleted ✓"); await renderRail(); navigate(`#/profile/${profileSlug}`); }
    catch(e){ toast("✗ "+e.message); }
  });
}

async function renderConfirmBulkDelete(ids, profileSlug){
  const n=ids.length;
  confirmPage("Delete posts",`Delete ${n} post${n!==1?"s":""} and their written content? This cannot be undone.`,async()=>{
    try{ const r=await api("/api/posts/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids,profile:profileSlug||undefined})});
      toast(`Deleted ✓ (${r.count})`); await renderRail(); navigate(`#/profile/${profileSlug}`); }
    catch(e){ toast("✗ "+e.message); }
  });
}

async function renderConfirmDeleteProject(slug){
  let name=slug; try{ name=((await api(`/api/project/${slug}`)).entity||{}).name||slug; }catch(_){}
  confirmPage("Delete project",`Delete project "${name}" and all its files? This cannot be undone.`,async()=>{
    try{ await jpost(`/api/project/${slug}/delete`,{}); OPEN.projects.delete(slug); saveOpen();
      toast("Project deleted ✓"); await renderRail(); navigate("#/calendar"); }
    catch(e){ toast("✗ "+e.message); }
  });
}

async function renderConfirmDeleteProfile(slug){
  let name=slug; try{ name=(await api(`/api/profile/${slug}`)).name||slug; }catch(_){}
  confirmPage("Delete profile",`Delete "${name}" and all its posts? This cannot be undone.`,async()=>{
    try{ await jpost(`/api/profile/${slug}/delete`,{}); toast("Profile deleted ✓"); await renderRail(); navigate("#/calendar"); }
    catch(e){ toast("✗ "+e.message); }
  });
}

async function renderConfirmDeleteChannel(channelSlug, profileSlug){
  const profNode=_TREE.flatMap(p=>p.profiles).find(pr=>pr.slug===profileSlug)||{channels:[]};
  const ch=(profNode.channels||[]).find(c=>c.slug===channelSlug)||{};
  const name=PLAT_NAME[ch.platform]||ch.platform||channelSlug;
  confirmPage("Delete channel",`Delete the ${name} channel? Guidelines will be lost.`,async()=>{
    try{ await jpost(`/api/channel/${channelSlug}/delete`,{}); toast("Channel deleted ✓"); await renderRail(); navigate(`#/profile/${profileSlug}`); }
    catch(e){ toast("✗ "+e.message); }
  });
}

// ── AI Consultant (bottom terminal) ────────────────────────────────────────
(function(){
  const stream    = document.getElementById("chat-stream");
  const input     = document.getElementById("chat-input");
  const sendBtn   = document.getElementById("chat-send");
  const statusEl  = document.getElementById("chat-status");
  const attachBtn = document.getElementById("attach-btn");
  const fileInput = document.getElementById("file-input");
  const attachEl  = document.getElementById("attachments");
  const appEl     = document.getElementById("app");
  document.getElementById("nav-toggle").onclick = () => appEl.classList.add("nav-open");
  document.getElementById("nav-scrim").onclick = closeNav;

  // ── canvas pane: right of the chat, resizable, hideable (persisted) ────────
  const cssValue = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const cssPx = (name, fallback) => parseInt(cssValue(name, ""), 10) || fallback;
  const CANVAS_W_MIN = cssPx("--ui-shell-center-min", 440);
  const CHAT_W_MIN = cssPx("--ui-shell-assistant-min", 320);
  const CANVAS_W_DEFAULT = cssPx("--canvas-w", 540);
  // At or below this width the canvas floats over the chat (see app.css).
  const CANVAS_OVERLAY_BP = cssPx("--ui-breakpoint-canvas-overlay", 1280);
  // Like a Slack canvas: closed until you need it (the panel button, a sidebar view, or a bot
  // saving something). Only its width is remembered.
  let canvasW = CANVAS_W_DEFAULT, canvasHidden = true;
  try { canvasW = parseInt(localStorage.getItem("canvasW"), 10) || CANVAS_W_DEFAULT; } catch {}
  // Widest the canvas can go right now: keep the chat column >= CHAT_W_MIN,
  // or, as an overlay, leave a strip of chat visible.
  function canvasWMax(){
    if (window.innerWidth <= CANVAS_OVERLAY_BP) return Math.max(CANVAS_W_MIN, window.innerWidth - 40);
    const dock = document.getElementById("chat-dock");
    const canvas = document.querySelector(".right-col");
    const shared = (dock ? dock.getBoundingClientRect().width : 0)
                 + (canvas && !canvasHidden ? canvas.getBoundingClientRect().width : 0);
    return Math.max(CANVAS_W_MIN, Math.floor(shared - CHAT_W_MIN));
  }
  function applyCanvas(){
    appEl.classList.toggle("canvas-hidden", canvasHidden);
    if (typeof highlight === "function") highlight();
    appEl.style.setProperty("--canvas-w", Math.max(CANVAS_W_MIN, Math.min(canvasW, canvasWMax())) + "px");
    document.getElementById("canvas-toggle").classList.toggle("on", !canvasHidden);
    try { localStorage.setItem("canvasW", String(canvasW)); } catch {}
  }
  function showCanvas(){ if (canvasHidden) { canvasHidden = false; applyCanvas(); } }
  function hideCanvas(){ if (!canvasHidden) { canvasHidden = true; applyCanvas(); } }
  (function bindCanvasResize(){
    const handle = document.getElementById("canvas-resize");
    let dragging = false, start = 0, startSize = 0;
    const onMove = e => {
      if (!dragging) return;
      const pt = e.touches ? e.touches[0] : e;
      canvasW = Math.min(canvasWMax(), Math.max(CANVAS_W_MIN, startSize + (start - pt.clientX)));
      applyCanvas();
      e.preventDefault();
    };
    const onEnd = () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("active");
      document.body.classList.remove("chat-resizing");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onEnd);
      window.removeEventListener("touchmove", onMove);
      window.removeEventListener("touchend", onEnd);
    };
    const onStart = e => {
      const pt = e.touches ? e.touches[0] : e;
      dragging = true; start = pt.clientX;
      startSize = document.querySelector(".right-col").getBoundingClientRect().width;
      handle.classList.add("active");
      document.body.classList.add("chat-resizing");
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onEnd);
      window.addEventListener("touchmove", onMove, { passive: false });
      window.addEventListener("touchend", onEnd);
      e.preventDefault();
    };
    handle.addEventListener("mousedown", onStart);
    handle.addEventListener("touchstart", onStart, { passive: false });
  })();
  applyCanvas();
  window.addEventListener("resize", applyCanvas);
  document.getElementById("canvas-toggle").onclick = () => { canvasHidden = !canvasHidden; applyCanvas(); };

  // ── rooms (Slack channels + DMs) of the current workspace ────────────────
  // A thread is a saved chat; each room remembers its last open thread and
  // each workspace remembers its last open room. Room ids are "<ws>/<local>".
  let activeRoomId = `${_WS}/general`;
  let roomThreads = {}, wsRooms = {};
  try {
    roomThreads = JSON.parse(localStorage.getItem("roomThreads") || "{}") || {};
    wsRooms = JSON.parse(localStorage.getItem("wsRooms") || "{}") || {};
  } catch {}
  activeRoomId = wsRooms[_WS] || `${_WS}/general`;
  const roomTitleEl = document.getElementById("room-title");
  const roomEditBtn = document.getElementById("room-edit");

  function paintRoomHeader(){
    const room = roomById(activeRoomId);
    if (!room) { roomTitleEl.textContent = "# general"; return; }
    const lead = botById(room.lead);
    roomTitleEl.textContent = room.kind === "dm" ? room.name : `# ${room.name}`;
    roomEditBtn.hidden = room.kind !== "topic" || !["owner", "admin"].includes(myRole());
    input.placeholder = room.kind === "dm"
      ? `Message ${room.name}…`
      : `Message #${room.name} (untagged → ${lead?.name || "lead"}, @ to tag)`;
  }
  roomEditBtn.onclick = () => { const r = roomById(activeRoomId); if (r) { showCanvas(); navigate(`#/room/${r.name}/edit`); } };

  function selectRoom(id){
    if (busy) return busyNote();
    if (_ROOMS.length && !roomById(id)) id = `${_WS}/general`;
    activeRoomId = id;
    wsRooms[_WS] = id;
    try { localStorage.setItem("wsRooms", JSON.stringify(wsRooms)); } catch {}
    chatListEl.hidden = true;
    paintRoomHeader();
    const thread = roomThreads[id];
    let opened = Promise.resolve();
    if (!activeChatId || activeChatId !== thread) opened = thread ? openChat(thread) : Promise.resolve(newChat());
    highlight();
    return opened;
  }
  // Jump to an approval's thread (any workspace); optionally answer it.
  async function openThread(a, answer){
    if (!a || busy) return busyNote();
    roomThreads[a.room] = a.chat;
    wsRooms[a.ws] = a.room;   // so entering the workspace lands on this thread
    if (a.ws !== _WS) await window.Workspaces.switchTo(a.ws);
    else await selectRoom(a.room);
    // The room may be gone (bot switched off): never answer in another thread.
    if (activeChatId !== a.chat) { statusEl.textContent = "That thread is no longer open here"; return; }
    if (answer === "approved") send("Yes, approved.", { approval: "approved" });
  }
  window.ChatRooms = {
    select: selectRoom,
    openThread,
    newThread: () => newThread(),
    current: () => activeRoomId,
    busy: () => busy,
    showCanvas: () => showCanvas(),
    hideCanvas: () => hideCanvas(),
    // After a workspace switch: reopen that workspace's last room.
    enterWorkspace: () => selectRoom(wsRooms[_WS] || `${_WS}/general`),
    refresh: () => {
      if (_ROOMS.length && !roomById(activeRoomId)) return selectRoom(wsRooms[_WS] || `${_WS}/general`);
      paintRoomHeader();
      if (!activeChatId && !history.length && !busy) showChat([]);   // welcome now knows the room
    },
  };

  // ── saved chats: each is its own claude session (server chat_store) ─────
  // Active chat id survives reloads; its transcript is loaded from the server.
  let activeChatId = null;
  let seenCount = -1;   // messages of the open thread last shown from the server (pollThread)
  let threadUsage = { tokens: 0, cost: 0 };   // running total of the open thread
  const chatListEl = document.getElementById("chat-list");
  const chatTitleEl = document.getElementById("chat-title");

  function setActiveChat(id, title, project){
    activeChatId = id || null;
    if (!id) threadUsage = { tokens: 0, cost: 0 };
    chatTitleEl.textContent = (id && title) || "New thread";
    chatTitleEl.title = chatTitleEl.textContent;
    if (activeChatId) roomThreads[activeRoomId] = activeChatId;
    else delete roomThreads[activeRoomId];
    try { localStorage.setItem("roomThreads", JSON.stringify(roomThreads)); } catch {}
  }
  function showChat(msgs){
    history.length = 0; stream.innerHTML = "";
    if (!msgs || !msgs.length) return showWelcome();
    msgs.forEach(m => {
      history.push(m);
      const b = addMsg(m.role, m.content, m.bot, m.asked_by, m.ts, m.by, m.by_name);
      if (m.role === "assistant") decorateReply(b.closest(".msg"), m);
    });
    renderAppCards();
    stream.scrollTop = stream.scrollHeight;
  }
  // Held app actions of this thread, at its end, until answered.
  function renderAppCards(){
    stream.querySelectorAll(".app-card").forEach(el => el.remove());
    _APPROVALS.filter(a => isAppAction(a) && a.chat && a.chat === activeChatId).forEach(a => {
      const el = document.createElement("div");
      el.className = "approval-bar app-card";
      el.innerHTML = appCardHtml(a);
      wireAppCard(el, a);
      stream.appendChild(el);
    });
  }
  function busyNote(){ statusEl.textContent = "Reply in progress · Esc to stop"; }
  // Bumped by every thread switch: a slow load drops out once you moved on.
  let chatSeq = 0;
  async function openChat(id){
    if (busy) return busyNote();
    const seq = ++chatSeq;
    const rec = await fetch(`/api/chats/${encodeURIComponent(id)}`).then(r => r.ok ? r.json() : null).catch(() => null);
    if (seq !== chatSeq) return;
    if (!rec) { setActiveChat(null); showChat([]); return; }
    setActiveChat(rec.id, rec.title, rec.scope && rec.scope[0]); showChat(rec.messages);
    seenCount = (rec.messages || []).length;
    threadUsage = { tokens: rec.tokens || 0, cost: rec.cost || 0 };
    statusEl.textContent = "Ready";
  }
  function newChat(){
    if (busy) return busyNote();
    chatSeq++;
    setActiveChat(null); showChat([]); statusEl.textContent = "Ready";
    fetch("/api/chat-reset", { method: "POST" }).catch(() => {});
  }
  function relTime(ts){
    const d = (Date.now() / 1000) - (ts || 0);
    if (d < 60) return "now";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    return Math.floor(d / 86400) + "d";
  }
  async function renderChatList(){
    const { chats = [] } = await fetch(`/api/chats?room=${encodeURIComponent(activeRoomId)}`).then(r => r.json()).catch(() => ({}));
    chatListEl.innerHTML = chats.length ? chats.map(c =>
      `<div class="cl-row${c.id === activeChatId ? " active" : ""}" data-chat="${esc(c.id)}">
         <span class="cl-title">${esc(c.title || "New chat")}</span>
         ${c.tokens || c.cost ? `<span class="cl-when">${esc(fmtUsage(c.tokens, c.cost))}</span>` : ""}
         <span class="cl-when">${relTime(c.updated)}</span>
         <button class="cl-del" data-del="${esc(c.id)}" title="Delete chat" aria-label="Delete chat">${icon("x")}</button>
       </div>`).join("") : `<div class="cl-empty">No threads in this channel yet.</div>`;
  }
  chatListEl.addEventListener("click", async e => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      if (busy && del.dataset.del === activeChatId) return busyNote();
      if (!del.classList.contains("armed")) {
        // Two-step delete: first click arms, second (within 3s) deletes.
        del.classList.add("armed"); del.textContent = "Delete?";
        setTimeout(() => { del.classList.remove("armed"); del.innerHTML = icon("x"); }, 3000);
        return;
      }
      await fetch(`/api/chats/${encodeURIComponent(del.dataset.del)}/delete`, { method: "POST" }).catch(() => {});
      if (del.dataset.del === activeChatId) { setActiveChat(null); showChat([]); }
      return renderChatList();
    }
    const row = e.target.closest("[data-chat]");
    if (row) { chatListEl.hidden = true; openChat(row.dataset.chat); }
  });
  document.getElementById("chat-list-btn").onclick = async e => {
    e.stopPropagation();
    if (chatListEl.hidden) await renderChatList();
    chatListEl.hidden = !chatListEl.hidden;
  };
  // Close the list on any outside click or Esc, like other dropdowns.
  document.addEventListener("click", e => {
    if (!chatListEl.hidden && !chatListEl.contains(e.target)) chatListEl.hidden = true;
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !chatListEl.hidden) chatListEl.hidden = true;
  });
  function newThread(){ chatListEl.hidden = true; newChat(); }
  // Others write in shared threads too: re-read the open one while idle.
  let polling = false;
  async function pollThread(){
    if (polling || busy || !activeChatId || document.visibilityState !== "visible") return;
    polling = true;
    const id = activeChatId, seq = chatSeq;
    try {
      const rec = await fetch(`/api/chats/${encodeURIComponent(id)}`).then(r => r.ok ? r.json() : null).catch(() => null);
      if (!rec || busy || id !== activeChatId || seq !== chatSeq) return;
      const msgs = rec.messages || [];
      if (msgs.length !== seenCount) {
        seenCount = msgs.length;
        const atEnd = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 40;
        const keep = stream.scrollTop;
        showChat(msgs);
        if (!atEnd) stream.scrollTop = keep;
        threadUsage = { tokens: rec.tokens || 0, cost: rec.cost || 0 };
      }
    } finally { polling = false; }
  }
  setInterval(pollThread, 5000);
  document.getElementById("chat-new").onclick = newThread;
  // Pre-saved-chats builds kept one transcript in localStorage; drop it.
  try { localStorage.removeItem("chatHistory"); localStorage.removeItem("chatSessionId"); } catch {}

  // ── integrated terminal (lazy WS connect + PTY spawn on first open) ───────
  let term, termSock, termFit;
  function initTerminal(){
    term = new Terminal({ fontFamily: cssValue("--ui-font-mono", "ui-monospace, SFMono-Regular, Menlo, monospace"),
                          fontSize: cssPx("--ui-terminal-font-size", 12),
                          theme: { background: cssValue("--ui-color-terminal", "black") } });
    termFit = new FitAddon.FitAddon();
    term.loadAddon(termFit);
    term.open(document.getElementById("term"));
    termFit.fit();
    termSock = new WebSocket(`ws://${location.host}/ws/terminal`);
    termSock.binaryType = "arraybuffer";
    termSock.onopen = () => sendResize();
    termSock.onmessage = e => term.write(new Uint8Array(e.data));
    termSock.onclose = () => { if (term) term.write("\r\n[session ended — toggle the terminal to reconnect]\r\n"); };
    term.onData(d => { if (termSock && termSock.readyState === 1) termSock.send(d); });
    window.addEventListener("resize", () => { if (termFit) { termFit.fit(); sendResize(); } });
    term.attachCustomKeyEventHandler(e => {
      if (e.type !== "keydown" || !e.metaKey || e.key !== "v") return true;
      (async () => {
        try {
          const items = await navigator.clipboard.read();
          const imgItem = items.find(i => i.types.some(t => t.startsWith("image/")));
          if (imgItem) {
            const imgType = imgItem.types.find(t => t.startsWith("image/"));
            const blob = await imgItem.getType(imgType);
            const b64 = await new Promise(res => {
              const r = new FileReader(); r.onload = () => res(r.result.split(",")[1]); r.readAsDataURL(blob);
            });
            const ext = imgType.split("/")[1] || "png";
            const { path } = await jpost("/api/upload-temp", { data: b64, ext });
            if (termSock && termSock.readyState === 1) termSock.send(path);
            return;
          }
        } catch {}
        const text = await navigator.clipboard.readText().catch(() => "");
        if (text && termSock && termSock.readyState === 1) termSock.send(text);
      })();
      return false;
    });
  }
  function sendResize(){
    if (termSock && termSock.readyState === 1 && term)
      termSock.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }
  function toggleTerminal(){
    if (isPerson()) return;   // the terminal is the owner's, on this machine only
    const panel = document.getElementById("term-panel");
    const opening = panel.classList.toggle("open");
    document.getElementById("term-close").textContent = opening ? "close" : "open ⌃`";
    if (opening && !term) initTerminal();
    // fit after the height transition settles, else xterm sizes to 0 rows
    if (opening) setTimeout(() => { if (termFit) { termFit.fit(); sendResize(); } }, 160);
  }
  document.getElementById("term-tab").onclick   = e => { if (!e.target.closest("#term-close")) toggleTerminal(); };
  document.getElementById("term-close").onclick = e => { e.stopPropagation(); toggleTerminal(); };
  // Cursor-style toggle: Ctrl+`
  document.addEventListener("keydown", e => {
    if (e.ctrlKey && e.key === "`") { e.preventDefault(); toggleTerminal(); }
  });

  // ── file attachments (button · paste · drag-drop) ─────────────────────────
  let attachedFiles = [];

  async function readFile(file){
    if (file.type.startsWith("image/")) return `[image: ${file.name} — not sent to the model, referenced by name]`;
    try {
      let t = await file.text();
      if (t.indexOf(String.fromCharCode(0)) !== -1) return `[binary: ${file.name}]`;  // NUL byte => not text
      if (t.length > CTX_ATTACH_CAP) {
        t = t.slice(0, CTX_ATTACH_CAP)
            + `\n[…truncated ${t.length - CTX_ATTACH_CAP} chars — use osctl read-file for full doc]`;
      }
      return t;
    } catch { return `[binary: ${file.name}]`; }
  }

  async function refreshChatStatus(base){
    try {
      const { turn_count } = await fetch("/api/chat-session").then(r => r.json());
      const n = turn_count || 0;
      const usage = fmtUsage(threadUsage.tokens, threadUsage.cost);
      statusEl.textContent = (base || "Ready") + ` · turn ${n}` + (usage ? ` · ${usage}` : "");
    } catch {
      if (base) statusEl.textContent = base;
    }
  }
  async function addFiles(fileList){
    for (const file of fileList) {
      attachedFiles.push({ name: file.name, content: await readFile(file) });
    }
    renderAttachments();
  }

  // The + button opens the manual picker (skills · web search · attach file); file attach
  // is reachable from inside it, plus paste & drag-drop still work directly.
  attachBtn.onclick = () => openPicker();
  fileInput.onchange = async e => { await addFiles(e.target.files); fileInput.value = ""; };

  // Cmd+V of a file/image lands in clipboardData.files; plain text falls through.
  input.addEventListener("paste", e => {
    const files = e.clipboardData && e.clipboardData.files;
    if (files && files.length) { e.preventDefault(); addFiles(files); }
  });

  // Drag a file anywhere onto the composer to attach it.
  const composeEl = attachBtn.closest(".compose");
  composeEl.addEventListener("dragover", e => { e.preventDefault(); composeEl.classList.add("drag"); });
  composeEl.addEventListener("dragleave", e => { if (e.target === composeEl) composeEl.classList.remove("drag"); });
  composeEl.addEventListener("drop", e => {
    e.preventDefault(); composeEl.classList.remove("drag");
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  function renderAttachments(){
    attachEl.innerHTML = attachedFiles.map((f, i) =>
      `<span class="att-chip">${esc(f.name)}<button data-ai="${i}" title="Remove">✕</button></span>`
    ).join("");
    attachEl.querySelectorAll("[data-ai]").forEach(b =>
      b.onclick = () => { attachedFiles.splice(+b.dataset.ai, 1); renderAttachments(); }
    );
  }

  // ── @-mention autocomplete (references real entities from _TREE) ───────────
  let mentions = [];                       // {token, type, slug, name}
  let menuItems = [], menuIdx = 0;
  const menu = document.createElement("div");
  menu.className = "mention-menu"; menu.style.display = "none";
  composeEl.appendChild(menu);

  // @  → entities (project/profile/channel/post) — their content is inlined.
  // /  → skills + tools (web search, attach file) — routing tokens, not inlined.
  function mentionCandidates(trigger){
    const out = [];
    if (trigger === "/") {
      out.push({ type:"action", slug:"__attach", name:"Attach file…", meta:"upload a document" });
      out.push({ type:"tool",   slug:"web",      name:"Web search",   meta:"let the OS research online this turn" });
      for (const s of _SKILLS)
        out.push({ type:"skill", slug:s.name, name:s.name, meta:(s.description||"").slice(0, 90) });
      return out;
    }
    const members = roomById(activeRoomId)?.members || [];
    const ranked = _BOTS.filter(b => b.active).sort((a, b) => members.includes(b.id) === members.includes(a.id) ? 0 : members.includes(a.id) ? -1 : 1);
    for (const b of ranked)
      out.push({ type:"bot", id:b.id, slug:b.handle, name:b.name, meta:"@" + b.handle });
    // A company's chat only references that company (HQ sees every one).
    const tree = _WS === "hq" ? _TREE : _TREE.filter(p => p.slug === _WS);
    const ownProfiles = new Set(tree.flatMap(p => (p.profiles || []).map(x => x.slug)));
    for (const p of tree) {
      out.push({ type:"project", slug:p.slug, osId:OSID.proj(p.slug), name:p.name, meta:(p.kind||p.type||"project") });
      for (const s of PROJECT_SECTIONS)
        out.push({
          type:"section",
          slug:`${p.slug}/${s.key}`,
          osId:OSID.tabProj(p.slug, s.key),
          name:`${p.name} · ${s.label}`,
          meta:"section",
        });
      for (const prof of (p.profiles||[])) {
        out.push({ type:"profile", slug:prof.slug, osId:OSID.prof(prof.slug), name:prof.name, meta:"profile" });
        for (const ch of (prof.channels||[]))
          out.push({ type:"channel", slug:ch.slug, osId:OSID.chan(ch.slug), name:(PLAT_NAME[ch.platform]||ch.platform), meta:(ch.platform||"channel") });
      }
    }
    for (const post of _POSTS.filter(x => _WS === "hq" || ownProfiles.has(x.profile_slug))) {
      out.push({
        type:"post",
        slug:post.id,
        osId:OSID.post(post.id),
        name:(post.working_title || post.pillar || post.id),
        meta:`${post.profile_name||post.profile_slug} · ${post.status||""}`.trim(),
      });
    }
    return out;
  }
  // the @- or /-query immediately left of the caret, or null
  function activeQuery(){
    const pos = input.selectionStart;
    const before = input.value.slice(0, pos);
    const m = before.match(/(^|\s)([@/])([\w.-]*)$/);
    return m ? { q: m[3].toLowerCase(), trigger: m[2], start: pos - m[3].length - 1, end: pos } : null;
  }
  function closeMenu(){ menu.style.display = "none"; menuItems = []; menuIdx = 0; }
  function openMenu(){
    const aq = activeQuery();
    if (!aq) return closeMenu();
    menuItems = mentionCandidates(aq.trigger)
      .filter(c => c.slug.toLowerCase().includes(aq.q) || c.name.toLowerCase().includes(aq.q)
        || (c.osId||"").toLowerCase().includes(aq.q))
      .slice(0, 8);
    if (!menuItems.length) return closeMenu();
    menuIdx = Math.min(menuIdx, menuItems.length - 1);
    const ICON = { project:"folder", profile:"users", channel:"hash", post:"doc", section:"layers", skill:"zap", tool:"globe", action:"clip" };
    menu.innerHTML = menuItems.map((c, i) =>
      `<div class="mi${i===menuIdx?" sel":""}" data-i="${i}">
         <span class="mi-ic">${c.type === "bot" ? botAvatar(botById(c.id)) : icon(ICON[c.type] || "hash")}</span><b>${esc(c.name)}</b>
         <span class="mi-meta">${esc(c.type)} · ${esc(c.meta)}</span></div>`
    ).join("");
    menu.querySelectorAll(".mi").forEach(el =>
      el.onmousedown = e => { e.preventDefault(); pickMention(+el.dataset.i); });
    menu.style.display = "block";
  }
  function pickMention(i){
    const c = menuItems[i]; if (!c) return;
    const aq = activeQuery(); if (!aq) return;
    // "Attach file" action: drop the typed "/" and open the file picker.
    if (c.type === "action" && c.slug === "__attach") {
      input.value = input.value.slice(0, aq.start) + input.value.slice(aq.end);
      input.setSelectionRange(aq.start, aq.start);
      closeMenu(); fileInput.click(); input.dispatchEvent(new Event("input"));
      return;
    }
    // Skills/tools (/slug) and bots (@handle) are routing tokens read server-side,
    // never inlined, so they are NOT tracked in mentions[]. Entities use @slug and ARE tracked.
    const sigil = (c.type === "skill" || c.type === "tool") ? "/" : "@";
    const token = sigil + (c.osId || c.slug);
    const routing = c.type === "skill" || c.type === "tool" || c.type === "bot";
    input.value = input.value.slice(0, aq.start) + token + " " + input.value.slice(aq.end);
    const caret = aq.start + token.length + 1;
    input.setSelectionRange(caret, caret);
    if (!routing && !mentions.some(m => m.token === token))
      mentions.push({ token, type:c.type, slug:mentionBare(c), osId:c.osId||null, name:c.name });
    closeMenu(); input.focus();
    input.dispatchEvent(new Event("input"));   // keep textarea autosize in sync
  }
  // + button = manual picker: insert "/" at the caret and open the skill/tool menu.
  function openPicker(){
    const pos = input.selectionStart;
    const before = input.value.slice(0, pos);
    const sep = (pos === 0 || /\s$/.test(before)) ? "" : " ";
    input.value = before + sep + "/" + input.value.slice(pos);
    const caret = pos + sep.length + 1;
    input.setSelectionRange(caret, caret);
    input.focus(); openMenu();
  }
  // Runs before the send handler below; swallows nav keys while the menu is open.
  input.addEventListener("keydown", e => {
    if (menu.style.display === "none") return;
    if (e.key === "ArrowDown") { menuIdx = (menuIdx + 1) % menuItems.length; openMenu(); e.preventDefault(); e.stopImmediatePropagation(); }
    else if (e.key === "ArrowUp") { menuIdx = (menuIdx - 1 + menuItems.length) % menuItems.length; openMenu(); e.preventDefault(); e.stopImmediatePropagation(); }
    else if (e.key === "Enter" || e.key === "Tab") { pickMention(menuIdx); e.preventDefault(); e.stopImmediatePropagation(); }
    else if (e.key === "Escape") { closeMenu(); e.preventDefault(); e.stopImmediatePropagation(); }
  });
  input.addEventListener("input", openMenu);
  input.addEventListener("blur", () => setTimeout(closeMenu, 120));

  // ── chat ─────────────────────────────────────────────────────────────────
  const history = [];
  let busy = false;
  selectRoom(activeRoomId);   // restore last room + its open thread
  let chatAbort = null;   // AbortController for the in-flight /api/ask stream

  // ESC stops the current turn: abort the SSE stream client-side AND tell the
  // server to kill the claude subprocess (otherwise it keeps burning tokens).
  function stopChat(){
    if (!busy) return false;
    if (chatAbort) chatAbort.abort();
    fetch("/api/chat-stop", { method: "POST" }).catch(() => {});
    return true;
  }

  function setMsgContent(el, role, text){
    if (role === "user" || role === "assistant") el.innerHTML = formatChatText(text);
    else el.textContent = text;
  }

  // Sender line of a message row: avatar, bold name, a muted note, the time.
  function paintSender(div, { avatar, name, note = "" }){
    div.querySelector(":scope > .av").outerHTML = avatar;
    div.querySelector(".mh-name").textContent = name;
    div.querySelector(".mh-note").textContent = note;
  }
  function clock(ts){ return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""; }

  // askedBy: a teammate pulled this bot in ("asked by @cmo"); "user" = co-tagged.
  function botLabel(div, bot, handoffFrom, askedBy){
    const b = typeof bot === "string" ? botById(bot) : bot;
    if (!b) return;
    const from = handoffFrom ? botById(handoffFrom) : null;
    const asker = askedBy && askedBy !== "user" ? botById(askedBy) : null;
    paintSender(div, { avatar: botAvatar(b), name: b.name,
      note: asker ? `asked by @${asker.handle}` : from ? `from @${from.handle}` : "" });
    div.classList.toggle("consulted", !!asker);
  }

  // Under a bot reply: what it saved (one slim row each) and the approval it
  // asks for. Token use sits in the time's tooltip.
  function decorateReply(div, m){
    const main = div.querySelector(".msg-main");
    main.querySelector(".deliv-list")?.remove();
    main.querySelector(".approval-bar")?.remove();
    const usage = fmtUsage(m.tokens, m.cost);
    const via = m.engine_label ? `via ${m.engine_label}` : "";
    div.querySelector(".mh time").title = [usage && `${usage} (tokens · estimated cost)`, via].filter(Boolean).join(" · ");
    main.querySelector(".msg-via")?.remove();
    if (via && m.engine !== "claude-code") {
      const tag = document.createElement("small");
      tag.className = "msg-via";
      tag.textContent = via;
      main.appendChild(tag);
    }
    const botId = typeof m.bot === "object" ? m.bot?.id : m.bot;
    if (m.deliverables?.length) {
      const box = document.createElement("div");
      box.className = "deliv-list";
      box.innerHTML = m.deliverables.map((d, i) => `<div class="deliv-row">
          <span class="deliv-ic">${icon(KIND_META[d.kind]?.[0] || "doc")}</span>
          <span class="deliv-t"><b>${esc(d.label)}</b><small>${esc([KIND_META[d.kind]?.[2] || d.kind, d.profile && profileName(d.profile), delivChannels(d)].filter(Boolean).join(" · "))}</small></span>
          ${d.status ? `<span class="tag">${esc(d.status)}</span>` : ""}
          <button class="link-btn" data-deliv="${i}">Open in canvas</button></div>`).join("");
      box.querySelectorAll("[data-deliv]").forEach(b => b.onclick = () =>
        openDeliverable({ ...m.deliverables[+b.dataset.deliv], bot: botId }));
      main.appendChild(box);
    }
    if (m.approval && _APPROVALS.some(a => a.id === (m.approval.id || m.approval))) {
      const a = typeof m.approval === "object" ? m.approval : _APPROVALS.find(x => x.id === m.approval);
      const bar = document.createElement("div");
      bar.className = "approval-bar";
      bar.innerHTML = `<span class="ap-sum">${esc(a.summary)}</span>
        <button class="btn sm primary" data-ap="approved">Approve</button>
        <button class="btn sm" data-ap="edit">Edit first</button>
        <button class="btn sm ghost" data-ap="rejected">Skip</button>`;
      bar.querySelectorAll("[data-ap]").forEach(b => b.onclick = () => {
        if (b.dataset.ap === "edit") { input.value = "Change before saving: "; input.focus(); input.dispatchEvent(new Event("input")); return; }
        bar.remove();
        send(b.dataset.ap === "approved" ? "Yes, approved." : "No, skip it.", { approval: b.dataset.ap });
      });
      main.appendChild(bar);
    }
  }

  // One left-aligned row per message. Returns the body element (.b), which
  // streaming fills; the row is its closest(".msg").
  // Who wrote a user message: `by` = an invited person (none = the owner).
  function senderOf(by, byName){
    const me = isPerson() ? _ME.id : "ceo";
    const who = by || "ceo";
    if (who === me) return { initial: "Y", name: "You" };
    const name = who === "ceo" ? "Owner" : (byName || "Teammate");
    return { initial: name[0].toUpperCase(), name };
  }
  function addMsg(role, text, bot, askedBy, ts, by, byName){
    const div = document.createElement("div");
    div.className = "msg " + (role === "user" ? "me" : "ai");
    div.innerHTML = `<span class="av"></span><div class="msg-main"><div class="mh"><b class="mh-name"></b><span class="mh-note"></span><time></time></div><div class="b"></div></div>`;
    div.querySelector("time").textContent = clock(ts);
    if (role === "user") {
      const who = senderOf(by, byName);
      paintSender(div, { avatar: `<span class="av">${esc(who.initial)}</span>`, name: who.name });
    }
    else botLabel(div, bot || roomById(activeRoomId)?.lead || "cos", null, askedBy);
    const bubble = div.querySelector(".b");
    setMsgContent(bubble, role, text);
    stream.appendChild(div);
    stream.scrollTop = stream.scrollHeight;
    return bubble;
  }

  // While a reply streams, one quiet "working… N steps" line sits under the
  // bot's name; it goes away when the reply is complete.
  let stepLabels = [], stepEl = null, stepAnchor = null;

  function startTurnSteps(anchor){ stepLabels = []; stepEl = null; stepAnchor = anchor; }

  function addStep(text){
    stepLabels.push(text);
    if (!stepEl){
      stepEl = document.createElement("div");
      stepEl.className = "tool-chip tool-summary";
      stepAnchor.before(stepEl);   // above the answer text, inside its row
    }
    const n = stepLabels.length;
    stepEl.textContent = `working… ${n} step${n > 1 ? "s" : ""}`;
    stepEl.title = stepLabels.join("  ·  ");
    stream.scrollTop = stream.scrollHeight;
  }

  function finalizeSteps(){
    if (stepEl) stepEl.remove();
    stepEl = null; stepLabels = [];
  }

  // Token-lean context: inject heavy profile/post bodies only when the turn
  // needs them. State snapshot + osctl cover everything else.
  const CTX_VOICE_FULL = 1200, CTX_SPEC_FULL = 1500;
  const CTX_ATTACH_CAP = 12000;
  const SKILL_TAG_RE = /(?:^|\s)\/([a-z][a-z0-9-]+)/;
  const WRITE_HINT_RE = /\b(save|commit|write|create|update|fill|populate|add|generate|draft|validate|assess|design|plan|build|launch|revise|brief|memo|intake|roadmap|experiment)\b/i;
  // Marker lines the server strips (core/approvals.py MARKER_RE, server.py
  // _CONSULT_RE), hidden while a reply streams. Same shapes, so prose that
  // merely starts with "Approval" or "Consult" stays visible.
  const APPROVAL_LINE_RE = /^[ \t>*_-]*APPROVAL:.*$/gm;
  const CONSULT_LINE_RE = /^[ \t>*_-]*CONSULT[*_ \t]+@[a-z][a-z0-9-]*[*_ \t]*:.*$/gim;
  const hideMarkers = text => text.replace(APPROVAL_LINE_RE, "").replace(CONSULT_LINE_RE, "").trimEnd();
  const CONTINUATION_RE = /\b(yes|yep|yeah|ok|okay|sure|go ahead|save|commit|do it|approved|looks good|next tab|proceed|continue|save all)\b/i;
  const CTX_PORTFOLIO_KW = /\b(all posts|other posts|how many posts|list posts|what posts|calendar|timeline|projects?|profiles?|portfolio)\b/i;

  function hasExplicitSkillTag(text) {
    const tags = text.match(/(?:^|\s)\/([a-z][a-z0-9-]+)/g) || [];
    return tags.some(t => _SKILLS.some(s => s.name === t.slice(1)));
  }

  // An ongoing chat keeps its own topic: thread + @tags. The open tab is
  // background unless the message points at it ("this post", "on screen").
  const VIEW_DEIXIS_RE = /\b(?:this|the open|open)\s+(?:post|page|view|profile|tab|screen|carousel|brief|slide|draft|memo|section)\b|\bon (?:my |the )?screen\b|\bi'?m (?:looking at|viewing)\b/i;

  function viewIsPrimary(text, refs) {
    if (history.length <= 1) return true;   // fresh chat: view is the starting point
    if (VIEW_DEIXIS_RE.test(text)) return true;
    if (!CURRENT_POST) return false;
    return refs.some(m => m.type === "post" && m.slug === CURRENT_POST.id)
        || text.includes(OSID.post(CURRENT_POST.id));
  }

  function needsFullProfile(text, refs) {
    if (refs.some(m => m.type === "profile")) return true;
    if (STATE.view === "profileSetup") return true;
    return /(?:^|\s)\/content-brief\b/.test(text);
  }

  function needsFullOpenPost(text, refs) {
    if (!CURRENT_POST) return false;
    if (CTX_PORTFOLIO_KW.test(text)) return false;
    if (!/(?:^|\s)\/content-brief\b/.test(text)) return false;
    if (refs.some(m => m.type === "post")) return true;
    const openId = CURRENT_POST.id;
    if (text.includes("@" + openId) || text.includes(OSID.post(openId))) return true;
    return true;
  }

  async function profileContextBlock(slug, label, full){
    const d = await api("/api/profile/" + slug);
    const oid = OSID.prof(slug);
    const vCap = CTX_VOICE_FULL;
    const sCap = CTX_SPEC_FULL;
    let body = `id: ${oid}\ntopic: ${d.topic || "—"}`;
    if (full) {
      const [voicesRes, specsRes] = await Promise.all([
        api(`/api/profile/${slug}/voices`), api(`/api/profile/${slug}/brief-specs`),
      ]);
      const vc1 = (voicesRes.voices || []).find(v => v.id === "vc1") || {};
      const br1 = (specsRes.specs || []).find(s => s.id === "br1") || {};
      body += `\nvoice (vc1${voicesRes.voices.length > 1 ? ` — ${voicesRes.voices.length} total, get-voice --list for others` : ""}):\n${(vc1.text || "").slice(0, vCap)}`;
      body += `\nbrief-spec (br1${specsRes.specs.length > 1 ? ` — ${specsRes.specs.length} total, get-brief-spec --list for others` : ""}):\n`
           + (br1.text ? String(br1.text).slice(0, sCap) : "(empty)");
    } else {
      body += `\n(voice/spec omitted — @mention profile or ask about content to inline; else osctl get-brief-spec / get-voice / read-file)`;
    }
    return `\n## ${label} "${d.name || slug}" (${oid})\n${body}\n`;
  }

  function compactOpenPostBlock() {
    const slot = CURRENT_POST.slot || {};
    const oid = OSID.post(CURRENT_POST.id);
    return `\n## Post in view (${oid})\n`
         + `status: ${slot.status || "—"} · date: ${slot.date || "—"}\n`
         + `title: ${slot.working_title || slot.pillar || CURRENT_POST.id}\n`
         + `(full slot/brief omitted — ask about this post or @mention it; else osctl read-file)\n`;
  }

  function pastedCanonicalIds(text){
    const re = /@([a-z]{2,3}\d{1,3}(?:\.[a-z]{2,3}\d{1,3})*)/g;
    const out = [];
    let m;
    while ((m = re.exec(text)) !== null) out.push(m[1]);
    return [...new Set(out)];
  }

  function projectForTurn(){
    // The workspace decides, never the open canvas view or an @mention: a
    // company's rooms are bound to it (the server enforces this too) and HQ
    // binds none. Send only the slug; the server resolves any folder itself.
    return _WS !== "hq" && _TREE.some(p => p.slug === _WS) ? _WS : null;
  }

  async function buildContext(text){
    const title = document.querySelector(".title");
    const crumbs = document.querySelector(".crumbs");
    let ctx = "";
    const refs = mentions.filter(m => text.includes(m.token));
    const viewProj = canvasProject(true);
    const foreignView = _WS !== "hq" && viewProj && viewProj.slug !== _WS;
    if (foreignView) {   // isolation: another company's page stays out of this company's chat
      return "## Screen (background only)\nUser has a page from another workspace open. Ignore it.\n";
    }
    const viewPrimary = viewIsPrimary(text, refs);
    if (viewPrimary) {
      if (crumbs) ctx += "Current view: " + crumbs.textContent + "\n";
      if (title)  ctx += "Section: " + title.textContent + "\n";
    } else {
      const where = [crumbs?.textContent, title?.textContent].filter(Boolean).join(" · ");
      const open = CURRENT_POST ? ` (open post ${OSID.post(CURRENT_POST.id)})` : "";
      ctx += "## Screen (background only)\n"
           + `User has ${where || "another page"}${open} open. Not the topic: keep the thread's subject and @tags. Use it only if they refer to it.\n`;
    }
    const ECAP = 3;
    const entSeen = new Set();
    const entRefs = [];
    for (const m of refs) {
      if (m.type === "post") continue;
      const key = m.osId || (m.type + ":" + m.slug);
      if (entSeen.has(key)) continue;
      entSeen.add(key); entRefs.push(m);
    }
    const profileSlugs = new Set();
    const autoProfile = viewPrimary ? (CURRENT_PROFILE_SLUG || STATE.profile) : null;
    const wantFullProfile = needsFullProfile(text, refs);
    if (autoProfile) {
      try {
        ctx += await profileContextBlock(autoProfile, "Active profile", wantFullProfile);
        profileSlugs.add(autoProfile);
      } catch { /* optional */ }
    }
    for (const m of entRefs.slice(0, ECAP)) {
      let body = "";
      try {
        if (m.type === "profile") {
          if (profileSlugs.has(m.slug)) continue;
          ctx += await profileContextBlock(m.slug, "Referenced profile", true);
          profileSlugs.add(m.slug);
          continue;
        } else if (m.type === "channel") {
          const d = await api("/api/channel/" + m.slug + "/guidelines");
          body = `guidelines:\n${(d.text || "(none)").slice(0, 2000)}`;
        } else if (m.type === "project") {
          const d = await api("/api/project/" + m.slug);
          const e = d.entity || {};
          const memos = (d.memos || []).map(x => `${x.type}-v${x.version} [${x.status}]`).join(", ") || "none";
          const secs = Object.entries(d.sections || {}).map(([k, s]) =>
            `${k} id=${s.id} [${s.empty ? "empty" : (s.artifacts || []).length + " artifacts"}]`
          ).join("\n") || "none";
          body = `kind: ${e.subtype || e.type || ""} · priority: ${e.priority || "—"} · status: ${e.status || "—"}\nmemos: ${memos}\nsections:\n${secs}`;
        } else if (m.type === "section") {
          const ent = IdReg.resolve(m.osId || "");
          const projSlug = ent?.ref?.project || (m.slug || "").split("/")[0];
          const secKey = ent?.ref?.section || (m.slug || "").split("/")[1];
          const d = await api("/api/project/" + projSlug);
          const sec = (d.sections || {})[secKey];
          if (sec) {
            const arts = (sec.artifacts || []).map(a =>
              `- ${a.id || a.kind}: ${a.label}${a.path ? ` (${a.path})` : ""}`
            ).join("\n") || "(none)";
            const hint = sec.skill ? `skill:${sec.skill}` : (sec.skills || []).map(s => `skill:${s}`).join(", ");
            body = `id: ${sec.id}\nlabel: ${sec.label}\nempty: ${sec.empty}\nartifacts:\n${arts}${hint ? `\nhints: ${hint}` : ""}`;
          } else {
            body = `(section not found: ${m.osId})`;
          }
        }
      } catch { body = "(could not load content)"; }
      ctx += `\n## Referenced ${m.type} "${m.name}" (slug: ${m.slug})\n${body}\n`;
    }
    for (const m of entRefs.slice(ECAP)) {
      ctx += `\n## Referenced ${m.type} "${m.name}" (slug: ${m.slug})\n`;
    }
    // post mentions: inline full slot+brief. Dedupe (incl. the open post) and cap.
    const seen = new Set(CURRENT_POST && viewPrimary ? [CURRENT_POST.id] : []);
    const postRefs = [];
    for (const m of refs) {
      if (m.type !== "post" || seen.has(m.slug)) continue;
      seen.add(m.slug); postRefs.push(m);
    }
    const CAP = 5;
    for (const m of postRefs.slice(0, CAP)) {
      let body;
      try {
        const d = await api("/api/post/" + m.slug);
        body = "Full content:\n```json\n"
             + JSON.stringify({ slot: d.slot, brief: d.brief }, null, 2)
             + "\n```\n";
      } catch {
        body = "(could not load content)\n";
      }
      ctx += `\n## Referenced post (id: ${m.slug})\n` + body;
    }
    for (const m of postRefs.slice(CAP)) {
      ctx += `\n## Referenced post (id: ${m.slug})\n${m.name}\n`;
    }
    const pasted = pastedCanonicalIds(text).filter(id => !refs.some(m => (m.osId || m.token.slice(1)) === id));
    for (const id of pasted.slice(0, 8)) {
      const ent = IdReg.resolve(id);
      const ref = ent?.ref || {};
      if (ent?.kind === "brief" && ref.post) {
        const pid = ref.post;
        if (seen.has(pid)) continue;
        seen.add(pid);
        try {
          const d = await api("/api/post/" + pid);
          ctx += `\n## Pasted ${id} (brief)\n\`\`\`json\n`
               + JSON.stringify({ brief: d.brief }, null, 2) + "\n\`\`\`\n";
        } catch { ctx += `\n## Pasted ${id}\n(could not load)\n`; }
      } else if (ref.post && !ref.field) {
        const pid = ref.post;
        if (seen.has(pid)) continue;
        seen.add(pid);
        try {
          const d = await api("/api/post/" + pid);
          ctx += `\n## Pasted ${id}\n\`\`\`json\n`
               + JSON.stringify({ slot: d.slot, brief: d.brief }, null, 2) + "\n\`\`\`\n";
        } catch { ctx += `\n## Pasted ${id}\n(could not load)\n`; }
      } else if (ref.post && ref.field) {
        const pid = ref.post, field = ref.field;
        try {
          const d = await api("/api/post/" + pid);
          const brief = d.brief || {};
          let val = brief[field];
          if (field.startsWith("slide-")) {
            const n = parseInt(field.split("-")[1], 10);
            const slide = (brief.slide_overlays || []).find(s => (s.slide ?? 0) === n);
            val = slide ? slide.overlay : null;
          } else if (field.startsWith("gen-prompt-")) {
            const n = parseInt(field.split("-")[2], 10);
            val = (brief.gen_prompts || [])[n - 1];
          }
          ctx += `\n## Pasted ${id}\npost: ${pid}\nfield: ${field}\nvalue:\n${val != null ? String(val) : "(empty)"}\n`;
        } catch { ctx += `\n## Pasted ${id}\n(could not load)\n`; }
      } else if (ref.project && ref.section) {
        try {
          const d = await api("/api/project/" + ref.project);
          const sec = (d.sections || {})[ref.section];
          if (sec) {
            const arts = (sec.artifacts || []).map(a =>
              `- ${a.id || a.kind}: ${a.label}${a.path ? ` (${a.path})` : ""}`
            ).join("\n") || "(none)";
            ctx += `\n## Pasted ${id}\n${arts}\n`;
          }
        } catch { /* skip */ }
      }
    }
    if (CURRENT_POST && viewPrimary) {
      if (needsFullOpenPost(text, refs)) {
        ctx += `\n## Post currently open (${OSID.post(CURRENT_POST.id)})\n`
             + "Brief edits: `update-brief --id " + CURRENT_POST.id + "`. Slot fields: `update-post`. Full content:\n```json\n"
             + JSON.stringify({ slot: CURRENT_POST.slot, brief: CURRENT_POST.brief }, null, 2)
             + "\n```\n";
      } else {
        ctx += compactOpenPostBlock();
      }
    }
    if (attachedFiles.length) {
      ctx += "\n## Attached files\n" + attachedFiles.map(f =>
        `### ${f.name}\n\`\`\`\n${f.content}\n\`\`\``
      ).join("\n\n");
    }
    return ctx;
  }

  async function send(override, extra){
    const text = (typeof override === "string" ? override : input.value).trim();
    if (!text || busy) return;
    busy = true;
    if (typeof override !== "string") input.value = "";
    const hadPending = _APPROVALS.some(a => a.chat === activeChatId);
    let heldThisTurn = false;
    stream.querySelectorAll(".approval-bar:not(.app-card)").forEach(el => el.remove());   // any reply answers it; app cards stay
    input.style.height = "";
    statusEl.textContent = "thinking…";
    sendBtn.disabled = true;

    const fileNote = attachedFiles.length ? ` [+${attachedFiles.length} file${attachedFiles.length>1?"s":""}]` : "";
    history.push({ role: "user", content: text });
    const userEl = addMsg("user", text + fileNote, null, null, Date.now() / 1000).closest(".msg");

    const projectSlug = projectForTurn(text);
    const ctx = await buildContext(text);
    attachedFiles = []; renderAttachments();
    mentions = [];

    // One message per bot this turn: the lead, teammates it pulls in, its follow-up.
    const replies = [];
    let bubble, full = "", turnMeta;
    const startReply = info => {
      if (bubble) {
        finalizeSteps();
        if (full) history.push({ role: "assistant", content: full });
        decorateReply(bubble.closest(".msg"), turnMeta);
        bubble.closest(".msg").classList.remove("streaming");
      }
      bubble = addMsg("assistant", "", null, null, Date.now() / 1000); bubble.innerHTML = ""; full = ""; turnMeta = {};
      bubble.closest(".msg").classList.add("streaming");   // typing dots until text arrives
      replies.push({ bubble, meta: turnMeta });
      startTurnSteps(bubble);
      if (info) { botLabel(bubble.closest(".msg"), info, null, info.asked_by || null); turnMeta.bot = info.id; }
    };
    startReply(null);
    const replyAt = i => (Number.isInteger(i) && replies[i]) || replies[replies.length - 1];
    let pendingBreak = false;

    chatAbort = new AbortController();
    try {
      const payload = { messages: [{ role: "user", content: text }], context: ctx, ...(extra || {}) };
      if (projectSlug) payload.project_slug = projectSlug;
      if (activeChatId) payload.chat_id = activeChatId;
      payload.room_id = activeRoomId;
      const resp = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: chatAbort.signal
      });

      if (!resp.ok) {
        const j = await resp.json().catch(() => ({}));
        bubble.innerHTML = formatChatText("Error: " + (j.error || resp.status));
        history.pop(); return;
      }

      const reader = resp.body.getReader();
      const dec = new TextDecoder(); let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6).trim();
          if (raw === "[DONE]") break;
          try {
            const obj = JSON.parse(raw);
            if (obj.chat_id) {
              // Server started a different chat (project scope changed): old one stays saved.
              if (activeChatId && obj.chat_id !== activeChatId) {
                // Show only what the new chat holds; the old chat is still in Threads.
                while (stream.firstChild && stream.firstChild !== userEl) stream.removeChild(stream.firstChild);
                history.splice(0, history.length - 1);
                const note = document.createElement("div");
                note.className = "tool-chip";
                note.textContent = `↺ New chat for ${obj.project || "all projects"}. Previous chat saved in Threads`;
                stream.insertBefore(note, userEl);
              }
              setActiveChat(obj.chat_id, obj.title, obj.project);
            }
            if (obj.bot) { botLabel(bubble.closest(".msg"), obj.bot, obj.bot.handoff_from); turnMeta.bot = obj.bot.id; }
            if (obj.msg_start) { startReply(obj.msg_start); pendingBreak = false; }
            if (obj.deliverables) replyAt(obj.msg).meta.deliverables = obj.deliverables;
            if (obj.usage) {
              turnMeta.tokens = obj.usage.tokens; turnMeta.cost = obj.usage.cost;
              turnMeta.engine = obj.usage.engine; turnMeta.engine_label = obj.usage.engine_label;
              threadUsage.tokens += obj.usage.tokens || 0; threadUsage.cost += obj.usage.cost || 0;
            }
            if (obj.clean !== undefined) {
              const r = replyAt(obj.msg);
              r.bubble.innerHTML = formatChatText(obj.clean);
              if (r.bubble === bubble) full = obj.clean;
            }
            if (obj.approval) { _APPROVALS.unshift(obj.approval); replyAt(obj.msg).meta.approval = obj.approval; }
            if (obj.app_actions) {
              obj.app_actions.forEach(a => { if (!_APPROVALS.some(x => x.id === a.id)) _APPROVALS.unshift(a); });
              heldThisTurn = true;
            }
            if (obj.error) { bubble.innerHTML = formatChatText("Error: " + obj.error); reader.cancel().catch(() => {}); return; }
            if (obj.delta) {
              // A tool call ran between two narration segments — start the next
              // one on a fresh line so thoughts don't run together ("week.14").
              if (pendingBreak && full && !full.endsWith("\n")) full += "\n";
              pendingBreak = false;
              full += obj.delta; bubble.innerHTML = formatChatText(hideMarkers(full)); stream.scrollTop = stream.scrollHeight;
            }
            if (obj.tool)  { addStep(obj.tool); pendingBreak = true; }
          } catch {}
        }
      }

      if (full) history.push({ role: "assistant", content: full });
      await refreshChatStatus("Ready");
    } catch(e) {
      if (e.name === "AbortError") {
        bubble.innerHTML = formatChatText(full ? full + "  ⏹ stopped" : "⏹ stopped");
        if (full) history.push({ role: "assistant", content: full });
        statusEl.textContent = "Stopped";
      } else {
        bubble.innerHTML = formatChatText("Error: " + e.message);
        history.pop(); statusEl.textContent = "Error";
      }
    } finally {
      chatAbort = null;
      finalizeSteps();                // the reply is complete: the steps line goes
      busy = false; sendBtn.disabled = false; input.focus();
      refreshViews().catch(()=>{});   // act directly → show result
      replies.forEach(r => { const row = r.bubble.closest(".msg"); row.classList.remove("streaming"); decorateReply(row, r.meta); });
      // You approved and the teammate saved it: show you the result.
      const saved = replies.flatMap(r => r.meta.deliverables || []);
      if (extra?.approval === "approved" && saved.length)
        openDeliverable({ ...saved[0], bot: replies.find(r => r.meta.deliverables?.length)?.meta.bot });
      if (hadPending || heldThisTurn || replies.some(r => r.meta.approval)) renderRail().catch(() => {});   // inbox count
      renderAppCards();
    }
  }

  sendBtn.onclick = send;
  input.addEventListener("keydown", e => {
    if (e.key === "Escape" && busy) { e.preventDefault(); e.stopPropagation(); stopChat(); return; }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  // Global ESC also stops a streaming turn (when focus isn't in the textarea).
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && busy) stopChat();
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });

  function showWelcome(){
    const room = roomById(activeRoomId);
    const lead = botById(room?.lead || "cos");
    const wsName = wsById(_WS)?.name || "HQ";
    const hello = room?.kind === "dm"
      ? `Hey, ${lead?.name || "I"} here. What do you need?`
      : _WS === "hq"
        ? `Morning. I keep the view across all your companies. Want the weekly review, or something specific?`
        : `Hi, ${lead?.name || "Chief of Staff"} for ${wsName}. Tell me what you need, I'll handle it or pull in the right person.`;
    addMsg("assistant", hello, lead?.id);
    const SUGS = {
      cos: ["Weekly review", "/weekly-review "], product: ["Design a test", "@product /experiment-design "],
      cmo: ["Positioning", "@cmo /positioning "], gtm: ["Launch plan", "@gtm /launch-plan "],
      social: ["Plan content", "@social /content-plan "], research: ["Competitors", "@research /competitor-scan "],
      cto: ["How the app works", "@cto "],
    };
    const ids = room?.members?.length ? room.members : Object.keys(SUGS);
    const cmds = ids.map(id => SUGS[id]).filter(Boolean).slice(0, 4);
    const wrap = document.createElement("div");
    wrap.className = "sugs";
    cmds.forEach(([label, cmd]) => {
      const b = document.createElement("button");
      b.className = "sug";
      b.type = "button";
      b.textContent = label;
      b.onclick = () => { input.value = cmd; input.focus(); input.dispatchEvent(new Event("input")); };
      wrap.appendChild(b);
    });
    stream.appendChild(wrap);
    stream.scrollTop = stream.scrollHeight;
  }
})();

boot();
