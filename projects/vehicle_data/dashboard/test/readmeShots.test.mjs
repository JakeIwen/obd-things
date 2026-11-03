import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { auditListenerLog, parseArgs, replaceDirectory } from "../tools/readme_shots.mjs";

// web.py's access-log line: client, quoted request line, status, size (log_message over log_request).
const request = (method, target, status = 200) => `127.0.0.1 "${method} ${target} HTTP/1.1" ${status} -`;

test("a demo capture's listener log passes: reads only, and the snapshot as the one data route", () => {
  const lines = [
    "dashboard v2 listening on 127.0.0.1:41234 static=/x broker=/run/van-telemetry/api.sock",
    request("GET", "/build.json"),
    request("GET", "/__demo/install-demo.js"),
    request("GET", "/assets/main.ZK3KA63F.js"),
    request("GET", "/v1/snapshot?fields=all"),
    request("GET", "/v1/snapshot"),
    "127.0.0.1 code 404, message File not found",
  ];
  assert.deepEqual(auditListenerLog(lines), {
    methods: { GET: 5 },
    paths: { "/build.json": 1, "/__demo/install-demo.js": 1, "/v1/snapshot": 2 },
    problems: [],
  });
});

test("a write, a live data route or an unreadable log refuses the capture", () => {
  const { problems } = auditListenerLog([request("GET", "/"), request("POST", "/v1/acquisitions", 403), request("GET", "/v1/history")]);
  assert.deepEqual(problems, ["POST requests reached the listener", "/v1/acquisitions reached the listener instead of the demo",
    "/v1/history reached the listener instead of the demo"]);
  assert.deepEqual(auditListenerLog(["127.0.0.1 GET / 200"]).problems, ["no GET requests in the listener log"]);
});

test("a staged run replaces the previous one, which survives a swap that fails", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "readme-shots-"));
  const stage = (file) => {
    const staged = path.join(root, "staged");
    fs.mkdirSync(staged);
    fs.writeFileSync(path.join(staged, file), file);
    return staged;
  };
  try {
    const target = path.join(root, "out");
    replaceDirectory(target, stage("first.png"));
    replaceDirectory(target, stage("second.png"));
    assert.deepEqual(fs.readdirSync(target), ["second.png"]);
    assert.throws(() => replaceDirectory(target, path.join(root, "missing")), { code: "ENOENT" });
    assert.deepEqual(fs.readdirSync(target), ["second.png"]);
    assert.deepEqual(fs.readdirSync(root), ["out"]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("arguments: shot numbers are two digits, and promoting takes what the capture took", () => {
  assert.deepEqual(parseArgs([]), { promote: false, only: null });
  assert.deepEqual(parseArgs(["--only", "01,05"]), { promote: false, only: ["01", "05"] });
  assert.deepEqual(parseArgs(["--promote"]), { promote: true, only: null });
  assert.throws(() => parseArgs(["--only", "1"]), /two-digit shot numbers/);
  assert.throws(() => parseArgs(["--promote", "--only", "01"]), /retake with --only first/);
  assert.throws(() => parseArgs(["--out", "/tmp/x"]), /Unknown option '--out'[\s\S]*usage:/);
});
