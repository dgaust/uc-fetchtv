"""Stress: the media browser with thousands of recordings and hundreds of channels, odd titles.

It has to page through them quickly and stay inside the Remote's limits (a
browse item's title and subtitle are at most 255 characters) whatever the
titles are, and every recording has to be reachable.
"""
import asyncio
import logging
import time
import tracemalloc

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import entities as ent
import fetchtv
from _stress import build_recordings, odd_title
from fake_fetch import LOUNGE
from ucapi import StatusCodes
from ucapi.api_definitions import Paging
from ucapi.media_player import BrowseOptions

N = 5000


class OfflineClient(fetchtv.FetchTvClient):
    """A client that never touches the network, for driving the browser directly."""

    def __init__(self):
        super().__init__("code", "pin")
        self.connected = True


async def browse(client, box, media_id, page, limit):
    return await ent.browse_channels(client, box, BrowseOptions(media_id=media_id, paging=Paging(page=page, limit=limit)))


def check_items(items):
    for item in items:
        assert 1 <= len(item.title) <= 255, (item.media_id, len(item.title))
        assert item.subtitle is None or 1 <= len(item.subtitle) <= 255, (item.media_id, len(item.subtitle))


async def browse_all(client, box, media_id, limit):
    """Every item of a folder, page by page."""
    items, page = [], 1
    while True:
        result = await browse(client, box, media_id, page, limit)
        assert not isinstance(result, StatusCodes), (media_id, page, result)
        chunk = result.media.items or []
        check_items(chunk)
        assert len(chunk) <= limit
        items += chunk
        total = result.pagination.count
        if not chunk or len(items) >= total:
            return items, total
        page += 1
        assert page < 10_000, "paging does not end"


async def browser_part():
    client = OfflineClient()
    box = fetchtv.Box(terminal_id=LOUNGE, name="Lounge", online=True, standby=False)
    box.recordings = [r for r in (fetchtv._recording(raw) for raw in build_recordings(N)) if r]
    assert len(box.recordings) == N
    series_ids = {r.series_id for r in box.recordings if r.series_id}
    singles = [r for r in box.recordings if not r.series_id]

    tracemalloc.start()
    t0 = time.perf_counter()
    items, total = await browse_all(client, box, "recordings", 50)
    elapsed = time.perf_counter() - t0
    # Each series is one folder, every other recording stands alone.
    assert total == len(series_ids) + len(singles) == len(items), (total, len(series_ids), len(singles), len(items))
    assert len({i.media_id for i in items}) == len(items), "an entry was listed twice"
    folders = [i for i in items if i.media_id.startswith("series:")]
    assert len(folders) == len(series_ids)

    # Every series folder lists all of its episodes, newest first, and every recording is reachable.
    seen = {i.media_id for i in items if i.media_id.startswith("recording:")}
    per_series_start = time.perf_counter()
    for folder in folders:
        episodes, count = await browse_all(client, box, folder.media_id, 20)
        assert count == len(episodes) and count > 1
        seen |= {e.media_id for e in episodes}
    assert seen == {f"recording:{r.id}" for r in box.recordings}, "a recording cannot be reached from the browser"
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    per_request = elapsed / (total / 50 + 1)
    print(f"  browse: {total} entries, {elapsed:.2f}s for the Recordings folder "
          f"({per_request * 1000:.1f} ms/page), {time.perf_counter() - per_series_start:.2f}s for the series, peak {peak / 1e6:.1f} MB")
    assert per_request < 0.5, f"a page of the Recordings folder takes {per_request:.2f}s"
    assert peak < 60e6, f"browsing used {peak / 1e6:.0f} MB"

    # Newest first: the dated entries come in order.
    newest = max(box.recordings, key=lambda r: (r.start_ms, r.id))
    first = (await browse(client, box, "recordings", 1, 3)).media.items[0]
    assert first.media_id in (f"recording:{newest.id}", f"series:{newest.series_id}"), first

    # The last page and pages past the end.
    for page in (N // 50, 10**6):
        result = await browse(client, box, "recordings", page, 50)
        assert len(result.media.items or []) <= 50 and result.pagination.count == total
    for media_id in ("", "nope", "series:", "series:missing", "recording:1", "recordings/../x", "x" * 100_000):
        result = await browse(client, box, media_id or None, 1, 10)
        assert result in (StatusCodes.NOT_FOUND,) or result.media is not None, media_id

    # Free to air and subscription folders with hundreds of channels, and long names.
    box.fta_channels = [fetchtv.Channel(id=str(9000 + i), epg_id=str(9000 + i), number=i, name=odd_title(i) + " ch")
                        for i in range(400)]
    items, total = await browse_all(client, box, "fta", 25)
    assert total == len(items) == 400 and len({i.media_id for i in items}) == 400


async def main():
    await browser_part()
    print("STRESS_SCALE_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 120))
