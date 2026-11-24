// DOM helpers. Elements are built with createElement / createElementNS and
// text only; no markup string is ever parsed. Artifact values reach the page
// as text nodes (the textContent equivalent).

// Standard SVG namespace identifier. It is not a network request.
export const SVG_NS = "http://www.w3.org/2000/svg";

const ATTR_ALLOWLIST = new Set([
  "id", "role", "tabindex", "type", "lang", "href", "for", "open", "title",
  "scope", "colspan", "rel", "name", "disabled", "hidden",
  // SVG presentation and geometry
  "viewBox", "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r",
  "points", "d", "fill", "stroke", "stroke-width", "stroke-dasharray",
  "text-anchor", "transform", "preserveAspectRatio", "focusable",
]);

function attrAllowed(name) {
  return ATTR_ALLOWLIST.has(name) || /^aria-[a-z]+$/.test(name) || /^data-[a-z-]+$/.test(name);
}

function hrefAllowed(value) {
  // Same-origin absolute paths and fragments only.
  const s = String(value);
  return (s.charAt(0) === "/" && s.charAt(1) !== "/") || s.charAt(0) === "#";
}

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    if (Array.isArray(child)) {
      appendChildren(node, child);
    } else if (typeof child === "object") {
      node.append(child);
    } else {
      node.append(text(child));
    }
  }
}

// el(tag, {class, attrs, svg}, ...children)
export function el(tag, options, ...children) {
  const opts = options || {};
  const node = opts.svg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  if (opts.class) {
    node.setAttribute("class", String(opts.class));
  }
  const attrs = opts.attrs || {};
  for (const name of Object.keys(attrs)) {
    if (!attrAllowed(name)) {
      throw new Error("attribute not allowed: " + name);
    }
    if (name === "href" && !hrefAllowed(attrs[name])) {
      throw new Error("href not allowed");
    }
    node.setAttribute(name, String(attrs[name]));
  }
  appendChildren(node, children);
  return node;
}

export function text(value) {
  return document.createTextNode(String(value));
}

export function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
  return node;
}

// An element whose only content is the given text (textContent).
export function textEl(tag, className, value, attrs) {
  const node = el(tag, { class: className, attrs: attrs });
  node.textContent = String(value);
  return node;
}
