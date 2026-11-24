"""A small static bounding-volume hierarchy for rectangle queries.

The index never scans every box for a query.  It is built once per page from
the declared cells and then answers positive-area intersection queries.
Exact half-open geometry is applied before an ID is yielded.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterator, Sequence
from dataclasses import dataclass, field

from .geometry import Rect, intersection, union_bounds


@dataclass(slots=True)
class _Node:
    bounds: Rect
    items: list[tuple[Hashable, Rect]] = field(default_factory=list)
    left: "_Node | None" = None
    right: "_Node | None" = None

    @property
    def leaf(self) -> bool:
        return self.left is None and self.right is None


class BoxIndex:
    def __init__(
        self,
        items: Sequence[tuple[Hashable, Rect]],
        *,
        leaf_size: int = 8,
    ) -> None:
        self.leaf_size = max(1, leaf_size)
        self._root = self._build(list(items))

    def _build(self, items: list[tuple[Hashable, Rect]]) -> _Node:
        bounds = union_bounds(r for _, r in items)
        if bounds is None:
            # An empty index is represented by an impossible zero-area box.
            return _Node(bounds=(0, 0, 0, 0), items=[])

        if len(items) <= self.leaf_size:
            return _Node(bounds=bounds, items=items)

        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        axis = 0 if width >= height else 1

        def center_key(item: tuple[Hashable, Rect]) -> int:
            r = item[1]
            return (r[axis] + r[axis + 2]) // 2

        items.sort(key=center_key)
        mid = len(items) // 2
        left = self._build(items[:mid])
        right = self._build(items[mid:])
        node_bounds = union_bounds([left.bounds, right.bounds]) or bounds
        return _Node(bounds=node_bounds, left=left, right=right)

    def query(self, rect: Rect) -> Iterator[Hashable]:
        """Yield IDs whose rect has positive-area intersection with *rect*."""

        root = self._root
        if root is None:
            return
        stack: list[_Node] = [root]
        while stack:
            node = stack.pop()
            if intersection(node.bounds, rect) is None:
                continue
            if node.leaf:
                for item_id, item_rect in node.items:
                    if intersection(item_rect, rect) is not None:
                        yield item_id
            else:
                # Push right first so left is processed first.
                if node.right is not None:
                    stack.append(node.right)
                if node.left is not None:
                    stack.append(node.left)

    def first_overlap(
        self, rect: Rect, *, ignore_id: Hashable | None = None
    ) -> Hashable | None:
        for item_id in self.query(rect):
            if ignore_id is not None and item_id == ignore_id:
                continue
            return item_id
        return None
