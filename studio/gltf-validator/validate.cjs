#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const validator = require("gltf-validator");

const MAX_ASSET_BYTES = 512 * 1024 * 1024;
const MAX_ISSUES = 50;
const MAX_FIELD_CHARS = 512;

function short(value) {
  return String(value ?? "").slice(0, MAX_FIELD_CHARS);
}

async function main() {
  const input = process.argv[2];
  if (!input || !path.isAbsolute(input)) {
    throw new Error("an absolute project-owned GLB path is required");
  }
  const size = fs.statSync(input).size;
  if (!Number.isSafeInteger(size) || size < 20 || size > MAX_ASSET_BYTES) {
    throw new Error("GLB size is outside the supported 20-byte to 512-MiB range");
  }

  const bytes = fs.readFileSync(input);
  // No externalResourceFunction is supplied: referenced external files are
  // reported unavailable and can never trigger network or arbitrary path reads.
  const result = await validator.validateBytes(bytes, {
    uri: path.basename(input),
    format: "glb",
    maxIssues: MAX_ISSUES,
    writeTimestamp: false,
  });
  const issues = result.issues ?? {};
  const messages = Array.isArray(issues.messages) ? issues.messages : [];
  const summary = {
    validator: "Khronos glTF-Validator",
    version: validator.version(),
    num_errors: Number(issues.numErrors ?? 0),
    num_warnings: Number(issues.numWarnings ?? 0),
    num_infos: Number(issues.numInfos ?? 0),
    num_hints: Number(issues.numHints ?? 0),
    messages: messages.slice(0, MAX_ISSUES).map((issue) => ({
      code: short(issue.code),
      message: short(issue.message),
      severity: Number(issue.severity ?? 0),
      pointer: short(issue.pointer),
    })),
  };
  process.stdout.write(`${JSON.stringify(summary)}\n`);
  if (summary.num_errors > 0) process.exitCode = 1;
}

main().catch((error) => {
  process.stderr.write(`${short(error && error.message)}\n`);
  process.exitCode = 2;
});
