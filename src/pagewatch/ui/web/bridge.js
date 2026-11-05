/* PageWatch viewer bridge (spec: Desktop UI → Filter assistant).

   Reports what the user selected in the Highlighted or New view, and the DOM path of the
   element(s) holding it, to the UI over QWebChannel:

     * Alt + select       release the mouse button with Alt held after selecting text
     * right-click        on a selection, or on an element (no selection needed)
     * window.__pwReport  what the keyboard shortcut calls

   The engine tags every element of those views with data-pw-path (the path the extractor gives
   blocks); this script only reads it. It runs in an isolated world, so the page's own scripts
   (there are none: the engine strips them) can neither see nor call it, and it does nothing in a
   document without data-pw-path (the Text diff view, error pages). */
(function () {
  "use strict";
  if (window.__pwBridgeInstalled) { return; }
  window.__pwBridgeInstalled = true;
  var bridge = null;

  function pathOf(node) {
    while (node && node.nodeType !== 1) { node = node.parentNode; }
    var el = node && node.closest ? node.closest("[data-pw-path]") : null;
    return el ? el.getAttribute("data-pw-path") : null;
  }

  function textNodes() {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var out = [];
    while (walker.nextNode()) {
      if (walker.currentNode.nodeValue.trim()) { out.push(walker.currentNode); }
    }
    return out;
  }

  /* A triple-click ends at offset 0 of the NEXT block, and a drag can start at the very end of
     the previous one. The user meant the text in between, so move each end onto it. */
  function innerEnds(range) {
    var start = range.startContainer, end = range.endContainer;
    var needStart = start.nodeType !== 3 ||
      range.startOffset >= start.nodeValue.length;
    var needEnd = end.nodeType !== 3 || range.endOffset === 0;
    if (!needStart && !needEnd) { return [start, end]; }
    var inside = textNodes().filter(function (t) {
      return range.comparePoint(t, 0) === 0 || t === start;
    });
    if (needStart && inside.length) { start = inside[0]; }
    if (needEnd && inside.length) { end = inside[inside.length - 1]; }
    return [start, end];
  }

  var BLOCK = /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|BR|CAPTION|DD|DETAILS|DIV|DL|DT|FIGCAPTION|FIGURE|FOOTER|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|SUMMARY|TABLE|TBODY|TFOOT|THEAD|TR|UL)$/;

  /* The selected text as the page's CURRENT text: what the highlighted view shows struck through
     (removed text, <del class="pw-del">) is no longer on the page and is left out; block
     elements start new lines. (String(selection) would include the struck-through text.) */
  function selectedText(range) {
    var holder = document.createElement("div");
    holder.appendChild(range.cloneContents());
    var gone = holder.querySelectorAll("del.pw-del, .pw-removed");
    for (var i = 0; i < gone.length; i++) { gone[i].remove(); }
    var out = [];
    (function walk(node) {
      for (var c = node.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 3) {
          out.push(c.nodeValue);
        } else if (c.nodeType === 1) {
          var block = BLOCK.test(c.tagName);
          if (block) { out.push("\n"); }
          walk(c);
          if (block) { out.push("\n"); }
        }
      }
    })(holder);
    return out.join("").replace(/[ \t\u00a0]+/g, " ").replace(/ ?\n ?/g, "\n")
      .replace(/\n+/g, "\n").trim();
  }

  function report(via, ev) {
    if (!bridge || !document.querySelector("[data-pw-path]")) { return false; }
    var sel = window.getSelection();
    var text = sel && sel.rangeCount && !sel.isCollapsed ? selectedText(sel.getRangeAt(0)) : "";
    var path = null, endPath = null;
    if (text) {
      var ends = innerEnds(sel.getRangeAt(0));
      path = pathOf(ends[0]);
      endPath = pathOf(ends[1]);
    } else {
      text = "";
      path = pathOf(ev && ev.target ? ev.target : null);
    }
    if (!path) { return false; }
    if (endPath === path) { endPath = null; }
    bridge.selected(JSON.stringify({
      via: via, text: text.slice(0, 4000), path: path, end_path: endPath,
      x: ev ? ev.clientX : 0, y: ev ? ev.clientY : 0
    }));
    return true;
  }

  document.addEventListener("mouseup", function (ev) {
    if (ev.altKey && ev.button === 0) { report("alt", ev); }
  });
  document.addEventListener("contextmenu", function (ev) {
    if (report("context", ev)) { ev.preventDefault(); }
  });
  window.__pwReport = function (via) { return report(via || "api", null); };

  new QWebChannel(qt.webChannelTransport, function (channel) {
    bridge = channel.objects.pagewatch;
  });
})();
