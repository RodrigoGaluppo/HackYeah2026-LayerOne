# LayerOne promotional site

Static GitHub Pages landing page for the LayerOne HackYeah 2026 MVP.

Public URL: https://rodrigogaluppo.github.io/HackYeah2026-LayerOne/

## Preview

From the repository root:

```sh
python3 -m http.server 8080 --directory docs
```

Visit `http://localhost:8080`. There is no build step or runtime dependency.

## Update and publish

Edit `docs/index.html`, `docs/styles.css`, or `docs/app.js`. Commit the changes
on `main`, then publish the contents of `docs` to the Pages branch:

```sh
git push origin main
git subtree push --prefix docs origin gh-pages
```

GitHub Pages should use **Deploy from a branch**, branch **gh-pages**, folder
**/ (root)** under repository **Settings → Pages**. Once configured, a push to
that branch triggers GitHub's Pages build. Do not edit `gh-pages` independently;
the source of truth is `docs` on `main`.

## Film and assets

- `assets/layerone-film.mp4`: the supplied `LayerOne-Energy-Polished-v2.mp4`,
  remuxed without re-encoding to place MP4 playback metadata before the media.
- `assets/captions-en.vtt`: English cues extracted from the supplied film's
  HTML source with its timing scale applied. The video also has burned-in captions.
- `assets/film-poster.jpg`: a frame from the supplied film.
- `assets/solar-field.svg` and `assets/mark.svg`: native vector artwork.
- `assets/social.jpg`: landing-page preview for social sharing.

All functional assets are hosted with the site. Google Fonts is an optional
typographic enhancement; local system fonts are the fallback.

The page does not connect to the Pi dashboard, collect telemetry, or present
simulated measurements as live. Its signal illustration is labeled. The film's
future hardware-key concept is distinguished from the implemented SQLite MVP.

## Browser checks

Validated in Chromium at widths 320, 375, 390, 768, 1024, and 1440 pixels.
Checks cover horizontal overflow, video playback, caption track availability,
signal controls, keyboard-operated protocol tabs, local anchors, and script errors.
