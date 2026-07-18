const tools = [
  {
    id: "portfolio",
    name: "Portfolio Manager",
    shortName: "Portfolio",
    description: "Review positions and plan rebalancing trades.",
    url: "./portfolio-manager/frontend/",
    icon: "chart",
  },
  {
    id: "mf-tracker",
    name: "MF Tracker",
    shortName: "MF Tracker",
    description: "Track and compare monthly mutual fund portfolios.",
    url: "http://127.0.0.1:5174",
    icon: "layers",
  },
];

const icons = {
  chart: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V9m6 10V5m6 14v-7m4 7H2" /></svg>',
  layers: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></svg>',
};

const elements = {
  shell: document.querySelector(".app-shell"),
  nav: document.querySelector("#toolNav"),
  frame: document.querySelector("#toolFrame"),
  loader: document.querySelector("#loader"),
  name: document.querySelector("#activeToolName"),
  description: document.querySelector("#activeToolDescription"),
  openButton: document.querySelector("#openButton"),
  collapseButton: document.querySelector("#collapseButton"),
  commandMenu: document.querySelector("#commandMenu"),
  commandSearch: document.querySelector("#commandSearch"),
  commandResults: document.querySelector("#commandResults"),
};

let kiteCallback = new URLSearchParams(window.location.search).get("kite");

function toolUrl(tool) {
  if (tool.id !== "portfolio" || !kiteCallback) return tool.url;
  return `${tool.url}?kite=${encodeURIComponent(kiteCallback)}`;
}

function getActiveTool() {
  const id = window.location.hash.slice(1);
  return tools.find((tool) => tool.id === id) || tools[0];
}

function selectTool(tool, updateHash = true) {
  if (updateHash && window.location.hash !== `#${tool.id}`) {
    window.location.hash = tool.id;
    return;
  }

  elements.loader.classList.remove("hidden");
  elements.frame.classList.remove("ready");
  const resolvedUrl = toolUrl(tool);
  elements.frame.src = resolvedUrl;
  elements.frame.title = tool.name;
  elements.name.textContent = tool.name;
  elements.description.textContent = tool.description;
  elements.openButton.href = resolvedUrl;
  document.title = `${tool.name} — Ledger`;

  document.querySelectorAll(".tool-link").forEach((link) => {
    const isActive = link.dataset.tool === tool.id;
    link.classList.toggle("active", isActive);
    link.setAttribute("aria-current", isActive ? "page" : "false");
  });
}

function makeToolButton(tool, className = "tool-link") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.dataset.tool = tool.id;
  button.innerHTML = `${icons[tool.icon]}<span><strong>${tool.name}</strong><small>${tool.description}</small></span>`;
  button.addEventListener("click", () => {
    selectTool(tool);
    elements.commandMenu.close();
  });
  return button;
}

function renderNavigation() {
  const label = document.createElement("p");
  label.className = "nav-label";
  label.textContent = "Workspace";
  elements.nav.appendChild(label);
  tools.forEach((tool) => elements.nav.appendChild(makeToolButton(tool)));

  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.className = "tool-link future-tool";
  addButton.disabled = true;
  addButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg><span><strong>More tools</strong><small>Add them in app.js</small></span>';
  elements.nav.appendChild(addButton);
}

function renderCommands(query = "") {
  const normalized = query.trim().toLowerCase();
  elements.commandResults.replaceChildren();
  tools
    .filter((tool) => `${tool.name} ${tool.description}`.toLowerCase().includes(normalized))
    .forEach((tool) => elements.commandResults.appendChild(makeToolButton(tool, "command-item")));
}

renderNavigation();
renderCommands();
selectTool(getActiveTool(), false);
if (kiteCallback) {
  window.history.replaceState({}, "", `${window.location.pathname}${window.location.hash}`);
  kiteCallback = null;
}

window.addEventListener("hashchange", () => selectTool(getActiveTool(), false));
elements.frame.addEventListener("load", () => {
  elements.loader.classList.add("hidden");
  elements.frame.classList.add("ready");
});

elements.collapseButton.addEventListener("click", () => {
  const collapsed = elements.shell.classList.toggle("collapsed");
  elements.collapseButton.setAttribute("aria-expanded", String(!collapsed));
  elements.collapseButton.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
});

function openCommandMenu() {
  renderCommands();
  elements.commandMenu.showModal();
  elements.commandSearch.value = "";
  elements.commandSearch.focus();
}

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openCommandMenu();
  }
});

elements.commandSearch.addEventListener("input", (event) => renderCommands(event.target.value));
elements.commandMenu.addEventListener("click", (event) => {
  if (event.target === elements.commandMenu) elements.commandMenu.close();
});
