# chevron 0.14.0 in Kabala

Source: https://github.com/noahmorrison/chevron (MIT, see `LICENSE`), copied from the PyPI wheel. chevron is mustache for Python: the same template language as mustache.js on the PayPal billing server, so a receipt template like `קבלה {{number}}` means the same on both servers.
The first commit that added this folder is the original; changes since are marked `# Kabala:`.

| Change | Why |
|---|---|
| Partials come only from `partials_dict`; the file system is never read (`partials_path` defaults to `None` and is ignored) | The original read `path.join(partials_path, name)` with `partials_path='.'` by default, so a template saying `{{> ../../some/file}}` could put a server file into an email |
| `main.py` (the command-line tool, which reads template files) removed | Not used, and it reads files |
| `render(..., escape_html=False)` | Plain text: a mail subject, or text the caller escapes itself (the receipt email's HTML) |

Used through `kabala_server/receipt_templates.py`, which on top of that allows only plain `{{variable}}` tags from a fixed list.
