"""Convert Claude Code markdown output to Telegram-compatible HTML.

Telegram's HTML parse mode supports a small tag whitelist:
<b> <i> <u> <s> <code> <pre> <a> <blockquote> <tg-spoiler>.

This converter maps common markdown to that subset. Tables get special
handling because Telegram doesn't render them natively AND its <pre>
block wraps instead of scrolling horizontally on mobile (desktop has
horizontal scroll, mobile clients don't). We use a width heuristic:

  * Narrow tables (total row width <= _PRE_MAX_WIDTH) stay as aligned
    monospace <pre> blocks — columns look crisp and fit on a phone.
  * Wider tables flatten to vertical bullet blocks — first column
    bolded as the row label, remaining columns as sub-lines. Readable
    on any screen width at the cost of more vertical space.

Pure stdlib so the Stop hook can call it without extra dependencies.
"""

from __future__ import annotations

import html
import re
import sys

_CODE_PLACEHOLDER = "\x00C{}\x00"
_INLINE_PLACEHOLDER = "\x00I{}\x00"
_TABLE_PLACEHOLDER = "\x00T{}\x00"


def convert(md: str) -> str:
    """Convert markdown to Telegram HTML."""
    code_blocks: list[str] = []
    inline_codes: list[str] = []
    tables: list[str] = []

    def stash_fence(m: re.Match[str]) -> str:
        lang = m.group(1) or ""
        body = html.escape(m.group(2), quote=False)
        cls = f' class="language-{lang}"' if lang else ""
        code_blocks.append(f"<pre><code{cls}>{body}</code></pre>")
        return _CODE_PLACEHOLDER.format(len(code_blocks) - 1)

    def stash_inline(m: re.Match[str]) -> str:
        body = html.escape(m.group(1), quote=False)
        inline_codes.append(f"<code>{body}</code>")
        return _INLINE_PLACEHOLDER.format(len(inline_codes) - 1)

    # 1. Fenced code blocks: protect from everything.
    md = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", stash_fence, md, flags=re.DOTALL)

    # 2. Tables: handle on raw markdown so we can clean cells ourselves.
    md = _stash_tables(md, tables)

    # 3. Inline code in non-table, non-fence text.
    md = re.sub(r"`([^`\n]+)`", stash_inline, md)

    # 4. Escape HTML in remaining text.
    md = html.escape(md, quote=False)

    # 5. Markdown → HTML tags.
    md = re.sub(r"^#{1,6}[ \t]+(.+?)[ \t]*#*$", r"<b>\1</b>", md, flags=re.MULTILINE)
    md = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", md)
    md = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", md)
    md = re.sub(r"(?<![\w_])_([^_\n]+?)_(?![\w_])", r"<i>\1</i>", md)
    md = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", md)
    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', md)
    md = re.sub(
        r"^>[ \t]*(.+)$", r"<blockquote>\1</blockquote>", md, flags=re.MULTILINE
    )
    md = re.sub(r"^([ \t]*)[-*+][ \t]+(.+)$", r"\1• \2", md, flags=re.MULTILINE)
    md = re.sub(r"^-{3,}$", "·····", md, flags=re.MULTILINE)

    # 6. Restore placeholders.
    for i, block in enumerate(tables):
        md = md.replace(_TABLE_PLACEHOLDER.format(i), block)
    for i, block in enumerate(inline_codes):
        md = md.replace(_INLINE_PLACEHOLDER.format(i), block)
    for i, block in enumerate(code_blocks):
        md = md.replace(_CODE_PLACEHOLDER.format(i), block)
    return md


_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")

# Max total monospace width that fits without wrapping on a typical
# mobile Telegram client at default font size. Above this we switch
# from aligned <pre> tables to vertical bullet blocks.
_PRE_MAX_WIDTH = 34


def _stash_tables(text: str, sink: list[str]) -> str:
    """Detect GFM tables and replace each with a placeholder.

    Rendered tables are aligned <pre> blocks so columns line up on mobile.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (
            _ROW_RE.match(lines[i])
            and i + 1 < len(lines)
            and _SEP_RE.match(lines[i + 1])
        ):
            header = _split_row(lines[i])
            i += 2
            rows: list[list[str]] = []
            while (
                i < len(lines)
                and _ROW_RE.match(lines[i])
                and not _SEP_RE.match(lines[i])
            ):
                rows.append(_split_row(lines[i]))
                i += 1
            sink.append(_render_table(header, rows))
            out.append(_TABLE_PLACEHOLDER.format(len(sink) - 1))
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _split_row(line: str) -> list[str]:
    return [_clean_cell(c) for c in line.strip().strip("|").split("|")]


def _clean_cell(cell: str) -> str:
    """Strip markdown decorations; don't escape yet (done after padding)."""
    cell = cell.strip()
    cell = re.sub(r"`([^`]+)`", r"\1", cell)
    cell = re.sub(r"\*\*([^*]+)\*\*", r"\1", cell)
    cell = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", cell)
    return cell


def _render_table(header: list[str], rows: list[list[str]]) -> str:
    """Render a table. Narrow → aligned <pre>; wide → vertical bullets."""
    if not header:
        return ""
    n = len(header)
    normalised = [header] + [r + [""] * (n - len(r)) for r in rows]
    widths = [max(len(row[col]) for row in normalised) for col in range(n)]
    total_width = sum(widths) + 2 * (n - 1)  # 2-space gutter between columns
    if total_width <= _PRE_MAX_WIDTH:
        return _render_table_monospace(header, normalised[1:], widths, n)
    return _render_table_vertical(header, normalised[1:], n)


def _render_table_monospace(
    header: list[str], rows: list[list[str]], widths: list[int], n: int
) -> str:
    """Aligned <pre> block — used when the table is narrow enough for mobile."""

    def fmt(cells: list[str]) -> str:
        padded = [cells[c].ljust(widths[c]) for c in range(n)]
        return "  ".join(padded).rstrip()

    sep = "  ".join("─" * w for w in widths)
    plain = [fmt(header), sep] + [fmt(r) for r in rows]
    body = "\n".join(html.escape(line, quote=False) for line in plain)
    return f"<pre>{body}</pre>"


def _render_table_vertical(header: list[str], rows: list[list[str]], n: int) -> str:
    """Vertical bullet blocks — used when the table would wrap on mobile.

    For 2-column tables the layout is tight:
        • <b>cell1</b>
          → cell2
    For 3+ columns we label every non-first cell with its header:
        • <b>cell1</b>
          <b>col2:</b> cell2
          <b>col3:</b> cell3
    """

    def esc(s: str) -> str:
        return html.escape(s, quote=False)

    blocks: list[str] = []
    for row in rows:
        first = esc(row[0]) if row else ""
        if n == 1:
            blocks.append(f"• <b>{first}</b>")
            continue
        if n == 2:
            second = esc(row[1]) if len(row) > 1 else ""
            block = f"• <b>{first}</b>"
            if second:
                block += f"\n  → {second}"
            blocks.append(block)
            continue
        lines = [f"• <b>{first}</b>"]
        for i in range(1, n):
            cell = esc(row[i]) if i < len(row) else ""
            lines.append(f"  <b>{esc(header[i])}:</b> {cell}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main() -> None:
    """CLI: read markdown from stdin, write HTML to stdout."""
    sys.stdout.write(convert(sys.stdin.read()))


if __name__ == "__main__":
    main()
