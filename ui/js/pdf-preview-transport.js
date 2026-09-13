import { getTauriInvoke } from "./transport.js";


async function browserRequest(body, signal) {
  const response = await fetch("/api/pdf-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  let result = null;
  try { result = await response.json(); } catch (_) { /* handled below */ }
  if (!response.ok) {
    throw new Error((result && (result.error || result.errors?.[0])) ||
      `the server answered ${response.status}`);
  }
  if (!result || typeof result !== "object") throw new Error("invalid PDF preview response");
  return result;
}


async function request(method, params, httpBody, signal) {
  const invoke = getTauriInvoke();
  if (invoke) {
    // A selected native bridge is authoritative. Never retry its failure over
    // HTTP, because packaged windows must stay on the bounded native channel.
    return invoke("wb_rpc", { method, params });
  }
  return browserRequest(httpBody, signal);
}


export function startPdfPreview(args, signal) {
  return request("pdf_preview.start", { args }, { op: "start", args }, signal);
}


export function pollPdfPreview(operationId, signal) {
  return request("pdf_preview.poll", { operation_id: operationId },
    { op: "poll", operation_id: operationId }, signal);
}


export function cancelPdfPreview(operationId, signal) {
  return request("pdf_preview.cancel", { operation_id: operationId },
    { op: "cancel", operation_id: operationId }, signal);
}
