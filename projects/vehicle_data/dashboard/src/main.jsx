/**
 * Dashboard v2 entry point: create the broker link over the store, start the
 * sparkline rings, and render the app shell.
 */

import { render } from "preact";
import "./styles/app.css";
import * as store from "./store.js";
import { createLink } from "./link.js";
import { startRings } from "./app/rings.js";
import { App } from "./app/App.jsx";
import { runtime } from "./app/runtime.js";

const link = createLink({
  fetch: (url, init) => fetch(url, init),
  EventSource: window.EventSource,
  store,
  addEventListener: (type, handler) =>
    (type === "pageshow" ? window : document).addEventListener(type, handler),
  removeEventListener: (type, handler) =>
    (type === "pageshow" ? window : document).removeEventListener(type, handler),
});

runtime.link = link;
startRings();

const root = document.getElementById("app");
root.textContent = "";
render(<App />, root);
link.start();

// Stable hooks for tools/measure.mjs and manual debugging; no behaviour depends on them.
window.__van = { link, store };
