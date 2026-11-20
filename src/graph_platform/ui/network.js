const SHAPE = { person: "ellipse", merchant: "rectangle", device: "diamond", identifier: "triangle", item: "rectangle" };

export default function ({ data, parentElement, setTriggerValue }) {
  const box = parentElement.querySelector(".gp-net");
  const root = getComputedStyle(document.documentElement);
  const ink = root.getPropertyValue("--st-text-color").trim() || "#1a1a1a";
  const paper = root.getPropertyValue("--st-background-color").trim() || "#ffffff";
  const font = getComputedStyle(document.body).fontFamily;
  const faint = "#8a8a84", alarm = "#b3261e";
  const text = (n) => (n.note ? `${n.label}\n${n.note}` : n.label);
  const party = (n) => n.kind === "person" || n.kind === "merchant";

  // parties on an outer ellipse, what they share or own on an inner one, the party being looked at in the middle
  box.style.height = `${data.height}px`;
  const w = box.clientWidth, h = data.height, at = {};
  const ring = (nodes, rx, ry) =>
    nodes.forEach((n, i) => {
      const a = -Math.PI / 2 + (2 * Math.PI * i) / nodes.length;
      at[n.id] = { x: w / 2 + rx * w * Math.cos(a), y: h / 2 - 12 + ry * h * Math.sin(a) };
    });
  ring(data.nodes.filter((n) => party(n) && !n.focus), 0.42, 0.4);
  ring(data.nodes.filter((n) => !party(n)), data.mode === "group" ? 0.15 : 0.23, data.mode === "group" ? 0.17 : 0.25);
  parentElement.querySelector(".gp-legend").style.display = data.legend ? "flex" : "none";
  ring(data.nodes.filter((n) => n.focus), 0, 0);
  const quiet = data.edges.length > 16 ? "quiet" : "";

  box._cy?.destroy();
  const cy = (box._cy = globalThis.cytoscape({
    container: box,
    elements: [
      ...data.nodes.map((n) => ({ data: { ...n, text: text(n) }, position: at[n.id] })),
      ...data.edges.map((e, i) => ({ data: { ...e, id: `e${i}` }, classes: quiet })),
    ],
    style: [
      { selector: "node", style: {
          shape: (n) => SHAPE[n.data("kind")], width: 22, height: 22, "background-color": paper, "border-width": 1.2, "border-color": ink,
          label: "data(text)", "font-family": font, "font-size": 12, color: ink, "text-wrap": "wrap", "text-max-width": 130,
          "text-valign": "bottom", "text-margin-y": 5, "line-height": 1.25,
          "text-background-color": paper, "text-background-opacity": 0.85, "text-background-padding": 1 } },
      { selector: "node[kind = 'item']", style: { width: 20, height: 7, "background-color": ink } },
      { selector: "node[kind = 'device'], node[kind = 'identifier']", style: { width: 18, height: 18 } },
      { selector: "node[?focus]", style: { "background-color": ink, width: 30, height: 30, "font-size": 13, "font-weight": 600 } },
      { selector: "node[?flag]", style: { "border-color": alarm, "border-width": 3, "border-style": "double", color: alarm } },
      { selector: "node[?suggest]", style: { "background-color": paper, "border-style": "dashed", height: 11 } },
      { selector: "edge", style: {
          width: 1, "line-color": faint, "curve-style": "bezier", label: "data(label)", "font-family": font, "font-size": 11, color: ink,
          "text-background-color": paper, "text-background-opacity": 1, "text-background-padding": 1 } },
      { selector: "edge[kind = 'transfer']", style: {
          "line-color": ink, width: "mapData(weight, 1, 40, 0.8, 3.5)", "target-arrow-shape": "triangle", "target-arrow-color": ink, "arrow-scale": 0.9 } },
      { selector: "edge[kind = 'refer']", style: {
          "line-color": ink, "line-style": "dashed", "line-dash-pattern": [8, 3, 2, 3], "target-arrow-shape": "vee", "target-arrow-color": ink } },
      { selector: "edge[kind = 'device']", style: { "line-style": "dashed", "line-dash-pattern": [5, 4] } },
      { selector: "edge[kind = 'identifier']", style: { "line-style": "dotted" } },
      { selector: "edge[kind = 'suggest']", style: {
          "line-color": ink, "line-style": "dashed", "line-dash-pattern": [3, 3], "target-arrow-shape": "vee", "target-arrow-color": ink } },
      { selector: "edge.quiet", style: { label: "" } },
    ],
    layout: { name: "preset", fit: false },
    userZoomingEnabled: false,
    userPanningEnabled: false,
  }));
  cy.on("mouseover", "node", (e) => e.target.connectedEdges().removeClass("quiet"));
  cy.on("mouseout", "node", (e) => quiet && e.target.connectedEdges().addClass("quiet"));
  cy.on("tap", "node[?open]", (e) => setTriggerValue("picked", e.target.id()));
  cy.on("mouseover", "node[?open]", () => (box.style.cursor = "pointer"));
  cy.on("mouseout", "node", () => (box.style.cursor = "default"));
  return () => cy.destroy();
}
