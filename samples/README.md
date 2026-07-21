# Sample documents

Generated invoices, committed so the rendering can be reviewed without running a
job. Every one is **synthetic end to end** — issuers, buyers, addresses, line
items and totals all come from the procedural seed catalogue. There is no
personal data here, and none of these correspond to a real company or invoice.

Regenerate them by rendering with the invoice pack; filenames encode what each
one exercises:

```
<nn>_<archetype>_<locale>_<business>_<currency>_<render flags>.pdf
```

What the set covers:

- **Archetypes** — `meta-sidebar-01`, `boxed-form-01`, `receipt-compact-01`,
  `telecom-itemized-37`, `banner-header-06`, `fullbleed-05`, and the
  colour-forward variants.
- **Locales / jurisdictions** — `en-US`, `en-GB`, `fr-FR`, `fr-CA` (Québec's two
  tax buckets), with the matching currency and label vocabulary.
- **Typography** — including `38_bundled-noto-serif` and
  `39_bundled-jetbrains-mono`, which render from the bundled OFL fonts embedded
  as base64 `@font-face` (byte-identical on any machine).
- **Pagination** — `40_telecom-contd_en_multipage.pdf` is an 11-page itemised
  bill; page 2 shows the `(cont'd)` marker on a section that spans a page break.
- **Capture conditions** — `41`–`44` are the same invoice as `clean`,
  `light-scan`, `heavy-scan`, and `handwritten`, produced by the degradation
  post-processor. The degraded ones are image-only PDFs with no text layer, so
  OCR has to read the pixels.
