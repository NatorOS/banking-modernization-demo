// Inline the built JS and CSS into dist/index.html so the Python dashboard server can serve the
// whole UI as one page (it has no static-file routes). Fonts are already base64 in the CSS.
import { readFileSync, rmSync, writeFileSync } from "node:fs";

const dist = new URL("../dist/", import.meta.url);
const read = (href) => readFileSync(new URL(href.replace(/^\//, ""), dist), "utf8");
let html = read("index.html");

html = html.replace(/<script type="module" crossorigin src="([^"]+)"><\/script>/g, (_, src) =>
  `<script type="module">${read(src).replace(/<\/script/gi, "<\\/script")}</script>`);
html = html.replace(/<link rel="stylesheet" crossorigin href="([^"]+)">/g, (_, href) =>
  `<style>${read(href).replace(/<\/style/gi, "<\\/style")}</style>`);

if (/\/assets\//.test(html)) throw new Error("dist/index.html still references /assets/ after inlining");
writeFileSync(new URL("index.html", dist), html);
rmSync(new URL("assets/", dist), { recursive: true, force: true });
console.log(`inlined dist/index.html (${(html.length / 1024).toFixed(0)} KiB)`);
