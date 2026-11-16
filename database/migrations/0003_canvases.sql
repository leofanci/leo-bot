-- Canvases (documents of blocks) and lists (tables), one row per file under
-- projects/<ws>/canvases/cvN.json and projects/<ws>/lists/lsN.json (HQ: team/canvases,
-- team/lists, ws 'hq'). Ids repeat across workspaces, so (ws, id) is the key.
CREATE TABLE canvases (
  id         TEXT NOT NULL,
  ws         TEXT NOT NULL,
  title      TEXT,
  updated    TEXT,
  created_by TEXT,
  PRIMARY KEY (ws, id)
);

CREATE TABLE canvas_lists (
  id      TEXT NOT NULL,
  ws      TEXT NOT NULL,
  title   TEXT,
  "rows"  INTEGER NOT NULL DEFAULT 0,
  updated TEXT,
  PRIMARY KEY (ws, id)
);
