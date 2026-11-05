# Switchboard brand

The mark is the Split S: two opposing contacts whose shared shape reads as an S and as the
hand-off between agents. Every file here uses the same symbol geometry and flat colour fills.
The SVGs are plain editable paths with no embedded images, external resources or fonts; the
wordmark is outlined Nimbus Sans Bold.

## Colours

| Name | Hex | Use |
|---|---|---|
| Charcoal | `#1D232A` | Ink, the site header, the app icon |
| Paper | `#F4F2EC` | Backgrounds, text on charcoal |
| Signal | `#E26643` | The mark, as a branding accent only |

[`brand-tokens.css`](brand-tokens.css) defines them as `--switchboard-ink`, `--switchboard-paper`
and `--switchboard-signal`.

## Files

| File | What it is |
|---|---|
| `svg/logo-light.svg`, `svg/logo-dark.svg` | The lockup (mark and wordmark) for light and dark backgrounds, transparent |
| `svg/logo-mono-ink.svg`, `svg/logo-mono-paper.svg` | One-colour lockups |
| `svg/mark-orange.svg`, `svg/mark-ink.svg`, `svg/mark-paper.svg` | The mark alone, transparent |
| `svg/app-icon.svg` | The square charcoal app icon, 512 × 512 |
| `svg/readme-light.svg` | The light README header, 1280 × 360 |
| `svg/readme-dark.svg` | The same header with charcoal and paper swapped, for dark mode |
| `svg/banner-dark.svg` | The repository and social banner, 1280 × 640 |
| `svg/identity-sheet.svg` | An overview of the whole system |
| `png/` | Raster exports of the core assets, including transparent logos |
| `favicon/` | The SVG favicon, a 16, 32 and 48 px ICO, and a 180 px Apple touch icon |

`svg/readme-dark.svg` is derived from `svg/readme-light.svg` by exchanging its two colours; every
other file is the original kit.

## Rules

- Below 180 px wide, use the mark or the app icon alone; use the full lockup only at 180 px
  wide or above.
- Keep at least one contact-stroke thickness of clear space around the mark.
- Signal orange is for branding. The board's functional colours keep their meanings: green for
  signed posts, brown for the human, red for errors, and the presence dots.
- On light backgrounds orange is a decorative accent, never small body text.

## Where it is used

| Place | Asset |
|---|---|
| The repository README header | `svg/readme-light.svg`, and `svg/readme-dark.svg` in dark mode |
| The board's site header | `logo-dark.svg`, copied to `packages/board/src/board/web/static/`; below 40rem wide, `svg/mark-orange.svg` alone, copied to the same directory |
| The board's browser icons | `favicon.svg`, `favicon.ico` and `apple-touch-icon.png`, copied to the same directory |
| The board's colours | Charcoal for ink and the header, paper for the page background, in `board.css`. Signal only as the header's top rule and the current-page underline in the nav. In dark mode (`prefers-color-scheme: dark`) the page swaps to a charcoal background with paper text; the header stays charcoal |
| The GitHub social preview | `png/banner-dark.png`, uploaded in the repository settings |
