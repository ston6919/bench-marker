import { Config } from "@remotion/cli/config";
import fs from "fs";
import path from "path";

// Prefer Remotion's chrome-headless-shell. Avoid snap Chromium on this VPS.
const bundled = path.join(
  __dirname,
  "node_modules/.remotion/chrome-headless-shell/linux64/chrome-headless-shell-linux64/chrome-headless-shell"
);

const candidates = [
  process.env.REMOTION_BROWSER_EXECUTABLE,
  bundled,
].filter(Boolean) as string[];

for (const browserPath of candidates) {
  try {
    if (
      browserPath &&
      fs.existsSync(browserPath) &&
      !browserPath.includes("/snap/")
    ) {
      Config.setBrowserExecutable(browserPath);
      break;
    }
  } catch {
    // ignore
  }
}

Config.setConcurrency(1);
Config.setChromiumOpenGlRenderer("swangle");
