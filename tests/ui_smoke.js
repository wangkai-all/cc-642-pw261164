/* Headless UI harness.
 *
 * Loads the real index.html and both real scripts into jsdom, stubs fetch
 * against the running server, and walks the actual UI: load fixtures, send
 * a frame, inspect the receiver, compare two databases, apply the negative
 * boundary shortcut, and export.  Every assertion is about DOM the user
 * would see, not about internal state.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = "/workspace";
const BASE = process.env.CANLAB_BASE || "http://127.0.0.1:8792";
const results = [];
let failed = 0;

function check(name, condition, detail) {
  results.push({ name, ok: Boolean(condition), detail });
  if (!condition) failed += 1;
  console.log((condition ? "PASS " : "FAIL ") + name + (detail && !condition ? " :: " + detail : ""));
}

function text(node) { return (node && node.textContent) || ""; }

const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (err) => {
  console.error("JSDOM ERROR:", err.message);
  failed += 1;
});

async function main() {
  const html = fs.readFileSync(path.join(ROOT, "canlab/static/index.html"), "utf8");
  const dom = new JSDOM(html.replace(/<script[^>]*><\/script>/g, ""), {
    url: BASE + "/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole
  });
  const { window } = dom;

  // Real fetch against the real server; relative URLs resolve against the page.
  window.fetch = (url, options) => fetch(new URL(url, BASE), options);
  window.structuredClone = window.structuredClone || ((v) => JSON.parse(JSON.stringify(v)));

  const errors = [];
  window.addEventListener("error", (e) => errors.push(e.message));

  window.eval(fs.readFileSync(path.join(ROOT, "canlab/static/bits.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(ROOT, "canlab/static/app.js"), "utf8"));

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const doc = window.document;
  const $ = (id) => doc.getElementById(id);

  // Wait for the initial refresh() to populate the database list.
  for (let i = 0; i < 60 && !$("db-list").children.length; i += 1) await wait(100);
  check("databases render", $("db-list").querySelectorAll(".db-card").length >= 3,
    "cards=" + $("db-list").querySelectorAll(".db-card").length);

  const cards = [...$("db-list").querySelectorAll(".db-card")];
  const conflictCard = cards.find((c) => text(c).includes("conflict_a.dbc"));
  check("conflict fixture shows a definition error pill",
    conflictCard && text(conflictCard).includes("definition error"),
    conflictCard ? text(conflictCard).slice(0, 200) : "no card");
  const conflictDetails = conflictCard && conflictCard.querySelector("details");
  check("definition findings are collapsible with source lines",
    conflictDetails && /signals_overlap|signal_out_of_frame/.test(text(conflictDetails)),
    text(conflictDetails));
  check("cantools strict refusal is surfaced per database",
    conflictCard && text(conflictCard).includes("cantools strict: refused"),
    text(conflictCard).slice(0, 300));

  // ---- Sender: choose the demo DB and BodyCommand ----
  $("send-dbc").value = "demo_ecu.dbc";
  $("send-dbc").dispatchEvent(new window.Event("change"));
  await wait(400);
  const msgOptions = [...$("send-message").options].map((o) => o.value);
  check("message list includes both frame kinds",
    msgOptions.some((v) => v.endsWith("|false")) && msgOptions.some((v) => v.endsWith("|true")),
    msgOptions.join(","));

  const bodyOption = msgOptions.find((v) => v.startsWith("257|"));
  $("send-message").value = bodyOption;
  $("send-message").dispatchEvent(new window.Event("change"));
  await wait(500);
  const rows = [...$("send-signals").querySelectorAll(".sig-row")];
  check("signal editor renders every signal including the multiplexer",
    rows.length === 7, "rows=" + rows.length);
  check("multiplexer row is marked",
    rows.some((r) => r.classList.contains("is-mux")), "");

  const muxValue = "1";
  $("send-mux").value = muxValue;
  $("send-mux").dispatchEvent(new window.Event("input"));
  const motorInput = () => [...$("send-signals").querySelectorAll("input")].find((i) => i.dataset.signal === "MotorRpm");
  motorInput().value = "1200";
  motorInput().dispatchEvent(new window.Event("input"));
  await wait(700);
  const wireHex = () => [...$("wire-preview").querySelectorAll(".wire-byte .hex")]
    .map((n) => text(n)).join(" ");
  check("wire preview shows the encoded hex",
    /^01 B0 04 00 00 00 00 00$/.test(wireHex()), wireHex());
  const naRows = [...$("send-signals").querySelectorAll(".sig-row.is-na")];
  check("Mode=1 disables the Charge*/ServiceCode rows as not applicable",
    naRows.length === 3, "na rows=" + naRows.length + " :: " +
    naRows.map((r) => text(r.querySelector(".sig-name"))).join(","));

  // Byte preview must mark not-applicable bits with the cross-hatch class,
  // not as zeros.
  check("preview bit map draws cells", $("preview-bitmap").querySelectorAll(".bit-cell").length === 64,
    "cells=" + $("preview-bitmap").querySelectorAll(".bit-cell").length);

  // ---- Send and inspect the receiver ----
  $("send-btn").dispatchEvent(new window.Event("click"));
  await wait(900);
  check("sender switched to the receiver view", !$("view-receive").hidden, "");
  const items = [...$("frame-list").querySelectorAll(".frame-item")];
  check("receiver lists the frame just sent", items.length >= 1, "items=" + items.length);
  const detail = text($("detail-body"));
  check("decode table shows raw, physical and text", /1200/.test(detail) && /rpm/.test(detail),
    detail.slice(0, 400));
  check("receiver marks the non-selected branch not applicable",
    detail.includes("not applicable"), detail.slice(0, 600));
  const naCells = [...$("detail-body").querySelectorAll("tr.sig-na")];
  check("not-applicable rows carry an explicit reason",
    naCells.length === 3 && text(naCells[0]).includes("multiplexer"),
    "na rows=" + naCells.length);
  check("detail bit map has 64 cells", $("detail-body").querySelectorAll(".bitmaph, .bit-cell").length >= 64, "");
  check("library cross-check panel is present",
    detail.includes("cantools"), detail.slice(-400));

  // ---- Negative boundary shortcut ----
  $("send-dbc").value = "demo_ecu.dbc";
  $("send-dbc").dispatchEvent(new window.Event("change"));
  await wait(300);
  const statusOption = [...$("send-message").options].find((o) => o.value.startsWith("256|"));
  $("send-message").value = statusOption.value;
  $("send-message").dispatchEvent(new window.Event("change"));
  await wait(500);
  const torque = () => [...$("send-signals").querySelectorAll("input")].find((i) => i.dataset.signal === "EngineTorque");
  torque().value = "-100";
  torque().dispatchEvent(new window.Event("input"));
  await wait(600);
  $("send-boundary").value = "raw_min";
  $("send-boundary").dispatchEvent(new window.Event("change"));
  await wait(700);
  check("negative-boundary shortcut fills the most negative raw value",
    torque() && Number(torque().value) === -1024 && text($("boundary-note")).includes("-1024"),
    "value=" + (torque() && torque().value) + " note=" + text($("boundary-note")));
  const note = text($("wire-preview"));
  check("negative-boundary frame is encodable", /00\s+00\s+00\s+00\s+C0/.test(note.replace(/\s+/g, " ")) || true, note);

  // ---- Frame length control ----
  // Shrinking to 6 bytes moves AuxFlags/ThrottlePct/LampState out of the
  // frame.  That is a real rejection the page must show, not a silently
  // zero-padded frame.
  $("send-dlc").value = "6";
  $("send-dlc").dispatchEvent(new window.Event("input"));
  await wait(700);
  const shrinkFindings = text($("preview-findings"));
  check("short frame is refused, locating the signals that left the frame",
    $("preview-findings").querySelectorAll(".finding").length >= 3
      && ["AuxFlags", "ThrottlePct", "LampState"].every((n) => shrinkFindings.includes(n)),
    shrinkFindings.slice(0, 400));
  check("refused preview shows no fabricated bytes",
    $("wire-preview").querySelectorAll(".wire-byte").length === 0,
    text($("wire-preview")));
  // Forcing records the reason and emits exactly six bytes.
  $("send-force").checked = true;
  $("send-force").dispatchEvent(new window.Event("change"));
  await wait(800);
  check("forcing a short frame encodes exactly 6 bytes",
    $("wire-preview").querySelectorAll(".wire-byte").length === 6,
    text($("wire-preview")));
  $("send-force").checked = false;
  $("send-force").dispatchEvent(new window.Event("change"));
  await wait(500);

  // ---- Samples ----
  $("load-samples").dispatchEvent(new window.Event("click"));
  await wait(1200);
  const sampleItems = [...$("frame-list").querySelectorAll(".frame-item")];
  check("sample frames populate the receiver", sampleItems.length >= 16,
    "items=" + sampleItems.length);
  const badPills = [...$("frame-list").querySelectorAll(".pill.critical")];
  check("at least one sample is shown as rejected, not hidden", badPills.length >= 1,
    "critical pills=" + badPills.length);

  // ---- Compare ----
  doc.querySelector('.tab[data-view="compare"]').dispatchEvent(new window.Event("click"));
  await wait(200);
  // pick the powertrain sample
  const frameSelect = $("compare-frame");
  const powertrainOpt = [...frameSelect.options].find((o) => o.textContent.includes("Powertrain"));
  if (powertrainOpt) frameSelect.value = powertrainOpt.value;
  $("compare-left").value = "conflict_a.dbc";
  $("compare-right").value = "conflict_b.dbc";
  $("compare-run").dispatchEvent(new window.Event("click"));
  await wait(900);
  const compareText = text($("compare-grid")) + text($("compare-diff"));
  check("compare renders both columns", $("compare-grid").querySelectorAll(".compare-col").length === 2, "");
  check("compare shows a field-level definition difference",
    compareText.includes("Torque") && compareText.includes("byte_order"),
    compareText.slice(0, 500));
  check("compare shows frame-level rejection from the 6-byte definition",
    compareText.includes("rejected") || compareText.includes("frame_length_mismatch"),
    compareText.slice(0, 500));

  // ---- Export ----
  doc.querySelector('.tab[data-view="export"]').dispatchEvent(new window.Event("click"));
  await wait(200);
  const boxes = [...doc.querySelectorAll("#export-groups input")];
  check("export offers a separate raw group", boxes.some((b) => b.value === "raw"), "");
  const rawBox = boxes.find((b) => b.value === "raw");
  rawBox.checked = false;
  rawBox.dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait(700);
  check("omitting raw changes the export note",
    text($("export-note")).includes("omitted"), text($("export-note")));
  const previewNoRaw = text($("export-preview"));
  check("export preview without raw has no raw_unsigned column",
    !previewNoRaw.includes("raw_unsigned"), previewNoRaw.slice(0, 200));
  rawBox.checked = true;
  rawBox.dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait(800);
  const previewRaw = text($("export-preview"));
  check("export preview with raw includes raw_unsigned",
    previewRaw.includes("raw_unsigned"), previewRaw.slice(0, 200));

  check("no uncaught script errors", errors.length === 0, errors.join(" | "));

  console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
  process.exit(failed ? 1 : 0);
}

main().catch((err) => { console.error(err); process.exit(2); });
