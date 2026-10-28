# validator.js 13.15.35 (isEmail) in Kabala

Source: https://github.com/validatorjs/validator.js (MIT, see `LICENSE`): `isEmail` and the files it imports, copied from the npm package's `es/lib` build.
The first commit that added this folder is the original; changes since are marked `// Kabala:`.

| Change | Why |
|---|---|
| Every relative import ends in `.js` | Node's ES modules require the extension; the original only worked through a bundler |
| `isEmail.d.ts` added | Types for TypeScript (validator ships them in a separate `@types` package) |

Used through `src/email/address.ts`, which turns on stricter options than validator's defaults for addresses people type: no display names, no IP domains, and no `,` `;` `<` `>` `"` characters (receipt recipients are stored comma-joined).
