// Synthetic geometry fixtures, not scanned field documents or OCR accuracy evidence.
import { writeFile } from "node:fs/promises";

function pdf({ rotation = 0, crop = "0 0 300 400" } = {}) {
  const painted = "q 0.8 0.1 0.1 rg 30 280 90 40 re f 0.1 0.3 0.8 rg 180 60 50 30 re f Q\n";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> /Contents 4 0 R >>",
    "<< /Length 4 >>\nstream\nq Q\nendstream",
    `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /CropBox [${crop}] /Rotate ${rotation} /Resources << >> /Contents 6 0 R >>`,
    `<< /Length ${Buffer.byteLength(painted)} >>\nstream\n${painted}endstream`,
  ];
  let body = "%PDF-1.7\n% SYNTHETIC GEOMETRY ONLY\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(body));
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = Buffer.byteLength(body);
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) body += `${String(offset).padStart(10, "0")} 00000 n \n`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF\n`;
  return body;
}

for (const [name, options] of [
  ["plain", {}],
  ["rotated", { rotation: 90 }],
  ["cropped", { crop: "20 40 280 380" }],
]) {
  await writeFile(new URL(`${name}.pdf`, import.meta.url), pdf(options), { flag: "wx" });
}
