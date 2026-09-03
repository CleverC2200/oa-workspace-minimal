#!/usr/bin/env node

const endpoint = process.argv[2];
const timeoutMs = Math.max(1000, Number(process.argv[3]) || 5000);
const requiredHost = String(process.argv[4] || "").toLowerCase();

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
  let targets;
  try {
    const response = await fetch(`${endpoint}/json/list`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) return fail("rpa_cdp_unavailable");
    targets = await response.json();
  } catch (error) {
    return fail(
      error && error.name === "TimeoutError"
        ? "rpa_cdp_timeout"
        : "rpa_cdp_unavailable",
    );
  }

  const target = Array.isArray(targets)
    ? targets.find((item) => {
        if (!item || item.type !== "page" || !item.webSocketDebuggerUrl) return false;
        try {
          return new URL(String(item.url || "")).hostname.toLowerCase() === requiredHost;
        } catch (_) {
          return false;
        }
      })
    : null;
  if (!target) return fail("rpa_session_missing");

  timer = setTimeout(() => fail("rpa_cdp_timeout"), timeoutMs);
  try {
    socket = new WebSocket(String(target.webSocketDebuggerUrl));
  } catch (_) {
    return fail("rpa_cdp_unavailable");
  }

  socket.addEventListener("open", () => {
    socket.send(
      JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: {
          awaitPromise: true,
          returnByValue: true,
          expression: `(async () => {
            const text = String(document.body?.innerText || "");
            const lowered = text.toLowerCase();
            const url = String(location.href || "").toLowerCase();
            const visiblePassword = Array.from(
              document.querySelectorAll('input[type="password"]'),
            ).some((element) => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
            });
            const timeoutText = [
              "由于长时间未操作",
              "系统自动退出",
              "需要重新登录",
              "登录态失效",
              "登录超时",
            ].some((marker) => lowered.includes(marker.toLowerCase()));
            const loginUrl = [
              "noright.jsp",
              "ddcodetimeout",
              "login_required",
              "loginrequired",
              "/login",
              "login.jsp",
            ].some((marker) => url.includes(marker));
            if (loginUrl || visiblePassword || timeoutText) {
              return { status: "expired" };
            }

            try {
              const query = new URLSearchParams({
                isLoadAll: "0",
                withPortal: "1",
                __random__: String(Date.now()),
              });
              const response = await fetch(
                "/api/portal/menu/getFrontEndMenu?" + query,
                {
                  method: "GET",
                  credentials: "same-origin",
                  cache: "no-store",
                  redirect: "follow",
                  headers: {
                    Accept: "application/json, text/plain, */*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "X-Requested-With": "XMLHttpRequest",
                  },
                  signal: AbortSignal.timeout(${timeoutMs}),
                },
              );
              const body = await response.text();
              const finalUrl = String(response.url || "").toLowerCase();
              const serverLoginMarker = [
                "由于长时间未操作",
                "系统自动退出",
                "需要重新登录",
                "登录态失效",
                "登录超时",
                "ddcodetimeout",
              ].some((marker) => body.toLowerCase().includes(marker.toLowerCase()));
              const serverLoginUrl = [
                "noright.jsp",
                "ddcodetimeout",
                "login_required",
                "loginrequired",
                "/login",
                "login.jsp",
              ].some((marker) => finalUrl.includes(marker));
              if (serverLoginMarker || serverLoginUrl) {
                return { status: "expired" };
              }
              if (!response.ok) return { status: "unknown" };

              const payload = JSON.parse(body);
              const authenticated =
                payload &&
                typeof payload === "object" &&
                Array.isArray(payload.data) &&
                Object.hasOwn(payload, "api_query_time") &&
                Object.hasOwn(payload, "api_deal_time");
              return { status: authenticated ? "authenticated" : "unknown" };
            } catch (_) {
              return { status: "unknown" };
            }
          })()`,
        },
      }),
    );
  });
  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(String(event.data || ""));
    } catch (_) {
      return;
    }
    if (message.id !== 1) return;
    const value = message?.result?.result?.value;
    if (
      message.error ||
      !value ||
      !["authenticated", "expired", "unknown"].includes(value.status)
    ) {
      return fail("rpa_session_invalid");
    }
    finish({ ok: true, status: value.status });
  });
  socket.addEventListener("error", () => fail("rpa_cdp_unavailable"));
}

main();
