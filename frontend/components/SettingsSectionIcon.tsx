export type SettingsSectionIconName =
  | "library"
  | "models"
  | "tools"
  | "advanced";

type Props = {
  name: SettingsSectionIconName;
  className?: string;
};

export function SettingsSectionIcon({ name, className = "h-5 w-5" }: Props) {
  const common = {
    width: 24,
    height: 24,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.25,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    className,
    style: { color: "hsl(var(--settings-icon))" },
    "aria-hidden": true,
  };

  if (name === "library") {
    return (
      <svg {...common} data-icon="reference-folder-papers">
        <g transform="rotate(-9 7.5 7)">
          <path d="M4.6 3.2h6.1l2 2v8.2H4.6Z" />
          <path d="M10.7 3.2v2h2M6.4 7h4.1M6.4 9h3.2" />
        </g>
        <g transform="rotate(8 14.5 7.5)">
          <path d="M10.5 2.7h6.3l2 2v8.7h-8.3Z" />
          <path d="M16.8 2.7v2h2M12.3 6.7h4.2M12.3 8.7h3.4" />
        </g>
        <path
          d="M3 9.2h6.1l1.5 1.7H21v8.6A1.5 1.5 0 0 1 19.5 21h-15A1.5 1.5 0 0 1 3 19.5Z"
          fill="hsl(var(--background))"
          stroke="none"
        />
        <path d="M3 9.2h6.1l1.5 1.7H21v8.6A1.5 1.5 0 0 1 19.5 21h-15A1.5 1.5 0 0 1 3 19.5Z" />
        <path d="M16.8 17.6h2.2" />
      </svg>
    );
  }

  if (name === "models") {
    return (
      <svg {...common} data-icon="reference-translation">
        <path d="M2.7 4.8c0-1.7 1.4-3 3-3h7.1c1.7 0 3 1.3 3 3v3.5c0 1.7-1.3 3-3 3H8.6L5.3 14v-2.9a3 3 0 0 1-2.6-3Z" />
        <path d="M7 5.6h4.5M7 8h3" />
        <path d="M15.2 5.1h3.1a3 3 0 0 1 3 3v3.2a3 3 0 0 1-2.5 3v2.4l-2.7-2.3h-1" />
        <path d="M17.3 8.7h1.8M17.3 11h1.2" />
        <circle cx="13.7" cy="17.3" r="4.2" fill="hsl(var(--background))" />
        <path d="M9.5 17.3h8.4M13.7 13.1c1.1 1.2 1.7 2.6 1.7 4.2s-.6 3-1.7 4.2M13.7 13.1c-1.1 1.2-1.7 2.6-1.7 4.2s.6 3 1.7 4.2" />
      </svg>
    );
  }

  if (name === "tools") {
    return (
      <svg {...common} data-icon="reference-document-tools">
        <path d="M3.2 2.5h9.1l3 3V20H3.2Z" />
        <path d="M12.3 2.5v3h3M6.1 8.2h5.8M6.1 11.2h4.7M6.1 14.2h3.5" />
        <path
          d="M19.2 11.3a4.1 4.1 0 0 0-5.1 5.1l-4.6 4.5a1.5 1.5 0 0 0 2.1 2.1l4.6-4.6a4.1 4.1 0 0 0 5.1-5.1L19 15.6l-2.2-2.2Z"
          fill="hsl(var(--background))"
          stroke="none"
        />
        <path d="M19.2 11.3a4.1 4.1 0 0 0-5.1 5.1l-4.6 4.5a1.5 1.5 0 0 0 2.1 2.1l4.6-4.6a4.1 4.1 0 0 0 5.1-5.1L19 15.6l-2.2-2.2Z" />
      </svg>
    );
  }

  return (
    <svg {...common} data-icon="reference-sliders">
      <path d="M2.5 6.1h11.8M17.7 6.1h3.8M2.5 12h4.3M10.2 12h11.3M2.5 17.9h9.2M15.1 17.9h6.4" />
      <circle cx="16" cy="6.1" r="1.7" fill="hsl(var(--background))" />
      <circle cx="8.5" cy="12" r="1.7" fill="hsl(var(--background))" />
      <circle cx="13.4" cy="17.9" r="1.7" fill="hsl(var(--background))" />
    </svg>
  );
}
