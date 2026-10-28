# mustache.js 4.2.0 in Kabala

Source: https://github.com/janl/mustache.js (MIT, see `LICENSE`): `mustache.mjs` from the npm package.
Mustache templates are "logic-less": no code can run inside them, only `{{name}}` placeholders. That makes them safe for text an admin edits in the panel.
The first commit that added this folder is the original; changes since are marked `// Kabala:`.

| Change | Why |
|---|---|
| The template cache keeps at most 200 templates (oldest dropped) | The original cache grew forever, and the admin panel's live preview renders a new template on every keystroke |
| `mustache.d.mts` added | Types for TypeScript |

Used through `src/receipts/templates.ts`, which allows only plain `{{variable}}` tags from a fixed list (no sections, partials, unescaped `{{{ }}}` or changed delimiters) and refuses anything else with a clear message.
