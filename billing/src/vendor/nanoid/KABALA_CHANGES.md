# nanoid 5.1.16 in Kabala

Source: https://github.com/ai/nanoid (MIT, see `LICENSE`), copied from the npm package (`index.js`, `index.d.ts`, `url-alphabet/`).
nanoid generates unguessable IDs from the operating system's secure random generator, with no bias between letters.
The first commit that added this folder is the original; changes since are marked `// Kabala:`.

| Change | Why |
|---|---|
| `customRandom` zeroes each random byte after turning it into a character | The bytes behind a license key are secret; nanoid keeps a shared random pool in memory, so used bytes are wiped instead of lingering there |

Used by `src/licenses/keys.ts`: license keys `KBL-XXXXX-XXXXX-XXXXX-XXXXX` from the Crockford base32 alphabet (no I, L, O, U, so no 0/O or 1/I mix-ups), 100 random bits.
