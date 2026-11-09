# Workspace logos

## Intent

Each workspace (HQ and every project) gets its own picture in the workspace
bar and at the top of the sidebar, so a company's logo marks its workspace.
The Leo Bot logo follows the graphite and indigo palette of the 2026-10-24
redesign.

## Behavior

- **Defaults.** HQ shows the Leo Bot logo; a project shows its initials on its
  hue, as before.
- **Upload.** "Workspace logo" in the sidebar's workspace menu, or "Change
  logo" in Project settings, opens a page with a preview, Upload image, and
  Remove (HQ: "Use the Leo Bot logo"). PNG, JPEG or WebP.
- The browser redraws the picked image on a 256px square canvas and sends that
  PNG, so the stored file is small and carries no camera metadata (EXIF
  location).
- Logos sit on a white tile (`--ui-color-logo-bg`) in both themes, since most
  marks are dark ink on transparent.
- Deleting a project deletes its logo.

## How

- `core/ws_logos.py` stores `team/logos/<ws>.<ext>` (folder 0700, file 0600,
  gitignored with `team/`). The type comes from the file's magic bytes, never
  from the client: PNG, JPEG, WebP only. SVG is refused because it can carry
  script. 512 KB cap. Workspace ids follow the project slug rule.
- `GET /api/workspaces` adds `logo`: a version token (file mtime) or null; the
  UI uses it to cache-bust `GET /api/workspace-logo/<ws>`.
- `GET /api/workspace-logo/<ws>` serves only known workspaces, with
  `Content-Security-Policy: default-src 'none'; sandbox` and the server-wide
  `nosniff`.
- `POST /api/workspace-logo {ws, data}` (base64) and
  `POST /api/workspace-logo/remove {ws}` need the session cookie and a
  same-origin `Origin`, like the integrations endpoints.
- `LeoBot.app`'s `AppIcon.icns` is rendered from `dashboard/logo.svg`;
  `scripts/ensure-app.sh` re-signs the app when the icon changes.
