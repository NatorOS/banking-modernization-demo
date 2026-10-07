// Vercel build step: fill in the tokens that scripts.dashboard substitutes at request time locally.
import { readFileSync, writeFileSync } from "node:fs";

const page = new URL("../dist/index.html", import.meta.url);
const namespace = process.env.DEMO_NAMESPACE || "demo-aws";
if (!/^demo-[a-z0-9][a-z0-9-]{0,39}$/.test(namespace)) throw new Error(`invalid DEMO_NAMESPACE ${namespace}`);
const host = process.env.DEMO_API_URL ? new URL(process.env.DEMO_API_URL).host : "AWS";
const label = `AWS (Vercel SigV4 proxy to ${host})`.replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);

const html = readFileSync(page, "utf8");
if (!html.includes("__BACKEND_LABEL__") || !html.includes('value="demo-local"')) {
  throw new Error("dist/index.html is missing the dashboard substitution tokens");
}
writeFileSync(page, html.replace("__BACKEND_LABEL__", label).replace('value="demo-local"', `value="${namespace}"`));
console.log(`dashboard target: ${label}, namespace ${namespace}`);
