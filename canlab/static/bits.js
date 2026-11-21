/* CANLab - bit map and layout helpers.
 *
 * Rendering rules that matter for correctness:
 *   - The grid always shows eight rows (bytes) and eight columns (bits,
 *     MSB on the left), even for short frames, so a bit that is missing
 *     from the payload is visibly empty rather than silently absent.
 *   - A cell carries its linear bit offset in a data attribute.  The
 *     server sends `bit_offsets` per signal in MSB-first order; we only
 *     draw, we never recompute the layout here.
 *   - "Not applicable" bits are drawn with a cross-hatch, never as zeros.
 */
(function () {
  "use strict";

  const SLOTS = [1, 2, 3];

  function bitCell(payloadBits, offset, signalIndex, state) {
    const byteIndex = Math.floor(offset / 8);
    const bitIndex = offset % 8;
    const bit = payloadBits[offset];
    const cell = document.createElement("div");
    cell.className = "bit-cell";
    cell.dataset.offset = String(offset);
    cell.dataset.bit = bit === undefined ? "" : String(bit);
    if (bit === undefined) {
      cell.classList.add("bit-missing");
      cell.title = `bit ${offset} (byte ${byteIndex} bit ${bitIndex}) - not present in the frame`;
    } else {
      cell.textContent = bit;
      cell.classList.add(bit ? "bit-one" : "bit-zero");
      cell.title = `bit ${offset} (byte ${byteIndex} bit ${bitIndex}) = ${bit}`;
    }
    if (signalIndex !== undefined && signalIndex !== null) {
      cell.classList.add("bit-signal");
      cell.dataset.slot = String(SLOTS[signalIndex % SLOTS.length]);
      if (state === "not_applicable") cell.classList.add("bit-na");
      if (state === "error") cell.classList.add("bit-error");
    }
    return cell;
  }

  function renderBitMap(root, payloadBytes, signals, activeSignal, opts) {
    opts = opts || {};
    const payloadBits = [];
    for (let byte = 0; byte < payloadBytes.length; byte += 1) {
      for (let bit = 0; bit < 8; bit += 1) {
        payloadBits[byte * 8 + bit] = (payloadBytes[byte] >> bit) & 1;
      }
    }
    root.textContent = "";
    const digits = document.createElement("div");
    digits.className = "bitmap-digits";
    digits.appendChild(document.createElement("span"));
    for (let bit = 7; bit >= 0; bit -= 1) {
      const label = document.createElement("span");
      label.className = "bitmap-digit";
      label.textContent = "b" + bit;
      digits.appendChild(label);
    }
    root.appendChild(digits);

    // Always draw 8 physical byte rows; rows beyond the payload are
    // marked missing.  A definition can be 4 bytes inside an 8-byte frame,
    // and a frame can be longer than the definition.
    const rows = Math.max(8, Math.ceil(payloadBytes.length / 8) * 8 / 8);
    const signalIndexByName = new Map(
      (signals || []).map((sig, index) => [sig.name, index])
    );
    let activeIndex = null;
    if (activeSignal) activeIndex = signalIndexByName.get(activeSignal) ?? null;

    for (let row = 0; row < rows; row += 1) {
      const line = document.createElement("div");
      line.className = "bitmap-row";
      const label = document.createElement("span");
      label.className = "bitmap-byte-label";
      label.textContent = "byte " + row;
      label.title = "byte " + row + (
        row >= payloadBytes.length ? " - beyond the received frame" : ""
      );
      line.appendChild(label);
      for (let bit = 7; bit >= 0; bit -= 1) {
        const offset = row * 8 + bit;
        let idx, state;
        if (activeIndex !== null) {
          const sig = signals[activeIndex];
          const offsets = sig.bit_offsets || [];
          if (offsets.includes(offset)) {
            idx = activeIndex;
            state = sig.status;
          }
        }
        line.appendChild(bitCell(payloadBits, offset, idx, state));
      }
      root.appendChild(line);
    }

    if (opts.legend !== false) {
      const legend = document.createElement("div");
      legend.className = "bitmap-legend";
      legend.innerHTML = [
        '<span class="legend-item"><span class="swatch bit-one"></span>1</span>',
        '<span class="legend-item"><span class="swatch bit-zero"></span>0</span>',
        '<span class="legend-item"><span class="swatch bit-missing"></span>not in frame</span>',
        '<span class="legend-item"><span class="swatch bit-na"></span>not applicable</span>',
        '<span class="legend-item"><span class="swatch bit-error"></span>unreadable</span>'
      ].join("");
      root.appendChild(legend);
    }
  }

  /* A horizontal strip of the same cells, MSB-first, for one signal.
   * Used where the full 8x8 grid would be too tall. */
  function renderSignalBits(root, payloadBytes, signal) {
    root.textContent = "";
    const payloadBits = [];
    for (let byte = 0; byte < payloadBytes.length; byte += 1) {
      for (let bit = 0; bit < 8; bit += 1) {
        payloadBits[byte * 8 + bit] = (payloadBytes[byte] >> bit) & 1;
      }
    }
    const strip = document.createElement("div");
    strip.className = "bit-strip";
    (signal.bit_offsets || []).forEach((offset, index) => {
      if (index > 0) {
        const prev = signal.bit_offsets[index - 1];
        if (Math.floor(prev / 8) !== Math.floor(offset / 8)) {
          const gap = document.createElement("span");
          gap.className = "bit-strip-gap";
          gap.textContent = "|";
          strip.appendChild(gap);
        }
      }
      const cell = bitCell(payloadBits, offset, 0, signal.status);
      cell.removeAttribute("data-slot");
      cell.classList.add("bit-strip-cell");
      strip.appendChild(cell);
    });
    root.appendChild(strip);
  }

  /* Stepped layout preview for one signal: which byte each bit lands in. */
  function layoutSegments(signal) {
    const segments = [];
    (signal.bit_offsets || []).forEach((offset, index) => {
      const byteIndex = Math.floor(offset / 8);
      const bitIndex = offset % 8;
      const last = segments[segments.length - 1];
      if (last && last.byte === byteIndex) {
        last.bits.push(bitIndex);
      } else {
        segments.push({ byte: byteIndex, bits: [bitIndex], firstIndex: index });
      }
    });
    return segments;
  }

  window.CanlabBits = {
    renderBitMap,
    renderSignalBits,
    layoutSegments,
    SLOTS
  };
})();
