"""Cricbuzz source (UNOFFICIAL): match list + scorecards, read from the JSON the public
website embeds in its pages. No API key. This is not a documented API, so it can change
or be blocked at any time and its terms of use apply; keep volume low (1 request/second,
completed scorecards cached on disk).

    from cricbuzz_source import list_matches, get_scorecard
"""
import json
import os
import re
import time

import requests

BASE = 'https://www.cricbuzz.com'
CACHE = '.cache_cricbuzz'
DELAY = 1.0
UA = {'user-agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
      'accept-language': 'en-US,en;q=0.9'}
LIST_PAGES = ('/cricket-match/live-scores', '/cricket-match/live-scores/recent-matches')


def _page_text(path):
    """Fetch a page and rebuild the text of its embedded Next.js data chunks."""
    time.sleep(DELAY)
    r = requests.get(BASE + path, headers=UA, timeout=45)
    r.raise_for_status()
    parts = []
    for c in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', r.text):
        try:
            parts.append(json.loads('"' + c + '"'))
        except ValueError:
            pass
    return ''.join(parts)


def _objects_after(text, key):
    """Yield each JSON object/array that directly follows `"key":` in text."""
    for m in re.finditer(r'"%s":' % re.escape(key), text):
        i = m.end()
        if i >= len(text) or text[i] not in '{[':
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < len(text):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in '{[':
                depth += 1
            elif ch in '}]':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        try:
            yield json.loads(text[i:j + 1])
        except ValueError:
            continue


def list_matches():
    """{matchId: matchInfo} from the live-scores and recent-matches pages."""
    out = {}
    for path in LIST_PAGES:
        for mi in _objects_after(_page_text(path), 'matchInfo'):
            if isinstance(mi, dict) and 'matchId' in mi:
                out[mi['matchId']] = mi
    return out


def _slug(mi):
    s = f"{mi['team1']['teamSName']} vs {mi['team2']['teamSName']} {mi.get('matchDesc', '')} " \
        f"{mi.get('seriesName', '')}"
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def get_scorecard(mi):
    """List of innings dicts for a match (batTeamDetails, bowlTeamDetails, scoreDetails,
    partnershipsData, ...). Completed matches are cached; live ones are always refetched."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{mi['matchId']}.json")
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    text = _page_text(f"/live-cricket-scorecard/{mi['matchId']}/{_slug(mi)}")
    cards = next(_objects_after(text, 'scoreCard'), None)
    if cards and mi.get('state') == 'Complete':
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cards, f)
    return cards
