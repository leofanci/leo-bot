---
name: tokensave
description: Inspect the workspace's first indexed linked folder through fresh, bounded TokenSave context supplied by myOS. Read-only and explicitly opt-in.
---

# TokenSave linked-app inspection

Use only for the workspace's linked folders: myOS queries the first one with an index.
This skill is read-only; use folder-work to change code.

1. Use the `Fresh TokenSave context` already included in this turn. myOS
   incrementally refreshed and queried that folder exactly once before the
   model started.
2. Use Glob/Grep and Read (maximum 200 relevant lines at a time) only when the
   supplied graph result is insufficient and those tools are available.
3. Summarize the answer; do not paste the entire graph response.

Never invoke TokenSave commands directly. myOS handles its bounded incremental
refresh outside the model turn. If no linked folder has an index or refresh fails,
myOS stops `/tokensave` before starting the model and reports the problem.
