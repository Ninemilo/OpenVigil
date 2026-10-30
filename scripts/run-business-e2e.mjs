import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const defaultPython = resolve(
  root,
  "backend/.venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
export function runBusinessE2E(
  args,
  { python = process.env.WINDOPS_E2E_PYTHON ?? defaultPython, spawn = spawnSync } = {},
) {
  const executable = resolve(root, python);
  if (!existsSync(executable)) {
    throw new Error(
      "Install the backend environment first: cd backend && uv sync --frozen --extra test",
    );
  }
  const result = spawn(
    executable,
    [resolve(root, "backend/scripts/run_business_e2e.py"), ...args],
    {
      cwd: root,
      stdio: "inherit",
      windowsHide: true,
    },
  );
  if (result.error) throw result.error;
  return result.status ?? 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = runBusinessE2E(process.argv.slice(2));
}
