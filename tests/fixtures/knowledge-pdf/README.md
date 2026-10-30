# Synthetic PDF geometry fixtures

These tiny vector PDFs test browser byte verification and rendering geometry only.
They provide no field-document, OCR, numerical-accuracy or real-service acceptance.

Each has a blank first page and a second page with a red rectangle at PDF coordinates
`[30, 280, 120, 320]` and a separate blue rectangle. The ordinary top-left locator is
`[30, 80, 120, 120]` on a 300 × 400 point page.

- `plain.pdf`: MediaBox and CropBox `[0, 0, 300, 400]`, rotation 0. Highlight is permitted.
- `rotated.pdf`: same boxes, rotation 90. The real PDF rotates; the current parser locator
  does not declare its rotation convention, so the UI deliberately renders page-only.
- `cropped.pdf`: CropBox `[20, 40, 280, 380]`. The real crop renders; nonzero crop origin
  is deliberately page-only because the locator has no crop-origin contract.

`generate-fixtures.mjs` documents their exact deterministic construction and refuses
to overwrite existing files. No external fonts, network resources or scripts are present.
