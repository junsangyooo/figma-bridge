// figma-bridge plugin: runs agent-issued Plugin API code and returns the result.
// The UI iframe does the networking (this thread has no fetch); see ui.html.

figma.showUI(__html__, { width: 260, height: 140, title: "figma-bridge" });

function describeError(e) {
  if (e && e.message) return String(e.message);
  return String(e);
}

// Values must survive postMessage + JSON, so drop anything not serializable.
function serializable(value) {
  try {
    return JSON.parse(JSON.stringify(value === undefined ? null : value));
  } catch (e) {
    return { _unserializable: String(value) };
  }
}

async function runJob(job) {
  // Same contract as use_figma: plain JS body, top-level await and return allowed.
  const body = "return (async () => {" + job.code + "\n})()";
  const fn = new Function("figma", body);
  return await fn(figma);
}

figma.ui.onmessage = async function (msg) {
  if (!msg || msg.type !== "job") return;

  let payload;
  try {
    const value = await runJob(msg);
    payload = { type: "result", id: msg.id, ok: true, value: serializable(value) };
  } catch (e) {
    payload = { type: "result", id: msg.id, ok: false, error: describeError(e) };
  }
  figma.ui.postMessage(payload);
};

// Reported on every poll so the CLI can refuse jobs aimed at a different file.
figma.ui.postMessage({
  type: "hello",
  fileKey: figma.fileKey || null,
  fileName: figma.root.name,
  editorType: figma.editorType,
});
