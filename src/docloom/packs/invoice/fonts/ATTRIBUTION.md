# Bundled fonts

Four OFL fonts are bundled (weights 400 and 700, Latin subset, `.woff2`) so
that documents using their typeface keys render byte-identically on any machine,
independent of what fonts the host has installed. All are redistributed under
the **SIL Open Font License, Version 1.1** — full text and copyright notices in
[OFL.txt](OFL.txt).

| Typeface key    | Family        | Source | License |
|-----------------|---------------|--------|---------|
| `serif-classic` | Noto Serif    | https://github.com/notofonts/latin-greek-cyrillic | OFL 1.1 |
| `sans-neutral`  | Inter         | https://github.com/rsms/inter | OFL 1.1 |
| `slab`          | Zilla Slab    | https://github.com/mozilla/zilla-slab | OFL 1.1 |
| `mono-invoice`  | JetBrains Mono| https://github.com/JetBrains/JetBrainsMono | OFL 1.1 |

The `.woff2` files in [files/](files/) are the Latin-subset builds distributed by
[Fontsource](https://fontsource.org/). The other typeface keys in
[`__init__.py`](__init__.py) have no bundled file and render from their semantic
fallback stack.

To add or refresh a bundled font: drop `<key>-400.woff2` / `<key>-700.woff2`
into `files/`, add the key → family mapping to `BUNDLED`, and record the
copyright notice in `OFL.txt` and the row above.
