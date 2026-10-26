"""Hard caps. wiki.toml may only lower them (see config.WikiConfig.effective_caps)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Caps:
    max_sources: int = 100
    warn_sources: int = 80
    max_pages: int = 500
    warn_pages: int = 400
    max_section_pages: int = 100
    warn_section_pages: int = 80
    max_index_lines: int = 99
    max_page_lines: int = 400
    max_pages_per_ingest: int = 15
    lookup_pages: int = 5
    split_chars: int = 80_000


HARD_CAPS = Caps()
