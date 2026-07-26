"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { LiteratureMapEdge, LiteraturePaper } from "@/lib/api";
import {
  createLiteratureLayout,
  firstAuthorLabel,
  nodeRadius,
  yearColor,
  type LiteratureNodePosition,
} from "@/lib/literatureMap";

interface Viewport {
  x: number;
  y: number;
  scale: number;
}

type DragState =
  | { kind: "pan"; pointerId: number; clientX: number; clientY: number }
  | { kind: "node"; pointerId: number; nodeId: string }
  | null;

const INITIAL_VIEWPORT: Viewport = { x: 0, y: 0, scale: 1 };

export function LiteratureGraph({
  nodes,
  edges,
  relation,
  selectedId,
  onSelect,
}: {
  nodes: LiteraturePaper[];
  edges: LiteratureMapEdge[];
  relation: "similarity" | "citation";
  selectedId: string;
  onSelect: (paperId: string) => void;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<DragState>(null);
  const initialPositions = useMemo(
    () => createLiteratureLayout(nodes, edges),
    [nodes, edges],
  );
  const [positions, setPositions] = useState(initialPositions);
  const [viewport, setViewport] = useState<Viewport>(INITIAL_VIEWPORT);

  useEffect(() => {
    setPositions(initialPositions);
    setViewport(INITIAL_VIEWPORT);
  }, [initialPositions]);

  const visibleEdges = useMemo(
    () => edges.filter((edge) => edge.kind === relation),
    [edges, relation],
  );
  const years = nodes
    .map((node) => node.year)
    .filter((year): year is number => typeof year === "number");
  const minYear = years.length ? Math.min(...years) : 2000;
  const maxYear = years.length ? Math.max(...years) : minYear;

  const pointInGraph = (clientX: number, clientY: number): LiteratureNodePosition => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    const x = ((clientX - rect.left) / rect.width) * 1000;
    const y = ((clientY - rect.top) / rect.height) * 700;
    return {
      x: (x - viewport.x) / viewport.scale,
      y: (y - viewport.y) / viewport.scale,
    };
  };

  const zoom = (factor: number) => {
    setViewport((current) => ({
      ...current,
      scale: Math.max(0.55, Math.min(2.5, current.scale * factor)),
    }));
  };

  return (
    <div className="literature-graph-shell">
      <div className="literature-graph-controls" aria-label="图谱缩放控制">
        <button type="button" onClick={() => zoom(1.15)} aria-label="放大图谱">+</button>
        <button type="button" onClick={() => zoom(1 / 1.15)} aria-label="缩小图谱">−</button>
        <button
          type="button"
          onClick={() => {
            setViewport(INITIAL_VIEWPORT);
            setPositions(initialPositions);
          }}
        >
          重置
        </button>
      </div>
      <svg
        ref={svgRef}
        className="literature-graph"
        viewBox="0 0 1000 700"
        role="group"
        aria-label={`${relation === "similarity" ? "相似关系" : "引用关系"}论文图谱`}
        onWheel={(event) => {
          event.preventDefault();
          const rect = svgRef.current?.getBoundingClientRect();
          if (!rect) return;
          const cursorX = ((event.clientX - rect.left) / rect.width) * 1000;
          const cursorY = ((event.clientY - rect.top) / rect.height) * 700;
          const nextScale = Math.max(
            0.55,
            Math.min(2.5, viewport.scale * (event.deltaY < 0 ? 1.1 : 0.9)),
          );
          const ratio = nextScale / viewport.scale;
          setViewport({
            scale: nextScale,
            x: cursorX - (cursorX - viewport.x) * ratio,
            y: cursorY - (cursorY - viewport.y) * ratio,
          });
        }}
        onPointerDown={(event) => {
          if (event.target !== event.currentTarget) return;
          dragRef.current = {
            kind: "pan",
            pointerId: event.pointerId,
            clientX: event.clientX,
            clientY: event.clientY,
          };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          const drag = dragRef.current;
          if (!drag || drag.pointerId !== event.pointerId) return;
          if (drag.kind === "node") {
            const point = pointInGraph(event.clientX, event.clientY);
            setPositions((current) => ({
              ...current,
              [drag.nodeId]: point,
            }));
            return;
          }
          const rect = svgRef.current?.getBoundingClientRect();
          if (!rect) return;
          const dx = ((event.clientX - drag.clientX) / rect.width) * 1000;
          const dy = ((event.clientY - drag.clientY) / rect.height) * 700;
          dragRef.current = { ...drag, clientX: event.clientX, clientY: event.clientY };
          setViewport((current) => ({ ...current, x: current.x + dx, y: current.y + dy }));
        }}
        onPointerUp={(event) => {
          if (dragRef.current?.pointerId === event.pointerId) {
            dragRef.current = null;
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
        }}
        onPointerCancel={() => {
          dragRef.current = null;
        }}
      >
        <defs>
          <marker
            id="literature-citation-arrow"
            viewBox="0 0 10 10"
            refX="8"
            refY="5"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" className="literature-citation-arrow" />
          </marker>
        </defs>
        <g transform={`translate(${viewport.x} ${viewport.y}) scale(${viewport.scale})`}>
          {visibleEdges.map((edge) => {
            const source = positions[edge.source];
            const target = positions[edge.target];
            if (!source || !target) return null;
            return (
              <line
                key={`${edge.kind}:${edge.source}:${edge.target}`}
                x1={source.x}
                y1={source.y}
                x2={target.x}
                y2={target.y}
                className={`literature-edge literature-edge-${edge.kind}`}
                markerEnd={edge.kind === "citation" ? "url(#literature-citation-arrow)" : undefined}
                style={{ opacity: 0.28 + Math.min(0.5, edge.weight * 0.38) }}
              />
            );
          })}
          {nodes.map((node) => {
            const position = positions[node.id];
            if (!position) return null;
            const origin = node.role === "origin";
            const selected = node.id === selectedId;
            const radius = nodeRadius(node.citation_count, origin);
            return (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={`选择论文：${node.title}`}
                aria-pressed={selected}
                className={`literature-node ${selected ? "literature-node-selected" : ""}`}
                transform={`translate(${position.x} ${position.y})`}
                onClick={() => onSelect(node.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(node.id);
                  }
                }}
                onPointerDown={(event) => {
                  event.stopPropagation();
                  dragRef.current = {
                    kind: "node",
                    pointerId: event.pointerId,
                    nodeId: node.id,
                  };
                  svgRef.current?.setPointerCapture(event.pointerId);
                }}
              >
                <circle
                  r={radius}
                  fill={origin
                    ? "hsl(var(--reader-accent))"
                    : yearColor(node.year, minYear, maxYear)}
                />
                <circle className="literature-node-ring" r={radius + 4} />
                <text y={radius + 17} textAnchor="middle">
                  {firstAuthorLabel(node)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
      <div className="literature-graph-legend" aria-hidden="true">
        <span><i className="literature-legend-origin" />核心论文</span>
        <span><i className="literature-legend-year" />颜色越深，年份越近</span>
        <span>节点越大，引用越多</span>
      </div>
    </div>
  );
}
