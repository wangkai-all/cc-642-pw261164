/* CANLab front-end.
 *
 * Rules this file follows:
 *   - The server is the only source of truth for a decode.  Nothing here
 *     recomputes bits, values or applicability.
 *   - A sender edit calls /api/frame/preview (debounced); the preview and
 *     the rejection list therefore come from the same encoder as "Send".
 *   - Library diagnostics and business findings are rendered in separate
 *     blocks with different labels; they are never concatenated.
 */
(function () {
  "use strict";

  const state = {
    session: { dbcs: {}, frames: [], count: 0 },
    samples: [],
    fixtures: [],
    exportGroups: {},
    defaultExportGroups: [],
    view: "send",
    selectedSeq: null,
    selectedDbc: null,
    send: {
      dbc: "",
      frameId: null,
      values: {},
      nestedMux: {},
      preview: null,
      timer: null
    },
    compare: { seq: null, left: "", right: "" },
    exportFormat: "csv"
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const fmtNum = (value) => {
    if (value === null || value === undefined) return "";
    if (typeof value !== "number") return String(value);
    if (Number.isInteger(value)) return String(value);
    return String(Math.round(value * 1e6) / 1e6);
  };
  const hex = (value) => "0x" + Number(value).toString(16).toUpperCase();

  function toast(message, isError) {
    const node = el("div", "toast" + (isError ? " error" : ""), message);
    document.body.appendChild(node);
    setTimeout(() => node.remove(), isError ? 6000 : 2800);
  }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, options || {}));
    let payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok && payload === null) {
      throw new Error(`request failed (${response.status})`);
    }
    return payload;
  }

  async function refresh(includeFrames) {
    const data = await api("/api/state" + (includeFrames === false ? "?frames=0" : ""));
    if (!data || !data.ok) throw new Error("could not read session state");
    state.session = data.session;
    state.samples = data.samples || [];
    state.fixtures = data.fixtures || [];
    state.exportGroups = data.export_groups || {};
    state.defaultExportGroups = data.default_export_groups || [];
    renderAll();
  }

  /* ------------------------------------------------------------------ */
  /* Databases                                                          */
  /* ------------------------------------------------------------------ */

  function dbcKeys() { return Object.keys(state.session.dbcs); }

  function renderDatabases() {
    const list = $("db-list");
    list.textContent = "";
    const keys = dbcKeys();
    if (!keys.length) {
      list.appendChild(el("p", "empty", "No database loaded. Load a fixture, upload a file, or paste DBC text."));
    }
    keys.forEach((key) => {
      const db = state.session.dbcs[key];
      const card = el("div", "db-card");
      const head = el("div", "db-card-head");
      head.appendChild(el("strong", null, key));
      const counts = db.definition_counts || {};
      const errorCount = (counts.error || 0);
      const warnCount = (counts.warning || 0);
      const verdict = errorCount ? el("span", "pill critical", `${errorCount} definition error${errorCount > 1 ? "s" : ""}`)
        : el("span", "pill good", "definitions consistent");
      head.appendChild(verdict);
      if (warnCount) head.appendChild(el("span", "pill warning", `${warnCount} warning`));
      const lib = db.library || {};
      if (lib.available) {
        const libPill = el("span",
          "pill " + (lib.strict_ok ? "good" : "serious"),
          lib.strict_ok ? "cantools strict: ok" : "cantools strict: refused");
        libPill.title = lib.strict_ok ? "cantools loads this file with its default strict checks."
          : "cantools' strict loader refuses this file; see the library panel in a decode.";
        head.appendChild(libPill);
      }
      const remove = el("button", "btn btn-small btn-quiet", "Remove");
      remove.addEventListener("click", async () => {
        await api("/api/dbc/remove", { method: "POST", body: JSON.stringify({ key }) });
        await refresh();
      });
      head.appendChild(remove);
      card.appendChild(head);

      const meta = el("div", "db-meta");
      meta.appendChild(el("span", null, `${db.messages.length} messages`));
      meta.appendChild(el("span", null, lib.available ? `cantools ${lib.version}` : "cantools not installed"));
      card.appendChild(meta);

      const chips = el("div", "db-messages");
      db.messages.forEach((msg) => {
        const chip = el("span", "msg-chip",
          `${msg.is_extended ? "29-bit " : ""}${hex(msg.frame_id)} ${msg.name} (${msg.length}B)`);
        chips.appendChild(chip);
      });
      card.appendChild(chips);

      if (db.definition_findings.length) {
        const details = el("details", "db-warnings");
        details.appendChild(el("summary", null,
          `${db.definition_findings.length} definition finding(s) - click for source lines`));
        const wrap = el("div", "findings");
        db.definition_findings.forEach((f) => wrap.appendChild(renderFinding(f)));
        details.appendChild(wrap);
        card.appendChild(details);
      }
      if (db.warnings && db.warnings.length) {
        const details = el("details", "db-warnings");
        details.appendChild(el("summary", null, `${db.warnings.length} parser note(s)`));
        const ul = el("ul");
        db.warnings.forEach((w) => ul.appendChild(el("li", null, w)));
        details.appendChild(ul);
        card.appendChild(details);
      }
      list.appendChild(card);
    });
  }

  function renderFixtureOptions() {
    const select = $("fixture-select");
    const current = select.value;
    select.textContent = "";
    state.fixtures.forEach((f) => {
      const option = el("option", null, f.filename);
      option.value = f.filename;
      select.appendChild(option);
    });
    if (current) select.value = current;
  }

  /* ------------------------------------------------------------------ */
  /* Sender                                                             */
  /* ------------------------------------------------------------------ */

  function messagesFor(key) {
    const db = state.session.dbcs[key];
    return db ? db.messages : [];
  }

  function currentMessage() {
    const key = $("send-dbc").value;
    const raw = $("send-message").value;
    const msg = messagesFor(key).find((m) =>
      `${m.frame_id}|${m.is_extended}` === raw);
    return { key, msg };
  }

  function renderSendSelectors() {
    const dbcSelect = $("send-dbc");
    const keys = dbcKeys();
    const keep = state.send.dbc && keys.includes(state.send.dbc)
      ? state.send.dbc : (keys[0] || "");
    dbcSelect.textContent = "";
    keys.forEach((key) => {
      const option = el("option", null, key);
      option.value = key;
      dbcSelect.appendChild(option);
    });
    dbcSelect.value = keep;
    state.send.dbc = keep;

    const msgSelect = $("send-message");
    const messages = messagesFor(keep);
    const keepMsg = msgSelect.value;
    msgSelect.textContent = "";
    messages.forEach((msg) => {
      const option = el("option", null,
        `${hex(msg.frame_id)} ${msg.name}${msg.is_extended ? " (29-bit)" : ""}`);
      option.value = `${msg.frame_id}|${msg.is_extended}`;
      msgSelect.appendChild(option);
    });
    const compatible = messages.some((m) => `${m.frame_id}|${m.is_extended}` === keepMsg);
    if (compatible) msgSelect.value = keepMsg;
    else if (messages.length) {
      // Prefer the first message with signals so the editor is useful.
      const withSignals = messages.find((m) => m.signals.length) || messages[0];
      msgSelect.value = `${withSignals.frame_id}|${withSignals.is_extended}`;
    } else {
      msgSelect.value = "";
    }
    syncDlcFromMessage();
    renderSignalEditor();
  }

  function syncDlcFromMessage() {
    const { msg } = currentMessage();
    if (msg && !state.send.dlcTouched) {
      $("send-dlc").value = msg.length;
    }
  }

  function signalLayout(sig) {
    const base = `${sig.byte_order === "big_endian" ? "Motorola" : "Intel"} ${sig.start}|${sig.length}`;
    const mux = sig.multiplexer_signal
      ? ` · mux ${sig.multiplexer_signal}=${sig.multiplexer_ids.join("/")}`
      : "";
    const sign = sig.is_signed ? " · signed" : "";
    return base + sign + mux;
  }

  function renderSignalEditor() {
    const wrap = $("send-signals");
    wrap.textContent = "";
    const { key, msg } = currentMessage();
    if (!msg) {
      wrap.appendChild(el("p", "empty", "No message selected."));
      return;
    }
    const full = msg;
    const muxInput = $("send-mux");
    const muxSignals = full.signals.filter((s) => s.is_multiplexer);
    const rootMux = muxSignals.find((s) => !s.multiplexer_signal);
    if (rootMux) {
      muxInput.disabled = false;
      if (muxInput.value === "") muxInput.value = "0";
    } else {
      muxInput.value = "";
      muxInput.disabled = true;
    }

    full.signals.forEach((sig) => {
      const row = el("div", "sig-row");
      if (sig.is_multiplexer) row.classList.add("is-mux");
      const name = el("div", "sig-name");
      name.appendChild(el("strong", null, sig.name));
      name.appendChild(el("small", null, signalLayout(sig)));
      row.appendChild(name);

      const applicable = signalApplicability(sig);
      if (applicable.status !== "active") {
        row.classList.add(applicable.status === "error" ? "is-error" : "is-na");
      }
      if (sig.is_multiplexer) {
        const hint = el("div", "sig-range");
        hint.textContent = sig.multiplexer_signal
          ? `sets the branch below ${sig.multiplexer_signal}`
          : "root selector - use the field above";
        row.appendChild(hint);
        const spacer = el("div");
        const spacer2 = el("div");
        row.appendChild(spacer);
        row.appendChild(spacer2);
        row.appendChild(el("div", "sig-badge", "mux"));
        wrap.appendChild(row);
        return;
      }

      const inputWrap = el("div", "sig-input");
      const input = el("input");
      input.type = "number";
      input.step = "any";
      input.dataset.signal = sig.name;
      input.placeholder = "0";
      input.disabled = applicable.status !== "active";
      if (state.send.values[sig.name] !== undefined) {
        input.value = state.send.values[sig.name];
      }
      input.addEventListener("input", () => {
        if (input.value === "") delete state.send.values[sig.name];
        else state.send.values[sig.name] = Number(input.value);
        schedulePreview();
      });
      inputWrap.appendChild(input);
      inputWrap.appendChild(el("span", "sig-unit", sig.unit || ""));
      row.appendChild(inputWrap);

      const range = el("div", "sig-range");
      range.textContent = rangeText(sig);
      range.title = "Declared physical range from the DBC";
      row.appendChild(range);

      const enumCell = el("div", "sig-enum");
      if (sig.choices && Object.keys(sig.choices).length) {
        enumCell.textContent = Object.entries(sig.choices)
          .map(([raw, text]) => `${raw}=${text}`).join(" · ");
        enumCell.title = "Text values defined for this signal";
      }
      row.appendChild(enumCell);
      row.appendChild(el("div", "sig-badge", sig.is_signed ? "sgn" : ""));

      if (applicable.status !== "active") {
        const reason = el("div", "sig-na-reason", applicable.reason || "");
        row.appendChild(reason);
      }
      wrap.appendChild(row);
    });

    const boundary = $("send-boundary");
    if (rootMux || full.signals.some((s) => s.is_signed)) {
      boundary.disabled = false;
    } else {
      boundary.disabled = true;
    }
  }

  /* Applicability in the editor is decided by the freshly previewed
   * decode, never guessed locally.  Before the first preview returns we
   * treat every signal as editable; the preview then disables the rows
   * the selected multiplexer branch does not cover. */
  function signalApplicability(sig) {
    const decoded = state.send.preview && state.send.preview.decode;
    if (!decoded) return { status: "active", reason: null };
    const match = decoded.signals.find((s) => s.name === sig.name);
    if (!match) return { status: "active", reason: null };
    return { status: match.status, reason: match.reason || match.error };
  }

  function rangeText(sig) {
    if (sig.minimum === null || sig.maximum === null ||
        sig.minimum === undefined || sig.maximum === undefined) return "no declared range";
    return `[${fmtNum(sig.minimum)} … ${fmtNum(sig.maximum)}] ${sig.unit || ""}`.trim();
  }

  function schedulePreview() {
    clearTimeout(state.send.timer);
    state.send.timer = setTimeout(requestPreview, 180);
  }

  function sendPayload() {
    const { key, msg } = currentMessage();
    if (!msg) return null;
    const muxValue = $("send-mux").value === "" ? null : Number($("send-mux").value);
    return {
      dbc: key,
      frame_id: msg.frame_id,
      is_extended: msg.is_extended,
      dlc: Number($("send-dlc").value || 0),
      mux_value: Number.isNaN(muxValue) ? null : muxValue,
      nested_mux: state.send.nestedMux,
      values: state.send.values,
      force: $("send-force").checked
    };
  }

  async function requestPreview() {
    const payload = sendPayload();
    if (!payload) return;
    const previousNa = naSignalNames();
    const result = await api("/api/frame/preview", {
      method: "POST", body: JSON.stringify(payload)
    });
    state.send.preview = result;
    renderWirePreview(result);
    renderPreviewFindings(result);
    if (result.ok && result.decode) {
      const nextNa = naSignalNames();
      if (!sameSet(previousNa, nextNa)) {
        // A branch change made some signals meaningless; drop the values
        // that no longer correspond to any bits so they cannot linger in
        // the next request.
        nextNa.forEach((name) => { delete state.send.values[name]; });
        renderSignalEditor();
      }
    }
  }

  function naSignalNames() {
    const decoded = state.send.preview && state.send.preview.decode;
    if (!decoded) return new Set();
    return new Set(decoded.signals
      .filter((s) => s.status !== "active")
      .map((s) => s.name));
  }

  function sameSet(a, b) {
    if (a.size !== b.size) return false;
    for (const value of a) if (!b.has(value)) return false;
    return true;
  }

  function renderWirePreview(result) {
    const wrap = $("wire-preview");
    wrap.textContent = "";
    $("send-stage").textContent = result ? (result.stage || "") : "";
    if (!result || !result.ok) {
      wrap.appendChild(el("p", "wire-empty",
        result ? "The encoder rejected this scenario; see the findings below." : "No preview yet."));
      $("preview-bitmap").textContent = "";
      $("preview-decode").textContent = "";
      return;
    }
    const decoded = result.decode;
    const bytes = decoded ? decoded.payload_hex.split(" ") : [];
    const line = el("div", "wire-line");
    bytes.forEach((value, index) => {
      const cell = el("div", "wire-byte" + (value === "00" ? " zero" : ""));
      cell.appendChild(el("span", "hex", value));
      cell.appendChild(el("span", "idx", index));
      line.appendChild(cell);
    });
    wrap.appendChild(line);
    wrap.appendChild(el("div", "wire-meta",
      `${bytes.length} byte(s) · ${result.message || ""}`));
    if (decoded) {
      CanlabBits.renderBitMap(
        $("preview-bitmap"), decoded.payload_bytes, [], null, { legend: true });
      renderPreviewDecode(decoded);
    }
  }

  function renderPreviewDecode(decoded) {
    const wrap = $("preview-decode");
    wrap.textContent = "";
    const table = el("table", "decode-table");
    const head = el("tr");
    ["Signal", "Raw", "Physical", "Text", "Status"].forEach((h) =>
      head.appendChild(el("th", null, h)));
    const thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    const body = el("tbody");
    decoded.signals.forEach((sig) => {
      const tr = el("tr");
      tr.className = sig.status === "active" ? "sig-active"
        : (sig.status === "error" ? "sig-error" : "sig-na");
      tr.appendChild(el("td", "sig-name-cell", sig.name));
      tr.appendChild(el("td", "num",
        sig.raw_signed === null ? "—" : fmtNum(sig.raw_signed)));
      tr.appendChild(el("td", "phys",
        sig.physical === null ? "—" : fmtNum(sig.physical) + (sig.unit ? " " + sig.unit : "")));
      tr.appendChild(el("td", null, sig.enum_text || ""));
      tr.appendChild(el("td", null, sig.status === "active" ? "active"
        : (sig.status === "error" ? "unreadable" : "not applicable")));
      body.appendChild(tr);
    });
    table.appendChild(body);
    wrap.appendChild(table);
  }

  function renderPreviewFindings(result) {
    const wrap = $("preview-findings");
    wrap.textContent = "";
    (result.findings || []).forEach((f) => wrap.appendChild(renderFinding(f)));
  }

  /* ------------------------------------------------------------------ */
  /* Findings                                                           */
  /* ------------------------------------------------------------------ */

  function renderFinding(finding) {
    const node = el("div", "finding " + (finding.severity || "info"));
    const head = el("div", "finding-head");
    head.appendChild(el("span", "finding-sev", finding.severity || "info"));
    head.appendChild(el("span", "finding-msg", finding.message));
    head.appendChild(el("span", "finding-code", finding.code));
    head.appendChild(el("span", "finding-scope", finding.scope || ""));
    node.appendChild(head);
    if (finding.detail) node.appendChild(el("div", "finding-detail", finding.detail));
    const loc = finding.location || {};
    const bits = [];
    if (loc.message_line) bits.push(`message line ${loc.message_line}`);
    if (loc.signal) bits.push(loc.signal);
    if (loc.signal_line) bits.push(`line ${loc.signal_line}`);
    if (loc.start !== undefined && loc.start !== null) {
      bits.push(`start ${loc.start} len ${loc.length} (${loc.byte_order})`);
    }
    if (loc.bit_offsets && loc.bit_offsets.length) {
      bits.push(`linear bits ${Math.min(...loc.bit_offsets)}..${Math.max(...loc.bit_offsets)}`);
    }
    if (loc.received_length !== undefined) bits.push(`received ${loc.received_length} B`);
    if (loc.requested_length !== undefined) bits.push(`requested ${loc.requested_length} B`);
    if (bits.length) node.appendChild(el("div", "finding-loc", bits.join(" · ")));
    return node;
  }

  function renderLibraryPanel(oracle) {
    const wrap = el("div", "lib-panel");
    const title = el("div", "lib-title", "Library cross-check (cantools)");
    wrap.appendChild(title);
    if (!oracle) {
      wrap.appendChild(el("div", "hint", "No cross-check for this database."));
      return wrap;
    }
    if (!oracle.ok) {
      const diag = oracle.diagnostic || {};
      wrap.appendChild(el("div", "lib-call", diag.call || ""));
      wrap.appendChild(el("div", "lib-exc", diag.exception || "failed"));
      wrap.appendChild(el("div", "lib-msg", diag.message || ""));
      wrap.appendChild(el("div", "hint", diag.detail || ""));
      if (diag.business_meaning) {
        const note = el("div", "finding info");
        note.appendChild(el("div", "finding-detail", diag.business_meaning));
        wrap.appendChild(note);
      }
      return wrap;
    }
    const values = Object.keys(oracle.values || {}).length;
    wrap.appendChild(el("div", "lib-msg",
      `cantools agreed on ${values} active signal value(s).`));
    (oracle.disagreement || []).forEach((d) => {
      const row = el("div", "finding error");
      row.appendChild(el("div", "finding-msg", `disagreement on ${d.signal}: ${d.kind}`));
      row.appendChild(el("div", "finding-detail", d.detail || ""));
      wrap.appendChild(row);
    });
    return wrap;
  }

  /* ------------------------------------------------------------------ */
  /* Receiver                                                           */
  /* ------------------------------------------------------------------ */

  function frameTitle(frame) {
    const first = Object.keys(frame.decodes || {})[0];
    const decoded = first ? frame.decodes[first] : null;
    return decoded && decoded.message ? decoded.message : `0x${frame.frame_id.toString(16).toUpperCase()}`;
  }

  function verdictBadges(frame) {
    const badges = [];
    const keys = Object.keys(frame.verdicts || {});
    keys.forEach((key) => {
      const verdict = frame.verdicts[key];
      const label = keys.length > 1 ? key + ": " : "";
      badges.push(verdict.accepted
        ? el("span", "pill good", label + "accepted")
        : el("span", "pill critical", label + "rejected"));
    });
    if (!keys.length) {
      const decoded = Object.values(frame.decodes || {})[0];
      if (decoded && decoded.message_found === false) {
        badges.push(el("span", "pill serious", "not defined"));
      }
    }
    (frame.sender_findings || []).forEach((f) => {
      if (f.severity === "error") badges.push(el("span", "pill critical", "sender rejected"));
    });
    return badges;
  }

  function renderFrameList() {
    const list = $("frame-list");
    list.textContent = "";
    const frames = state.session.frames || [];
    if (!frames.length) {
      list.appendChild(el("p", "empty", "No frames yet. Send one, load the samples, or import raw bytes."));
      return;
    }
    frames.slice().reverse().forEach((frame) => {
      const item = el("div", "frame-item");
      item.setAttribute("role", "listitem");
      item.tabIndex = 0;
      item.setAttribute("aria-selected", frame.seq === state.selectedSeq ? "true" : "false");
      item.appendChild(el("span", "frame-seq", "#" + frame.seq));
      item.appendChild(el("span", "frame-title", frame.label || frameTitle(frame)));
      const badges = el("div", "frame-badges");
      verdictBadges(frame).forEach((b) => badges.appendChild(b));
      item.appendChild(badges);
      item.appendChild(el("span", "frame-sub",
        `${frame.frame_id_hex}${frame.is_extended ? " (29-bit)" : ""} · ${frame.dlc} B · ${frame.payload_hex}`));
      const open = () => selectFrame(frame.seq);
      item.addEventListener("click", open);
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      });
      list.appendChild(item);
    });
  }

  function selectFrame(seq) {
    state.selectedSeq = seq;
    renderFrameList();
    renderDetail();
  }

  function selectedFrame() {
    return (state.session.frames || []).find((f) => f.seq === state.selectedSeq) || null;
  }

  function renderDetail() {
    const body = $("detail-body");
    body.textContent = "";
    const frame = selectedFrame();
    if (!frame) {
      body.appendChild(el("p", "empty", "No frame selected."));
      $("detail-subtitle").textContent = "Select a frame.";
      return;
    }
    const keys = Object.keys(frame.decodes || {});
    if (!state.selectedDbc || !keys.includes(state.selectedDbc)) {
      state.selectedDbc = keys[0] || null;
    }
    $("detail-subtitle").textContent =
      `frame #${frame.seq} · ${frame.time_text} · ${frame.source}`;

    const head = el("div", "detail-head");
    head.appendChild(el("span", "frame-id", frame.frame_id_hex));
    head.appendChild(el("span", "hint", frame.is_extended ? "29-bit extended" : "11-bit standard"));
    head.appendChild(el("span", "hint", `${frame.dlc} byte(s) on the wire`));
    if (frame.label) head.appendChild(el("span", "hint", frame.label));
    body.appendChild(head);

    if (keys.length > 1) {
      const picker = el("div", "field");
      const label = el("span", null, "Interpret with");
      picker.appendChild(label);
      const select = el("select");
      keys.forEach((key) => {
        const option = el("option", null, key);
        option.value = key;
        select.appendChild(option);
      });
      select.value = state.selectedDbc;
      select.addEventListener("change", () => {
        state.selectedDbc = select.value;
        renderDetail();
      });
      picker.appendChild(select);
      body.appendChild(picker);
    }

    const key = state.selectedDbc;
    const decoded = frame.decodes[key];
    if (!decoded) {
      body.appendChild(el("p", "empty", "This database was not loaded when the frame arrived."));
      return;
    }

    const raw = el("div", "detail-raw");
    const bitmap = el("div", "bitmap-wrap");
    raw.appendChild(bitmap);
    const side = el("div", "raw-side");
    const dl = el("dl", "kv");
    const pairs = [
      ["DLC", `${frame.dlc} byte(s)`],
      ["Expected", decoded.expected_dlc === null ? "—" : `${decoded.expected_dlc} byte(s)`],
      ["Payload", frame.payload_hex],
      ["ASCII", frame.payload_ascii]
    ];
    pairs.forEach(([k, v]) => {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    });
    side.appendChild(dl);
    const verdict = frame.verdicts[key];
    if (verdict) {
      side.appendChild(el("span", "pill " + (verdict.accepted ? "good" : "critical"),
        verdict.accepted ? "frame accepted" : "frame rejected"));
    }
    if (frame.sender_findings && frame.sender_findings.length) {
      side.appendChild(el("span", "pill serious",
        `${frame.sender_findings.length} sender finding(s)`));
    }
    raw.appendChild(side);
    body.appendChild(raw);

    const muxRow = el("div", "crosscheck-row");
    if (Object.keys(decoded.mux_values || {}).length) {
      muxRow.appendChild(el("span", "hint", "Resolved selectors:"));
      Object.entries(decoded.mux_values).forEach(([name, value]) => {
        muxRow.appendChild(el("span", "layout-tag", `${name} = ${value}`));
      });
    }
    if (muxRow.childNodes.length) body.appendChild(muxRow);

    const firstActive = decoded.signals.find((s) => s.status === "active");
    let highlight = firstActive ? firstActive.name : null;
    CanlabBits.renderBitMap(bitmap, decoded.payload_bytes, decoded.signals,
      highlight, { legend: true });

    const tableWrap = el("div");
    tableWrap.appendChild(renderDecodeTable(decoded, (sig) => {
      highlight = sig.name;
      CanlabBits.renderBitMap(bitmap, decoded.payload_bytes, decoded.signals,
        highlight, { legend: true });
    }));
    body.appendChild(tableWrap);

    if (verdict && verdict.findings.length) {
      const wrap = el("div", "findings");
      wrap.appendChild(el("h3", null, "Business findings for this frame"));
      verdict.findings.forEach((f) => wrap.appendChild(renderFinding(f)));
      body.appendChild(wrap);
    }

    if (frame.sender_findings && frame.sender_findings.length) {
      const wrap = el("div", "findings");
      wrap.appendChild(el("h3", null, "Sender findings (recorded with the frame)"));
      frame.sender_findings.forEach((f) => wrap.appendChild(renderFinding(f)));
      body.appendChild(wrap);
    }

    body.appendChild(renderLibraryPanel(frame.oracle ? frame.oracle[key] : null));
  }

  function renderDecodeTable(decoded, onHover) {
    const table = el("table", "decode-table");
    const thead = el("thead");
    const head = el("tr");
    ["Signal", "Layout", "Raw", "Phys.", "Text", "Status"].forEach((title) =>
      head.appendChild(el("th", null, title)));
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = el("tbody");
    decoded.signals.forEach((sig) => {
      const tr = el("tr");
      tr.className = sig.status === "active" ? "sig-active"
        : (sig.status === "error" ? "sig-error" : "sig-na");
      tr.addEventListener("mouseenter", () => onHover(sig));
      tr.addEventListener("focusin", () => onHover(sig));
      tr.tabIndex = 0;

      tr.appendChild(el("td", "sig-name-cell", sig.name));
      const layout = el("td");
      layout.appendChild(el("span", "layout-tag",
        `${sig.byte_order === "big_endian" ? "Motorola" : "Intel"} ${sig.start}|${sig.length}`));
      if (sig.multiplexer_signal) {
        layout.appendChild(el("span", "hint",
          ` mux ${sig.multiplexer_signal}=${sig.multiplexer_ids.join("/")}`));
      }
      tr.appendChild(layout);

      tr.appendChild(el("td", "num",
        sig.status === "active" ? fmtNum(sig.raw_signed) : "—"));
      const phys = el("td", "phys");
      if (sig.status === "active" && sig.physical !== null) {
        phys.textContent = fmtNum(sig.physical);
        if (sig.unit) {
          const unit = el("span", "unit", " " + sig.unit);
          phys.appendChild(unit);
        }
      } else {
        phys.textContent = "—";
      }
      tr.appendChild(phys);
      tr.appendChild(el("td", null, sig.status === "active" ? (sig.enum_text || "") : ""));

      const status = el("td", "reason-cell");
      if (sig.status === "active") {
        if (sig.in_defined_range === false) {
          status.appendChild(el("span", "pill warning", "outside declared range"));
        } else if (sig.enum_known === false) {
          status.appendChild(el("span", "hint", "no text for this raw value"));
        } else {
          status.textContent = "";
        }
      } else if (sig.status === "not_applicable") {
        status.appendChild(el("span", "na-badge", "not applicable"));
        status.appendChild(el("div", "hint", sig.reason || ""));
      } else {
        status.appendChild(el("span", "na-badge", "unreadable"));
        status.appendChild(el("div", "hint", sig.error || ""));
      }
      tr.appendChild(status);

      if (sig.status === "active") {
        const strip = el("div");
        CanlabBits.renderSignalBits(strip, decoded.payload_bytes, sig);
        layout.appendChild(strip);
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  /* ------------------------------------------------------------------ */
  /* Compare                                                            */
  /* ------------------------------------------------------------------ */

  function renderCompareSelectors() {
    const frameSelect = $("compare-frame");
    const keys = dbcKeys();
    const left = $("compare-left");
    const right = $("compare-right");
    [left, right].forEach((select, index) => {
      const keep = select.value;
      select.textContent = "";
      keys.forEach((key) => {
        const option = el("option", null, key);
        option.value = key;
        select.appendChild(option);
      });
      if (keys.includes(keep)) select.value = keep;
      else if (keys[index]) select.value = keys[index];
      else if (keys[0]) select.value = keys[0];
    });
    if (state.compare.left) left.value = state.compare.left;
    if (state.compare.right) right.value = state.compare.right;
    state.compare.left = left.value;
    state.compare.right = right.value;

    const keepFrame = frameSelect.value;
    frameSelect.textContent = "";
    (state.session.frames || []).forEach((frame) => {
      const option = el("option", null,
        `#${frame.seq} ${frame.label || frame.frame_id_hex} (${frame.payload_hex})`);
      option.value = String(frame.seq);
      frameSelect.appendChild(option);
    });
    if (keepFrame && [...frameSelect.options].some((o) => o.value === keepFrame)) {
      frameSelect.value = keepFrame;
    } else if (state.session.frames.length) {
      frameSelect.value = String(state.session.frames[state.session.frames.length - 1].seq);
    }
    if (frameSelect.value) state.compare.seq = Number(frameSelect.value);
  }

  async function runCompare() {
    const seq = Number($("compare-frame").value);
    const left = $("compare-left").value;
    const right = $("compare-right").value;
    state.compare = { seq, left, right };
    const frame = (state.session.frames || []).find((f) => f.seq === seq);
    const grid = $("compare-grid");
    const diffWrap = $("compare-diff");
    grid.textContent = "";
    diffWrap.textContent = "";
    if (!frame) {
      diffWrap.appendChild(el("p", "empty", "Select a recorded frame."));
      return;
    }
    if (!left || !right) {
      diffWrap.appendChild(el("p", "empty", "Load and select two databases."));
      return;
    }

    // Definition-level differences.
    const diff = await api("/api/compare", {
      method: "POST", body: JSON.stringify({ left, right })
    });
    if (diff && diff.ok) {
      const relevant = diff.differences.filter((d) =>
        d.frame_id === frame.frame_id && Boolean(d.is_extended) === frame.is_extended);
      const list = el("div", "diff-list");
      const header = el("h3", null,
        `Definition differences for ${frame.frame_id_hex} (${relevant.length})`);
      diffWrap.appendChild(header);
      const allDetails = el("details");
      allDetails.appendChild(el("summary", "hint",
        `All definition differences between ${left} and ${right} (${diff.differences.length})`));
      const allList = el("div", "diff-list");
      diff.differences.forEach((d) => allList.appendChild(renderDiff(d)));
      allDetails.appendChild(allList);
      diffWrap.appendChild(allDetails);
      if (!relevant.length) {
        list.appendChild(el("p", "hint",
          "The two files agree on this message's definitions; any difference below is in the frame verdict."));
      }
      relevant.forEach((d) => list.appendChild(renderDiff(d)));
      diffWrap.appendChild(list);
    }

    [left, right].forEach((key, index) => {
      const decoded = frame.decodes[key];
      const col = el("div", "compare-col");
      col.dataset.side = index === 0 ? "left" : "right";
      const head = el("div", "compare-col-head");
      head.appendChild(el("strong", null, key));
      if (decoded && decoded.message) head.appendChild(el("span", "hint", decoded.message));
      const verdict = frame.verdicts[key];
      if (verdict) {
        head.appendChild(el("span", "pill " + (verdict.accepted ? "good" : "critical"),
          verdict.accepted ? "accepted" : "rejected"));
      }
      col.appendChild(head);
      const body = el("div", "compare-body");
      if (!decoded) {
        body.appendChild(el("p", "empty", "This database was not loaded when the frame arrived."));
      } else {
        const bitmap = el("div", "bitmap-wrap");
        CanlabBits.renderBitMap(bitmap, decoded.payload_bytes, decoded.signals,
          null, { legend: true });
        body.appendChild(bitmap);
        body.appendChild(renderDecodeTable(decoded, () => {}));
        const oracle = frame.oracle ? frame.oracle[key] : null;
        if (oracle && (!oracle.ok || (oracle.disagreement || []).length)) {
          body.appendChild(renderLibraryPanel(oracle));
        }
      }
      col.appendChild(body);
      grid.appendChild(col);
    });
  }

  function renderDiff(diff) {
    const item = el("div", "diff-item");
    item.appendChild(el("span", "diff-kind", diff.kind.replace(/_/g, " ")));
    if (diff.changes) {
      diff.changes.forEach((change) => {
        const row = el("div");
        row.appendChild(el("span", "diff-field", change.signal ? `${diff.message}.${change.signal}.${change.field}` : `${diff.message}.${change.field}`));
        row.appendChild(el("span", "diff-values",
          `  ${JSON.stringify(change.left)} → ${JSON.stringify(change.right)}`));
        item.appendChild(row);
      });
    } else {
      item.appendChild(el("span", "diff-field", diff.message || ""));
      item.appendChild(el("div", "diff-values", diff.detail || ""));
    }
    return item;
  }

  /* ------------------------------------------------------------------ */
  /* Export                                                             */
  /* ------------------------------------------------------------------ */

  function renderExportGroups() {
    const wrap = $("export-groups");
    wrap.textContent = "";
    Object.keys(state.exportGroups).forEach((group) => {
      const row = el("label", "export-group");
      const box = el("input");
      box.type = "checkbox";
      box.value = group;
      box.checked = state.defaultExportGroups.includes(group);
      box.dataset.group = group;
      row.appendChild(box);
      const text = el("span");
      text.appendChild(el("strong", null, group));
      text.appendChild(el("small", null, state.exportGroups[group].join(", ")));
      row.appendChild(text);
      wrap.appendChild(row);
    });
    updateExportNote();
  }

  function selectedGroups() {
    return [...document.querySelectorAll("#export-groups input:checked")]
      .map((box) => box.value);
  }

  function updateExportNote() {
    const groups = selectedGroups();
    const note = groups.includes("raw")
      ? "Raw integers will be included: the export can be replayed against the DBC."
      : "Raw integers will be omitted: the export carries interpreted values only.";
    $("export-note").textContent = note;
  }

  async function previewExport() {
    const groups = selectedGroups();
    if (!groups.length) {
      $("export-preview").textContent = "Select at least one column group.";
      return;
    }
    const query = groups.map((g) => "group=" + encodeURIComponent(g)).join("&");
    const response = await fetch("/api/export?format=csv&" + query);
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      $("export-preview").textContent = payload ? payload.error : "Export failed.";
      return;
    }
    const text = await response.text();
    $("export-preview").textContent = text.split("\n").slice(0, 30).join("\n");
  }

  async function download(format) {
    const groups = selectedGroups();
    if (!groups.length) return toast("Select at least one column group", true);
    const query = groups.map((g) => "group=" + encodeURIComponent(g)).join("&");
    const link = document.createElement("a");
    link.href = `/api/export?format=${format}&${query}`;
    link.download = format === "json" ? "canlab-frames.json" : "canlab-frames.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  /* ------------------------------------------------------------------ */
  /* Views & wiring                                                     */
  /* ------------------------------------------------------------------ */

  function renderAll() {
    renderDatabases();
    renderFixtureOptions();
    renderSendSelectors();
    renderFrameList();
    renderDetail();
    renderCompareSelectors();
    renderExportGroups();
    $("header-status").textContent =
      `${dbcKeys().length} database(s) · ${state.session.count} frame(s)`;
    if (state.view === "send") requestPreview();
  }

  function setView(view) {
    state.view = view;
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.setAttribute("aria-selected", tab.dataset.view === view ? "true" : "false");
    });
    ["send", "receive", "compare", "export"].forEach((name) => {
      $("view-" + name).hidden = name !== view;
    });
    if (view === "receive" && state.selectedSeq === null && state.session.frames.length) {
      state.selectedSeq = state.session.frames[state.session.frames.length - 1].seq;
      renderFrameList();
      renderDetail();
    }
    if (view === "compare") renderCompareSelectors();
    if (view === "export") renderExportGroups();
  }

  function wire() {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => setView(tab.dataset.view));
    });

    $("load-fixture").addEventListener("click", async () => {
      const filename = $("fixture-select").value;
      if (!filename) return;
      const result = await api("/api/dbc/load-fixture", {
        method: "POST", body: JSON.stringify({ filename })
      });
      if (!result.ok) return toast(result.error || "load failed", true);
      await refresh(false);
      toast(`Loaded ${filename}`);
    });

    $("dbc-file").addEventListener("change", async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      const text = await file.text();
      const result = await api("/api/dbc/upload", {
        method: "POST", body: JSON.stringify({ filename: file.name, text })
      });
      if (!result.ok) return toast(result.error || "upload failed", true);
      await refresh();
      toast(`Loaded ${file.name}`);
      event.target.value = "";
    });

    $("upload-paste").addEventListener("click", async () => {
      const text = $("dbc-paste").value;
      if (!text.trim()) return toast("Paste a DBC definition first", true);
      const result = await api("/api/dbc/upload", {
        method: "POST", body: JSON.stringify({ filename: "pasted.dbc", text })
      });
      if (!result.ok) return toast(result.error || "load failed", true);
      $("dbc-paste").value = "";
      await refresh();
      toast("Loaded pasted DBC");
    });

    $("send-dbc").addEventListener("change", () => {
      state.send.dbc = $("send-dbc").value;
      state.send.values = {};
      state.send.nestedMux = {};
      renderSendSelectors();
      schedulePreview();
    });
    $("send-message").addEventListener("change", () => {
      state.send.values = {};
      state.send.nestedMux = {};
      state.send.dlcTouched = false;
      syncDlcFromMessage();
      renderSignalEditor();
      schedulePreview();
    });
    $("send-dlc").addEventListener("input", () => {
      state.send.dlcTouched = true;
      schedulePreview();
    });
    $("send-mux").addEventListener("input", schedulePreview);
    $("send-force").addEventListener("change", schedulePreview);
    $("send-preview-btn").addEventListener("click", requestPreview);

    $("send-boundary").addEventListener("change", () => {
      applyBoundary($("send-boundary").value);
    });

    $("send-btn").addEventListener("click", async () => {
      const payload = sendPayload();
      if (!payload) return;
      const result = await api("/api/frame/send", {
        method: "POST", body: JSON.stringify(payload)
      });
      if (!result.ok) {
        renderPreviewFindings(result);
        return toast(`Sender rejected the frame (${result.stage})`, true);
      }
      await refresh();
      toast(`Sent ${result.frame.payload_hex}`);
      setView("receive");
      selectFrame(result.frame.seq);
    });

    $("load-samples").addEventListener("click", async () => {
      const result = await api("/api/samples/load", {
        method: "POST", body: JSON.stringify({})
      });
      if (!result.ok) return toast(result.error || "could not load samples", true);
      await refresh();
      const failed = (result.results || []).filter((r) => !r.ok);
      toast(`Loaded ${result.session.count} sample frame(s)` +
        (failed.length ? `, ${failed.length} rejected by the sender` : ""));
      setView("receive");
    });

    $("clear-frames").addEventListener("click", async () => {
      await api("/api/frames/clear", { method: "POST", body: "{}" });
      state.selectedSeq = null;
      await refresh();
    });

    $("import-toggle").addEventListener("click", () => {
      $("import-form").hidden = !$("import-form").hidden;
      const select = $("import-dbc");
      select.textContent = "";
      dbcKeys().forEach((key) => {
        const option = el("option", null, key);
        option.value = key;
        select.appendChild(option);
      });
    });

    $("import-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const result = await api("/api/frame/import", {
        method: "POST",
        body: JSON.stringify({
          dbc: $("import-dbc").value,
          frame_id: $("import-id").value,
          is_extended: $("import-ext").checked,
          payload_hex: $("import-hex").value
        })
      });
      if (!result.ok) return toast(result.error || "import failed", true);
      await refresh();
      setView("receive");
      selectFrame(result.frame.seq);
    });

    $("compare-run").addEventListener("click", runCompare);
    $("compare-frame").addEventListener("change", runCompare);

    $("export-csv").addEventListener("click", () => download("csv"));
    $("export-json").addEventListener("click", () => download("json"));
    document.addEventListener("change", (event) => {
      if (event.target.closest("#export-groups")) {
        updateExportNote();
        previewExport();
      }
    });
  }

  function applyBoundary(kind) {
    const note = $("boundary-note");
    note.hidden = true;
    note.textContent = "";
    if (!kind) return;
    const { key, msg } = currentMessage();
    const db = state.session.dbcs[key];
    if (!msg || !db) return;
    const values = {};
    const lines = [];
    // Boundary values need the full signal definitions; the preview decode
    // returns them.  Use the last successful preview when available.
    const decoded = state.send.preview && state.send.preview.decode;
    if (!decoded) return;
    decoded.signals.forEach((sig) => {
      if (sig.status !== "active" || sig.is_multiplexer) return;
      if (kind === "min" && sig.minimum !== null) {
        values[sig.name] = sig.minimum;
        lines.push(`${sig.name} = ${sig.minimum} ${sig.unit || ""}`.trim());
      } else if (kind === "max" && sig.maximum !== null) {
        values[sig.name] = sig.maximum;
        lines.push(`${sig.name} = ${sig.maximum} ${sig.unit || ""}`.trim());
      } else if (kind === "raw_min") {
        const rawMin = sig.is_signed ? -(2 ** (sig.length - 1)) : 0;
        const physical = rawMin * sig.scale + sig.offset;
        values[sig.name] = physical;
        lines.push(`${sig.name} raw ${rawMin} → ${fmtNum(physical)} ${sig.unit || ""}`.trim());
      }
    });
    state.send.values = Object.assign({}, state.send.values, values);
    note.hidden = false;
    note.textContent = lines.length
      ? "Boundary values applied: " + lines.join(" · ")
      : "No signal in this message has the requested boundary.";
    renderSignalEditor();
    schedulePreview();
  }

  wire();
  setView("send");
  refresh().then(() => {
    if (state.samples.length) {
      $("header-status").textContent += " · samples available";
    }
  }).catch((err) => toast(err.message, true));
})();
