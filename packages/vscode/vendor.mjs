// Copy xterm.js and its addons from node_modules to media/vendor, where the console view loads
// them. The package holds no node_modules.
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const target = path.join(here, "media", "vendor");
fs.mkdirSync(target, { recursive: true });
const files = {
  xterm: ["lib/xterm.js", "css/xterm.css", "LICENSE"],
  "addon-fit": ["lib/addon-fit.js"],
  "addon-web-links": ["lib/addon-web-links.js"],
  "addon-unicode11": ["lib/addon-unicode11.js"],
};
for (const [name, wanted] of Object.entries(files)) {
  for (const file of wanted) {
    const source = path.join(here, "node_modules", "@xterm", name, file);
    const text = fs.readFileSync(source, "utf8");
    // The source maps are not copied.
    const copy = file === "LICENSE" ? "xterm.LICENSE" : path.basename(file);
    fs.writeFileSync(path.join(target, copy), text.replace(/\n\/\/# sourceMappingURL=.*\n?/, "\n"));
  }
}
