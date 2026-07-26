/** FadeUp — 包裹组件，挂 .fade-up + 可选 delay 类，触发滚动揭示。 */

export function FadeUp({
  children,
  delay,
  className = "",
}: {
  children: React.ReactNode;
  delay?: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9;
  className?: string;
}) {
  const delayClass = delay ? `fade-delay-${delay}` : "";
  return <div className={`fade-up ${delayClass} ${className}`}>{children}</div>;
}
