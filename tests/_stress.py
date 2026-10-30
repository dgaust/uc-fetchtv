"""Test data shared by the stress tests."""


def odd_title(i: int) -> str:
    return [
        f"Show {i}",
        f"Café ☕ {i} — Ünïcödé",
        f"日本語のタイトル {i}",
        f"שלום עולם {i}",
        f"Emoji 🎬📺 {i}",
        f"é combining {i}",
        f"  padded {i}  ",
        f"Very long title {i} " + "x" * 400,
        f"Quote \" and ' and <b>{i}</b> & more",
        f"Line\nbreak {i}",
    ][i % 10]


def build_recordings(n: int) -> list[dict]:
    starts = [1_700_000_000_000 + i * 3_600_000 for i in range(n)]
    # A few start times a Remote cannot show: none, huge, negative, in seconds, as text.
    odd_starts = {7: 0, 11: 10**18, 13: -86_400_000, 17: 1_700_000_000, 19: "1700000000000", 23: None}
    out = []
    for i in range(n):
        out.append({
            "id": 1000 + i, "name": odd_title(i), "episodeTitle": "Episode title " * (30 if i % 50 == 0 else 1),
            "seriesNumber": 1 + i % 9, "episodeNumber": i % 30 if i % 4 else None, "channelId": "30411316",
            "seriesLinkId": f"series-{i % 300}" if i % 3 else None, "description": "A synopsis. " * 5,
            "programStartDate": odd_starts.get(i, starts[i]), "programEndDate": starts[i] + 1_800_000,
        })
    return out
