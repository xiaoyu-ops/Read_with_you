import type {
  LiteratureMapEdge,
  LiteraturePaper,
} from "@/lib/api";

export interface LiteratureNodePosition {
  x: number;
  y: number;
}

const GRAPH_WIDTH = 1000;
const GRAPH_HEIGHT = 700;
const GRAPH_PADDING = 70;

function stableUnit(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

export function createLiteratureLayout(
  nodes: LiteraturePaper[],
  edges: LiteratureMapEdge[],
): Record<string, LiteratureNodePosition> {
  if (nodes.length === 0) return {};
  const years = nodes
    .map((node) => node.year)
    .filter((year): year is number => typeof year === "number");
  const minYear = years.length ? Math.min(...years) : 2000;
  const maxYear = years.length ? Math.max(...years) : minYear + 1;
  const yearSpan = Math.max(1, maxYear - minYear);
  const originId = nodes.find((node) => node.role === "origin")?.id || nodes[0].id;
  const positions: Record<string, LiteratureNodePosition> = {};

  nodes.forEach((node, index) => {
    if (node.id === originId) {
      positions[node.id] = { x: GRAPH_WIDTH / 2, y: GRAPH_HEIGHT / 2 };
      return;
    }
    const year = node.year ?? minYear + yearSpan / 2;
    const x = GRAPH_PADDING
      + ((year - minYear) / yearSpan) * (GRAPH_WIDTH - GRAPH_PADDING * 2);
    const unit = stableUnit(`${node.id}:${index}`);
    positions[node.id] = {
      x: x + (stableUnit(`${node.id}:jitter`) - 0.5) * 80,
      y: GRAPH_PADDING + unit * (GRAPH_HEIGHT - GRAPH_PADDING * 2),
    };
  });

  const nodeIds = new Set(nodes.map((node) => node.id));
  const activeEdges = edges.filter(
    (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
  );

  for (let tick = 0; tick < 120; tick += 1) {
    const force: Record<string, LiteratureNodePosition> = Object.fromEntries(
      nodes.map((node) => [node.id, { x: 0, y: 0 }]),
    );
    for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < nodes.length; rightIndex += 1) {
        const left = positions[nodes[leftIndex].id];
        const right = positions[nodes[rightIndex].id];
        const dx = right.x - left.x;
        const dy = right.y - left.y;
        const distanceSquared = Math.max(100, dx * dx + dy * dy);
        const distance = Math.sqrt(distanceSquared);
        const strength = 1750 / distanceSquared;
        const fx = (dx / distance) * strength;
        const fy = (dy / distance) * strength;
        force[nodes[leftIndex].id].x -= fx;
        force[nodes[leftIndex].id].y -= fy;
        force[nodes[rightIndex].id].x += fx;
        force[nodes[rightIndex].id].y += fy;
      }
    }
    activeEdges.forEach((edge) => {
      const source = positions[edge.source];
      const target = positions[edge.target];
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const desired = edge.kind === "similarity" ? 118 : 165;
      const strength = (distance - desired) * (edge.kind === "similarity" ? 0.005 : 0.002);
      const fx = (dx / distance) * strength;
      const fy = (dy / distance) * strength;
      force[edge.source].x += fx;
      force[edge.source].y += fy;
      force[edge.target].x -= fx;
      force[edge.target].y -= fy;
    });
    nodes.forEach((node) => {
      if (node.id === originId) return;
      const year = node.year ?? minYear + yearSpan / 2;
      const yearTarget = GRAPH_PADDING
        + ((year - minYear) / yearSpan) * (GRAPH_WIDTH - GRAPH_PADDING * 2);
      force[node.id].x += (yearTarget - positions[node.id].x) * 0.012;
      force[node.id].y += (GRAPH_HEIGHT / 2 - positions[node.id].y) * 0.0015;
      positions[node.id].x = Math.max(
        GRAPH_PADDING,
        Math.min(GRAPH_WIDTH - GRAPH_PADDING, positions[node.id].x + force[node.id].x),
      );
      positions[node.id].y = Math.max(
        GRAPH_PADDING,
        Math.min(GRAPH_HEIGHT - GRAPH_PADDING, positions[node.id].y + force[node.id].y),
      );
    });
  }

  return positions;
}

export function nodeRadius(citationCount: number | null, isOrigin = false): number {
  const scaled = 8 + Math.sqrt(Math.max(0, citationCount || 0)) * 0.6;
  return Math.max(isOrigin ? 16 : 9, Math.min(isOrigin ? 27 : 22, scaled));
}

export function firstAuthorLabel(paper: LiteraturePaper): string {
  const author = paper.authors[0]?.trim() || "Unknown";
  const surname = author.includes(",")
    ? author.split(",")[0].trim()
    : author.split(/\s+/).at(-1) || author;
  return `${surname} ${paper.year || "—"}`;
}

export function formatCitationCount(value: number | null): string {
  if (value == null) return "未知";
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

export function yearColor(
  year: number | null,
  minYear: number,
  maxYear: number,
): string {
  if (year == null) return "hsl(var(--muted-foreground))";
  const ratio = maxYear === minYear ? 0.65 : (year - minYear) / (maxYear - minYear);
  const lightness = 63 - ratio * 22;
  const saturation = 12 + ratio * 25;
  return `hsl(214 ${saturation}% ${lightness}%)`;
}
