import hashlib
import os
import re


class MarkdownChunker:
    """Split Markdown into bounded, heading-aware chunks.

    ``target_chars`` controls normal packing, ``max_chars`` is a hard bound,
    and ``min_chars`` is used to merge short neighboring chunks when possible.
    """

    def __init__(self, min_chars=100, max_chars=1500, target_chars=800):
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
        self.target_chars = min(self.max_chars, max(self.min_chars, int(target_chars)))

    def split_markdown_into_sections(self, content: str, file_path: str):
        raw_doc_title = os.path.splitext(os.path.basename(file_path))[0]
        lines = content.splitlines()
        sections = []
        current_headings = {0: raw_doc_title}
        current_chunk_lines = []
        in_code_block = False
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

        def flush_section():
            if not current_chunk_lines:
                return
            chunk_text = "\n".join(current_chunk_lines).strip()
            if chunk_text:
                sections.append({
                    "heading_path": self._build_path_str(current_headings),
                    "content": chunk_text,
                })

        for line in lines:
            marker = self._fence_marker(line)
            if marker:
                in_code_block = not in_code_block
                current_chunk_lines.append(line)
                continue
            if in_code_block:
                current_chunk_lines.append(line)
                continue

            match = heading_pattern.match(line)
            if not match:
                current_chunk_lines.append(line)
                continue

            flush_section()
            current_chunk_lines.clear()
            level = len(match.group(1))
            heading_text = match.group(2).strip()
            if level == 1:
                current_headings = {1: heading_text}
            else:
                for old_level in [l for l in current_headings if l >= level]:
                    del current_headings[old_level]
                current_headings[level] = heading_text
            current_chunk_lines.append(line)

        flush_section()

        if not sections and content.strip():
            sections.append({
                "heading_path": raw_doc_title,
                "content": content.strip(),
            })

        bounded_sections = []
        for sec in sections:
            text = sec["content"]
            if self._is_heading_only(text):
                # The heading is retained in heading_path/vector_text; a
                # heading with no body must not become an orphan chunk.
                continue
            if len(text) <= self.max_chars:
                bounded_sections.append(sec)
                continue
            sub_chunks = self._split_large_text(text)
            sub_chunks = [
                sub_text for sub_text in sub_chunks
                if not self._is_heading_only(sub_text)
            ]
            for sub_idx, sub_text in enumerate(sub_chunks):
                sub_path = sec["heading_path"]
                if len(sub_chunks) > 1:
                    sub_path = f"{sub_path} (Part {sub_idx + 1})"
                bounded_sections.append({
                    "heading_path": sub_path,
                    "content": sub_text,
                })

        # Pack adjacent short sections toward target_chars without crossing
        # max_chars. Keeping the first heading path is intentional: subsequent
        # heading lines remain in content and are still searchable by FTS.
        packed = []
        for sec in bounded_sections:
            if not packed:
                packed.append(dict(sec))
                continue
            previous = packed[-1]
            combined = f"{previous['content']}\n\n{sec['content']}"
            should_merge = (
                len(previous["content"]) < self.target_chars
                or len(sec["content"]) < self.min_chars
            )
            if should_merge and len(combined) <= self.max_chars:
                previous["content"] = combined
            else:
                packed.append(dict(sec))

        final_chunks = []
        for idx, sec in enumerate(packed):
            final_chunks.append(
                self._make_chunk(file_path, sec["heading_path"], sec["content"], idx)
            )
        return final_chunks

    def _is_heading_only(self, text: str) -> bool:
        """Return true when a section contains headings but no body text."""
        heading_pattern = re.compile(r"^\s{0,3}#{1,6}\s+.+$")
        non_empty = [line.strip() for line in text.splitlines() if line.strip()]
        return bool(non_empty) and all(heading_pattern.match(line) for line in non_empty)

    def _build_path_str(self, headings_dict):
        crumbs = []
        for level in sorted(headings_dict):
            value = str(headings_dict[level]).strip()
            if value and (not crumbs or crumbs[-1] != value):
                crumbs.append(value)
        return " > ".join(crumbs) if crumbs else "知识库文档"

    def _make_chunk(self, file_path, heading_path, content, index):
        vector_text = f"【路径】{heading_path}\n\n{content}"
        chunk_id = hashlib.md5(
            f"{file_path}::{index}::{content[:50]}".encode("utf-8")
        ).hexdigest()
        return {
            "chunk_id": chunk_id,
            "file_path": file_path,
            "heading_path": heading_path,
            "content": content,
            "vector_text": vector_text,
            "char_count": len(content),
        }

    def _fence_marker(self, line):
        stripped = line.lstrip()
        if stripped[:1] in {"-", "*", "+"}:
            stripped = stripped[1:].lstrip()
        if stripped.startswith("```"):
            return "```"
        if stripped.startswith("~~~"):
            return "~~~"
        return None

    def _is_fence_closing(self, line, marker):
        stripped = line.lstrip()
        if stripped[:1] in {"-", "*", "+"}:
            stripped = stripped[1:].lstrip()
        return stripped == marker

    def _fence_has_info(self, line, marker):
        stripped = line.lstrip()
        if stripped[:1] in {"-", "*", "+"}:
            stripped = stripped[1:].lstrip()
        return bool(stripped[len(marker):].strip())

    def _split_blocks_preserving_fences(self, text: str):
        """Split blank-line paragraphs without splitting fenced blocks."""
        blocks = []
        current = []
        fences = []

        def flush():
            if current:
                value = "\n".join(current).strip()
                if value:
                    blocks.append(value)
                current.clear()

        for line in text.splitlines():
            marker = self._fence_marker(line)
            if marker and not fences:
                flush()
                current.append(line)
                fences.append(marker)
                continue
            if marker and fences:
                if self._is_fence_closing(line, fences[-1]):
                    current.append(line)
                    fences.pop()
                    if not fences:
                        flush()
                    continue
                if self._fence_has_info(line, marker):
                    fences.append(marker)
                current.append(line)
                continue
            if fences:
                current.append(line)
                continue
            if line.strip():
                current.append(line)
            else:
                flush()
        flush()
        return blocks

    def _split_large_text(self, text: str):
        """Split paragraphs toward target_chars and never exceed max_chars."""
        paragraphs = self._split_blocks_preserving_fences(text)
        chunks = []
        current = []
        current_len = 0

        def flush():
            nonlocal current, current_len
            if current:
                value = "\n\n".join(current).strip()
                if value:
                    chunks.append(value)
            current = []
            current_len = 0

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) > self.max_chars:
                flush()
                chunks.extend(self._split_oversized_piece(paragraph))
                continue
            separator_len = 2 if current else 0
            if current and (
                current_len >= self.target_chars
                or current_len + separator_len + len(paragraph) > self.max_chars
            ):
                flush()
            current.append(paragraph)
            current_len += (2 if len(current) > 1 else 0) + len(paragraph)
        flush()

        # Rebalance a tail that is too short to stand alone.  A simple
        # concatenation is not enough when the previous chunk is already at
        # max_chars; move a small suffix from it to the tail instead.
        return self._rebalance_short_chunks(chunks)

    def _rebalance_short_chunks(self, chunks):
        if len(chunks) < 2:
            return chunks
        chunks = list(chunks)
        if len(chunks[0]) < self.min_chars:
            chunks = self._grow_short_chunk_from_next(chunks, 0)
        return self._rebalance_short_tail(chunks)

    def _grow_short_chunk_from_next(self, chunks, index):
        if index < 0 or index + 1 >= len(chunks):
            return chunks
        current = chunks[index]
        following = chunks[index + 1]
        if self._contains_fence(current) or self._contains_fence(following):
            return chunks
        combined = f"{current}\n\n{following}"
        if len(combined) <= self.max_chars:
            return chunks[:index] + [combined] + chunks[index + 2:]

        required_from_following = max(1, self.min_chars - len(current) - 2)
        split_at = required_from_following
        # Prefer a nearby line boundary that still satisfies the actual
        # post-strip lengths; a boundary just before the target can leave the
        # heading chunk at 198 characters.
        candidates = [
            pos for pos in range(max(1, split_at), min(len(following), split_at + 240) + 1)
            if following[pos - 1:pos] == "\n"
            and len(following[:pos].rstrip()) >= required_from_following
            and len(following[pos:].lstrip()) >= self.min_chars
        ]
        if candidates:
            split_at = min(candidates, key=lambda pos: abs(pos - split_at))
        moved = following[:split_at].rstrip()
        remainder = following[split_at:].lstrip()
        grown = f"{current}\n\n{moved}" if moved else current
        if len(grown) < self.min_chars or len(remainder) < self.min_chars:
            return chunks
        return chunks[:index] + [grown, remainder] + chunks[index + 2:]

    def _contains_fence(self, text):
        return "```" in text or "~~~" in text

    def _rebalance_short_tail(self, chunks):
        if len(chunks) < 2 or len(chunks[-1]) >= self.min_chars:
            return chunks
        if self._contains_fence(chunks[-2]) or self._contains_fence(chunks[-1]):
            return chunks

        previous = chunks[-2]
        tail = chunks[-1]
        combined = f"{previous}\n\n{tail}"
        if len(combined) <= self.max_chars:
            return chunks[:-2] + [combined]

        required_from_previous = max(1, self.min_chars - len(tail) - 2)
        split_at = len(previous) - required_from_previous
        # Prefer a nearby line boundary so Markdown/code is not cut in the
        # middle of a line.  Fall back to the exact character boundary for a
        # single very long line.
        candidates = [
            pos for pos in range(max(1, split_at - 240), min(len(previous), split_at + 240) + 1)
            if previous[pos - 1:pos] == "\n"
        ]
        if candidates:
            split_at = min(candidates, key=lambda pos: abs(pos - split_at))
        prefix = previous[:split_at].rstrip()
        moved = previous[split_at:].strip()
        new_tail = f"{moved}\n\n{tail}" if moved else tail
        if (
            len(prefix) >= self.min_chars
            and self.min_chars <= len(new_tail) <= self.max_chars
        ):
            return chunks[:-2] + [prefix, new_tail]
        # This is only reachable for unusual malformed input.  Keeping the
        # bounded chunks is safer than dropping content or exceeding max.
        return chunks

    def _split_nested_fenced_body(self, lines, limit, fence):
        units = []
        plain = []
        nested = []

        def flush_plain():
            if plain:
                value = "\n".join(plain).strip()
                if value:
                    units.append(value)
                plain.clear()

        for line in lines:
            marker = self._fence_marker(line)
            if not nested:
                if marker == fence and not self._is_fence_closing(line, fence):
                    flush_plain()
                    nested.append(line)
                else:
                    plain.append(line)
                continue
            nested.append(line)
            if marker == fence and self._is_fence_closing(line, fence):
                units.append("\n".join(nested).strip())
                nested.clear()
        flush_plain()
        if nested:
            units.append("\n".join(nested).strip())
        return self._pack_units(units, limit)

    def _pack_units(self, units, limit):
        expanded = []
        for unit in units:
            if len(unit) <= limit:
                expanded.append(unit)
                continue
            first = unit.splitlines()[0] if unit.splitlines() else unit
            last = unit.splitlines()[-1] if unit.splitlines() else unit
            marker = self._fence_marker(first)
            if marker and self._is_fence_closing(last, marker):
                expanded.extend(self._split_oversized_piece(unit))
            else:
                expanded.extend(self._bounded_line_chunks(unit.splitlines(), limit))

        chunks = []
        current = ""
        for unit in expanded:
            if not unit:
                continue
            candidate = f"{current}\n{unit}" if current else unit
            if current and len(candidate) > limit:
                chunks.append(current.strip())
                current = unit
            else:
                current = candidate
        if current.strip():
            chunks.append(current.strip())
        return chunks

    def _split_oversized_piece(self, text: str):
        """Bound a single oversized paragraph/code block by lines/chars."""
        lines = text.splitlines() or [text]
        fence = self._fence_marker(lines[0])
        if fence and len(lines) >= 2 and self._is_fence_closing(lines[-1], fence):
            opening = lines[0].rstrip()
            closing = lines[-1].strip()
            nested = any(
                self._fence_marker(line) == fence
                and not self._is_fence_closing(line, fence)
                for line in lines[1:-1]
            )
            if nested and fence == "```":
                opening = "`" + opening
                closing = "`" + closing
            room = self.max_chars - len(opening) - len(closing) - 2
            if room > 0:
                if nested:
                    body_chunks = self._split_nested_fenced_body(lines[1:-1], room, fence)
                else:
                    body_chunks = self._bounded_line_chunks(lines[1:-1], room)
                return [
                    f"{opening}\n{body}\n{closing}"
                    for body in body_chunks
                    if body
                ]

        return self._bounded_line_chunks(lines, self.max_chars)

    def _bounded_line_chunks(self, lines, limit):
        """Split lines without exceeding limit, preferring line boundaries."""
        pieces = []
        current = ""
        for line in lines:
            remaining = line
            if remaining == "":
                remaining = "\n"
            while remaining:
                separator_len = 1 if current else 0
                room = limit - len(current) - separator_len
                if room <= 0:
                    pieces.append(current.strip())
                    current = ""
                    continue
                part, remaining = remaining[:room], remaining[room:]
                candidate = f"{current}\n{part}" if current else part
                if len(candidate) >= limit or remaining:
                    pieces.append(candidate.strip())
                    current = ""
                else:
                    current = candidate
        if current.strip():
            pieces.append(current.strip())
        return [piece for piece in pieces if piece]
