// Read-only API client. Returns {ok: true, status, data} or
// {ok: false, status, code}. Status 0 means the API was not reachable or did
// not answer with JSON. The API error "message" is never read; only the stable
// "code" selects fixed copy. No timestamp is parsed here.

const CODE_RE = /^[a-z_]{1,40}$/;

export async function fetchJson(url) {
  let response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (err) {
    return { ok: false, status: 0, code: "unreachable" };
  }
  let body = null;
  let parsed = true;
  try {
    body = await response.json();
  } catch (err) {
    parsed = false;
  }
  if (!response.ok) {
    let code = null;
    if (parsed && body && body.error && typeof body.error.code === "string" && CODE_RE.test(body.error.code)) {
      code = body.error.code;
    }
    return { ok: false, status: response.status, code: code };
  }
  if (!parsed) {
    return { ok: false, status: 0, code: "bad_response" };
  }
  return { ok: true, status: response.status, data: body };
}

export function fetchRuns() {
  return fetchJson("/api/runs");
}

export function fetchRun(runId) {
  return fetchJson("/api/runs/" + encodeURIComponent(runId));
}
