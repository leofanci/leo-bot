"""app_preview.py — what an approval card shows for a held app action.

Built by the server from the real arguments, never from the bot's words: an
email can carry injected instructions, so the card shows exactly what will run.
Tailored labels for Gmail, Google Calendar and Drive; every other tool gets its
name in words plus every argument. Nothing is truncated or summarized.
"""
import json

# slug -> (title, [(label, candidate argument keys), ...])
_MAIL = [("To", ("recipient_email", "to")), ("Cc", ("cc",)), ("Bcc", ("bcc",)),
         ("Subject", ("subject",)), ("Body", ("body", "message_body")),
         ("Attachments", ("attachment", "attachments")), ("Thread", ("thread_id",))]
_EVENT = [("Title", ("summary", "title")), ("Start", ("start_datetime", "start_time", "start")),
          ("End", ("end_datetime", "end_time", "end")), ("Duration (h)", ("event_duration_hour",)),
          ("Attendees", ("attendees",)), ("Location", ("location",)),
          ("Description", ("description",)), ("Time zone", ("timezone",)), ("Event", ("event_id",))]
_FILE = [("File", ("file_id", "file_name", "file_to_upload", "name")),
         ("Folder", ("folder_id", "folder_to_upload_to", "parent_id")),
         ("With", ("email_address", "email", "emails")), ("Role", ("role",)), ("Type", ("type",))]
TAILORED = {
    "GMAIL_SEND_EMAIL": ("Send email", _MAIL),
    "GMAIL_REPLY_TO_THREAD": ("Reply to email", _MAIL),
    "GMAIL_CREATE_EMAIL_DRAFT": ("Create email draft", _MAIL),
    "GOOGLECALENDAR_CREATE_EVENT": ("Create calendar event", _EVENT),
    "GOOGLECALENDAR_UPDATE_EVENT": ("Update calendar event", _EVENT),
    "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE": ("Share Drive file", _FILE),
    "GOOGLEDRIVE_UPLOAD_FILE": ("Upload to Drive", _FILE),
    "GOOGLEDRIVE_TRASH_FILE": ("Move Drive file to trash", _FILE),
    "GOOGLEDRIVE_DELETE_FILE": ("Delete Drive file", _FILE),
}


def _fmt(value, join=False):
    """Strings in full; a tailored field joins a list of strings (recipients)."""
    if isinstance(value, str):
        return value
    if join and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return ", ".join(value)
    return json.dumps(value, indent=2, ensure_ascii=False)


def _title(slug, toolkit):
    tk = toolkit or slug.split("_", 1)[0].lower()
    action = slug[len(tk) + 1:] if slug.upper().startswith(tk.upper() + "_") else slug
    return f"{tk.replace('_', ' ').capitalize()}: {action.replace('_', ' ').lower()}"


def build(slug, arguments, toolkit=None, account=None):
    """{"title", "fields": [[label, value], ...]} for one held tool call; the
    account it runs as comes first, so two Gmails never get mixed up."""
    title, spec = TAILORED.get(slug, (None, []))
    title = title or _title(slug, toolkit)
    head = [["Account", account]] if account else []
    if not isinstance(arguments, dict):
        return {"title": title, "fields": head + [["Arguments", _fmt(arguments)]]}
    fields, used = list(head), set()
    for label, keys in spec:
        for k in keys:
            if k in arguments and arguments[k] not in (None, "", []):
                fields.append([label, _fmt(arguments[k], join=True)])
                used.add(k)
                break
    fields += [[k, _fmt(v)] for k, v in arguments.items() if k not in used]
    return {"title": title, "fields": fields}
