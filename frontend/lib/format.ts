const SQL_KEYWORDS =
  /\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|WITH|AS|AND|OR|NOT|IN|IS|NULL|BETWEEN|LIKE|CASE|WHEN|THEN|ELSE|END|DISTINCT|COUNT|SUM|AVG|MIN|MAX|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TABLE|INDEX|VIEW)\b/gi;
const SQL_FUNCTIONS =
  /\b(COALESCE|NULLIF|CAST|CONVERT|DATE|YEAR|MONTH|DAY|NOW|CURRENT_DATE|CURRENT_TIMESTAMP|ROUND|FLOOR|CEIL|ABS|LENGTH|UPPER|LOWER|TRIM|CONCAT|SUBSTRING|EXTRACT|DATEDIFF|DATEADD|IFNULL|IIF|ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD|OVER|PARTITION BY)\b/gi;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Returns SQL wrapped in span classes (sql-kw/sql-fn/sql-str/sql-num) for
 * syntax highlighting. Input is HTML-escaped first, so this is safe to
 * render with dangerouslySetInnerHTML.
 */
export function highlightSql(sql: string): string {
  const escaped = escapeHtml(sql);
  return escaped
    .replace(SQL_KEYWORDS, (m) => `<span class="sql-kw">${m.toUpperCase()}</span>`)
    .replace(SQL_FUNCTIONS, (m) => `<span class="sql-fn">${m}</span>`)
    .replace(/&#39;([^&]*)&#39;|'([^']*)'/g, (m) => `<span class="sql-str">${m}</span>`)
    .replace(/\b(\d+(\.\d+)?)\b/g, '<span class="sql-num">$1</span>');
}

export function formatTime(ts: number | string): string {
  const d = typeof ts === "number" ? new Date(ts) : new Date(ts);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function uid(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}
