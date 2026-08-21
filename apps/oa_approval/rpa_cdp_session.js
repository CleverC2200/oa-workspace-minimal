#!/usr/bin/env node

const endpoint = process.argv[2];
const timeoutMs = Math.max(1000, Number(process.argv[3]) || 5000);

let socket;
let timer;
let finished = false;

function finish(payload, exitCode = 0) {
  if (finished) return;
  finished = true;
  if (timer) clearTimeout(timer);
  try {
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
  } catch (_) {
    // 退出时关闭失败不影响本次读取结果。
  }
  process.stdout.write(JSON.stringify(payload), () => process.exit(exitCode));
}

function fail(code) {
  finish({ ok: false, code }, 1);
}

async function main() {
  let version;
  try {
    const response = await fetch(`${endpoint}/json/version`, { signal: AbortSignal.timeout(timeoutMs) });
    if (!response.ok) return fail("rpa_cdp_unavailable");
    version = await response.json();
  } catch (error) {
    return fail(error && error.name === "TimeoutError" ? "rpa_cdp_timeout" : "rpa_cdp_unavailable");
  }

  const webSocketUrl = String(version.webSocketDebuggerUrl || "");
  if (!webSocketUrl) return fail("rpa_session_invalid");

  timer = setTimeout(() => fail("rpa_cdp_timeout"), timeoutMs);
  try {
    socket = new WebSocket(webSocketUrl);
  } catch (_) {
    return fail("rpa_cdp_unavailable");
  }
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ id: 1, method: "Storage.getCookies" }));
  });
  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(String(event.data || ""));
    } catch (_) {
      return;
    }
    if (message.id !== 1) return;
    if (message.error || !message.result || !Array.isArray(message.result.cookies)) {
      return fail("rpa_session_invalid");
    }
    finish({ ok: true, cookies: message.result.cookies });
  });
  socket.addEventListener("error", () => fail("rpa_cdp_unavailable"));
}

main();
