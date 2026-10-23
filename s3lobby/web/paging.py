"""Pages of a long list: which slice of the rows a page shows, read from the request's ?page=.

A lobby with hundreds of accounts or thousands of games would otherwise send them all at once.
The page number from the address is untrusted: anything that is not a whole number counts as the
first page, and a number past the end shows the last page.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import request

# Rows per page in the admins' long lists.
PAGE_SIZE = 50
FIRST_PAGE = 1
# A page number has at most this many digits; longer text is not read as a number at all.
MAX_PAGE_DIGITS = 6


@dataclass(frozen=True)
class Pagination:
    """One page of total rows: its number (from 1) and how many rows a page holds."""

    number: int
    size: int
    total: int

    @property
    def page_count(self) -> int:
        """How many pages the rows fill; at least one, so an empty list still has its page."""
        # Rounded up: 51 rows at 50 a page fill two pages.
        full_pages, rest = divmod(self.total, self.size)
        if rest:
            full_pages += 1
        return max(FIRST_PAGE, full_pages)

    @property
    def offset(self) -> int:
        """How many rows come before this page."""
        return (self.number - 1) * self.size

    @property
    def has_previous(self) -> bool:
        """True after the first page."""
        return self.number > FIRST_PAGE

    @property
    def has_next(self) -> bool:
        """True before the last page."""
        return self.number < self.page_count

    def slice(self, rows: list) -> list:
        """This page's rows out of a list that holds all of them."""
        return rows[self.offset:self.offset + self.size]


def pagination_from_request(total: int, size: int = PAGE_SIZE) -> Pagination:
    """The page the request asks for (?page=N), kept between the first and the last page."""
    text = request.args.get("page", "")
    number = FIRST_PAGE
    if text.isdigit() and len(text) <= MAX_PAGE_DIGITS:
        number = int(text)
    last_page = Pagination(FIRST_PAGE, size, total).page_count
    number = min(max(number, FIRST_PAGE), last_page)
    return Pagination(number, size, total)
