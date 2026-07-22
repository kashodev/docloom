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
- **Capture conditions** — `41`–`43` are the same invoice as `clean`,
  `light-scan` and `heavy-scan`, produced by the degradation post-processor. The
  degraded ones are image-only PDFs with no text layer, so OCR has to read the
  pixels.
- **Handwritten** — `44`–`46` use the `handwritten-form-01` archetype: a
  pre-printed pad with ruled item lines, filled in by hand. Every value — dates,
  line items, quantities, prices, subtotal, tax and total — is rendered in a
  bundled OFL handwriting face with per-field jitter, over printed chrome, and
  signed and stamped in roughened ink, then scanned. The stamp is the issuer's
  own **official seal** — a procedural SVG carrying its registered name, town and
  registration number, circular/oval/rectangular, in red, blue or violet pad ink,
  landing somewhere plausible on the page at a hand-pressed angle. `45` is the
  neatest hand
  (in French) and `46` the messiest, showing the legibility dial: the same
  invoice as `44`, much harder to read. `46` also shows the **wear** dial at its
  worn default; `47_handwritten_crisp.pdf` is the same kind of document at
  `wear = 0` — a well-preserved artefact, sharp and legible, though the seal
  edges and pen strokes are still not vector-clean, because real ink never is.
  `48_handwritten_goods-receipt.pdf` is the delivery-note variant: under the
  issuer's signature sits a **received by / print name / date received** block
  that the customer signs on taking delivery, in a second, scrawlier hand. That
  variant is always physical goods — nobody signs a delivery note for a month of
  consulting — which the record validates. The figures are identical to the clean
  twin — handwriting changes the rendering, never the ground truth.
