/* =========================================================================
   A small Markdown -> HTML renderer.
   Deliberately hand-rolled instead of pulling in a CDN library: it keeps the
   app dependency-free and, more importantly, keeps the security story simple
   — every character of model output is HTML-escaped *first*, and only the
   tags this file generates can ever reach the DOM.
   ========================================================================= */
(function (global) {
  "use strict";

  // Placeholder sentinel. A NUL byte can never appear in model output, so
  // tokens built from it can't collide with real text.
  const NUL = "\u0000";
  const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const BLOCK_TOKEN_RE = /^\s*(\u0000CB\d+\u0000)\s*$/;

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Drop the indentation a fenced block inherits from its list item. */
  function dedent(body) {
    const lines = body.split("\n").filter((l) => l.trim());
    if (!lines.length) return body;
    const pad = Math.min(...lines.map((l) => l.match(/^ */)[0].length));
    if (!pad) return body;
    return body.split("\n").map((l) => l.slice(pad)).join("\n");
  }

  function safeUrl(url) {
    const trimmed = url.trim();
    if (/^(https?:|mailto:|#|\/)/i.test(trimmed)) return trimmed;
    return "#";
  }

  // ---- inline: emphasis, links, images-as-links -------------------------
  function inline(text) {
    return text
      // links  [label](url)
      .replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g,
        (_, label, url) =>
          `<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`)
      // bold + italic
      .replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?;:]|$)/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?;:]|$)/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  }

  // ---- table helper ------------------------------------------------------
  function isTableSeparator(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  }

  function splitRow(line) {
    return line
      .replace(/^\s*\|/, "")
      .replace(/\|\s*$/, "")
      .split("|")
      .map((c) => c.trim());
  }

  // ---- block parser ------------------------------------------------------
  function blocks(src) {
    const lines = src.split("\n");
    const out = [];
    let i = 0;

    const flushParagraph = (buf) => {
      if (buf.length) out.push(`<p>${inline(buf.join("<br>"))}</p>`);
      buf.length = 0;
    };

    const para = [];

    while (i < lines.length) {
      const line = lines[i];

      // blank
      if (!line.trim()) { flushParagraph(para); i++; continue; }

      // heading
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        flushParagraph(para);
        const level = Math.min(h[1].length + 1, 6); // h1 -> h2, keeps page hierarchy sane
        out.push(`<h${level}>${inline(h[2].trim())}</h${level}>`);
        i++; continue;
      }

      // horizontal rule
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
        flushParagraph(para);
        out.push("<hr>");
        i++; continue;
      }

      // blockquote (consume consecutive > lines)
      if (/^\s*&gt;\s?/.test(line)) {
        flushParagraph(para);
        const quote = [];
        while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*&gt;\s?/, ""));
          i++;
        }
        out.push(`<blockquote>${blocks(quote.join("\n"))}</blockquote>`);
        continue;
      }

      // table
      if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushParagraph(para);
        const header = splitRow(line);
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          body.push(splitRow(lines[i]));
          i++;
        }
        const head = header.map((c) => `<th>${inline(c)}</th>`).join("");
        const rows = body
          .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
          .join("");
        out.push(`<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`);
        continue;
      }

      // lists (unordered / ordered, flat — nesting is rare in chat replies)
      const listMatch = line.match(LIST_RE);
      if (listMatch) {
        flushParagraph(para);
        const ordered = /\d/.test(listMatch[2]);
        const items = [];

        while (i < lines.length) {
          const m = lines[i].match(LIST_RE);

          if (!m) {
            // Blank lines — and fenced code blocks that belong to the item we
            // just closed — shouldn't end the list and restart numbering at 1.
            // Peek ahead; if another item of the same kind follows, absorb what
            // we skipped into the previous <li> and carry on.
            const carried = [];
            let j = i;
            while (j < lines.length) {
              if (!lines[j].trim()) { j++; continue; }
              const token = lines[j].match(BLOCK_TOKEN_RE);
              if (token) { carried.push(token[1]); j++; continue; }
              break;
            }
            const next = j < lines.length ? lines[j].match(LIST_RE) : null;
            if (j > i && next && /\d/.test(next[2]) === ordered) {
              if (carried.length && items.length) {
                items[items.length - 1] = items[items.length - 1]
                  .replace(/<\/li>$/, carried.join("") + "</li>");
              }
              i = j;
              continue;
            }
            break;
          }

          if (/\d/.test(m[2]) !== ordered) break;

          let content = m[3];
          i++;
          // fold plain continuation lines into the same <li>
          while (
            i < lines.length &&
            lines[i].trim() &&
            !lines[i].match(LIST_RE) &&
            !/^(#{1,6})\s/.test(lines[i])
          ) {
            content += (BLOCK_TOKEN_RE.test(lines[i]) ? "" : " ") + lines[i].trim();
            i++;
          }
          items.push(`<li>${inline(content)}</li>`);
        }

        out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
        continue;
      }

      para.push(line);
      i++;
    }

    flushParagraph(para);
    return out.join("");
  }

  /**
   * render(markdown) -> HTML string
   * Safe to assign to innerHTML: the input is escaped before any parsing runs.
   */
  function render(src) {
    if (!src) return "";

    const codeBlocks = [];
    const inlineCode = [];

    // 1. pull fenced code out first so nothing inside it gets parsed
    let text = String(src).replace(
      /```([a-zA-Z0-9+#._-]*)\n?([\s\S]*?)```/g,
      (_, lang, body) => {
        codeBlocks.push({ lang: lang || "code", body: dedent(body.replace(/\s+$/, "")) });
        return `${NUL}CB${codeBlocks.length - 1}${NUL}`;
      }
    );

    // 2. then inline code
    text = text.replace(/`([^`\n]+)`/g, (_, body) => {
      inlineCode.push(body);
      return `${NUL}IC${inlineCode.length - 1}${NUL}`;
    });

    // 3. escape everything that's left, then parse block structure
    let html = blocks(escapeHtml(text));

    // 4. put the code back, escaped, with a copy button on fenced blocks
    html = html.replace(new RegExp(`${NUL}IC(\\d+)${NUL}`, "g"), (_, n) =>
      `<code>${escapeHtml(inlineCode[+n])}</code>`
    );

    html = html.replace(new RegExp(`(?:<p>)?${NUL}CB(\\d+)${NUL}(?:</p>)?`, "g"), (_, n) => {
      const { lang, body } = codeBlocks[+n];
      return (
        `<div class="code-block" data-code="${escapeHtml(body)}">` +
          `<div class="code-head"><span>${escapeHtml(lang)}</span>` +
          `<button type="button" class="code-copy">copy</button></div>` +
          `<pre><code>${escapeHtml(body)}</code></pre>` +
        `</div>`
      );
    });

    return html;
  }

  global.MD = { render, escapeHtml };
})(window);
