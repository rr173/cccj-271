"""Integer, half-open axis-aligned rectangle geometry."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .errors import PageDescriptionError

Rect = tuple[int, int, int, int]


def as_rect(value: Any, *, what: str = "rect") -> Rect:
    """Validate and return an ``(x0, y0, x1, y1)`` half-open integer rect.

    Booleans are rejected even though ``bool`` is a subclass of ``int``.
    """

    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PageDescriptionError(
            "invalid_rectangle",
            reason=f"{what} must be a four-element [x0,y0,x1,y1] array",
            field=what,
        )
    if any(not isinstance(v, int) or isinstance(v, bool) for v in value):
        raise PageDescriptionError(
            "invalid_coordinate",
            reason=f"{what} coordinates must be integers",
            field=what,
        )

    x0, y0, x1, y1 = (int(v) for v in value)
    if x1 <= x0 or y1 <= y0:
        raise PageDescriptionError(
            "invalid_rectangle",
            reason=f"{what} must have positive area and x0<x1, y0<y1",
            field=what,
        )
    return x0, y0, x1, y1


def intersection(a: Rect, b: Rect) -> Rect | None:
    """Return the positive-area intersection, or ``None``.

    Touching at an edge or corner has zero area and therefore returns ``None``.
    """

    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x0 < x1 and y0 < y1:
        return x0, y0, x1, y1
    return None


def intersects(a: Rect, b: Rect) -> bool:
    return intersection(a, b) is not None


def area(r: Rect) -> int:
    return (r[2] - r[0]) * (r[3] - r[1])


def contains(outer: Rect, inner: Rect) -> bool:
    """Whether ``inner`` is completely contained in ``outer`` (edges allowed)."""

    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def ensure_inside(bounds: Rect, rect: Rect, *, what: str) -> None:
    """Raise if *rect* is not wholly inside *bounds*."""

    violations: list[str] = []
    if rect[0] < bounds[0]:
        violations.append("x0_before_page_left")
    if rect[1] < bounds[1]:
        violations.append("y0_before_page_top")
    if rect[2] > bounds[2]:
        violations.append("x1_after_page_right")
    if rect[3] > bounds[3]:
        violations.append("y1_after_page_bottom")
    if violations:
        raise PageDescriptionError(
            "element_out_of_page",
            reason=f"{what} extends outside the declared page boundary",
            field=what,
            violations=violations,
        )


def union_bounds(rects: Iterable[Rect]) -> Rect | None:
    it = iter(rects)
    try:
        first = next(it)
    except StopIteration:
        return None
    x0, y0, x1, y1 = first
    for r in it:
        x0 = min(x0, r[0])
        y0 = min(y0, r[1])
        x1 = max(x1, r[2])
        y1 = max(y1, r[3])
    return x0, y0, x1, y1


def split_rect(r: Rect) -> list[Rect]:
    """Split into positive-area integer halves/quadrants."""

    x0, y0, x1, y1 = r
    width = x1 - x0
    height = y1 - y0

    if width > 1:
        mx = x0 + width // 2
        xs: Sequence[tuple[int, int]] = ((x0, mx), (mx, x1))
    else:
        xs = ((x0, x1),)

    if height > 1:
        my = y0 + height // 2
        ys: Sequence[tuple[int, int]] = ((y0, my), (my, y1))
    else:
        ys = ((y0, y1),)

    out: list[Rect] = []
    for xa, xb in xs:
        for ya, yb in ys:
            if xa < xb and ya < yb:
                out.append((xa, ya, xb, yb))
    return out


def subtract_rect(fragments: list[Rect], cutter: Rect) -> list[Rect]:
    """Subtract *cutter* from every disjoint rect in *fragments*.

    The returned rectangles remain pairwise disjoint and integer/half-open.
    Boundary-only contact produces no positive-area intersection and therefore
    leaves the fragments unchanged.
    """

    result: list[Rect] = []
    for fragment in fragments:
        cut = intersection(fragment, cutter)
        if cut is None:
            result.append(fragment)
            continue
        if cut == fragment:
            continue

        fx0, fy0, fx1, fy1 = fragment
        cx0, cy0, cx1, cy1 = cut

        if fx0 < cx0:
            result.append((fx0, fy0, cx0, fy1))
        if cx1 < fx1:
            result.append((cx1, fy0, fx1, fy1))
        if fy0 < cy0:
            result.append((cx0, fy0, cx1, cy0))
        if cy1 < fy1:
            result.append((cx0, cy1, cx1, fy1))

    return result
