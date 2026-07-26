export function resolvePdfAssetUrl(value: string, apiBase: string): string {
  if (/^https?:\/\//i.test(value)) return value;

  const normalizedValue = value.startsWith("/") ? value : `/${value}`;
  const normalizedApiBase = apiBase.replace(/\/$/, "");
  if (normalizedValue.startsWith("/assets/")) {
    if (normalizedApiBase.startsWith("/")) return normalizedValue;
    return new URL(normalizedValue, new URL(normalizedApiBase).origin).toString();
  }
  return `${normalizedApiBase}${normalizedValue}`;
}
