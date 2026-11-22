"""MI PLAYER WATCH: call out every active watchlist player who played in a date window.

Flow: BCCI results lists (international + domestic filters in bcci_filters.json)
      -> scorecard per completed match -> match watchlist IDs against both XIs
      -> one block per player per match, in the MI HPP RAW message style.

Needs env var EDAK (the scorecard key your browser sends to the BCCI site).
Put it in .env as EDAK=... (loaded automatically if python-dotenv is installed).

Usage:
  python mi_player_watch.py --from 2026-11-13 --to 2026-11-15
Options: --include-confirm (also track 'confirm' players), --max-scorecards N
"""
import argparse
import csv
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

from cricbuzz_source import get_scorecard as cb_scorecard
from cricbuzz_source import list_matches as cb_list_matches

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36')
H_STATS = {'accept': 'application/json', 'origin': 'https://www.bcci.tv',
           'referer': 'https://www.bcci.tv/', 'user-agent': UA}
STATS_URL = 'https://stats.bcci.tv/match/{kind}/'
SC_URL = 'https://epr.ellipsedata.com/redirect/1/match/{gid}/scorecard'
CACHE = '.cache_scorecards'
WATCH_CSV = 'mi_watchlist.csv'
FILTERS = 'bcci_filters.json'
DELAY = 0.4  # seconds between requests


def toks(s):
    return re.sub(r'[^a-z ]', '', (s or '').lower()).split()


# ---------------------------------------------------------------- data access
def fetch_matches(kind, comps, teams, start, end=None):
    out, page = [], 1
    while True:
        params = ([('comp_gid', c) for c in comps] + [('team_gid', t) for t in teams]
                  + [('min_start_date', start.isoformat()), ('page', page), ('size', 100)])
        r = requests.get(STATS_URL.format(kind=kind), params=params,
                         headers=H_STATS, timeout=45)
        r.raise_for_status()
        j = r.json()
        out += j.get('match', [])
        nxt = (j.get('page') or {}).get('next_page')
        if not nxt:
            break
        page = nxt
        time.sleep(DELAY)
    return [m for m in out if m['start_date'] >= start.isoformat()
            and (end is None or m['start_date'] <= end.isoformat())]


def live_matches(filters, today):
    """Multi-day matches that span `today` and are not finished (Tests, Ranji...).

    Taken from the fixtures list by date, not by status name, because the status
    label for 'in progress' has not been seen yet (no live match was available)."""
    found = {}
    for kind_name in ('international', 'domestic'):
        f = filters[kind_name]
        for m in fetch_matches('fixtures', f['comp_gids'], f.get('team_gids', []),
                               today - timedelta(days=6), None):
            if (m['start_date'] <= today.isoformat() <= m['end_date']
                    and int(m.get('scheduled_days') or 1) > 1
                    and m.get('match_status') != 'complete'):
                m['_kind'] = kind_name
                found[m['gid']] = m
    return list(found.values())


def get_scorecard(gid, edak):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, gid + '.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    time.sleep(DELAY)
    r = requests.get(SC_URL.format(gid=gid),
                     params={'advanced': 'false', 'partnerships': 'false'},
                     headers={'accept': '*/*', 'content-type': 'application/json',
                              'edak': edak, 'origin': 'https://www.bcci.tv',
                              'referer': 'https://www.bcci.tv/', 'user-agent': UA},
                     timeout=45)
    if r.status_code != 200:
        return None
    sc = r.json()
    if sc.get('match_status') == 'complete':  # completed scorecards never change
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(sc, f)
    return sc


# ------------------------------------------------------------------ watchlist
def load_watch(include_confirm):
    """Indian players are tracked via the BCCI feed (by ID); overseas players via Cricbuzz."""
    ok = {'yes', 'confirm'} if include_confirm else {'yes'}
    by_id, unresolved, overseas = {}, [], []
    with open(WATCH_CSV, newline='', encoding='utf-8') as f:
        rows = [r for r in csv.DictReader(f) if r['active'] in ok]
    order = [r['name'] for r in rows]
    for r in rows:
        if r['group'] == 'overseas':
            overseas.append(r)
        elif r['bcci_player_id']:
            by_id[int(r['bcci_player_id'])] = r
        else:
            unresolved.append(r)
    return by_id, unresolved, order, overseas


# ------------------------------------------------------------ fact-based notes
ORD = ['', '1st', '2nd', '3rd', '4th', '5th', '6th', '7th', '8th', '9th', '10th']


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tidy_result(s):
    s = re.sub(r'\bwin by\b', 'won by', s or '', flags=re.I)
    s = re.sub(r'\bwkts\b', 'wickets', s, flags=re.I)
    return re.sub(r'\bwkt\b', 'wicket', s, flags=re.I)


def build_notes(facts, limit=3):
    """Rule-based lines from scorecard facts only. Nothing is inferred beyond the numbers."""
    cands = []  # (priority, text)
    team = facts.get('team', '')
    for b in facts['bat']:
        runs, balls = b['runs'], b['balls']
        if runs >= 100:
            cands.append((1, 'Century.'))
        st = b.get('stand')
        if st and st['runs'] >= 40:
            if st['unbeaten']:
                cands.append((2, f"Unbeaten {st['runs']}-run partnership with {st['partner']}."))
            else:
                w = ORD[st['wicket']] if 0 < st['wicket'] < 11 else ''
                cands.append((2, f"{st['runs']}-run {w + '-wicket ' if w else ''}stand with "
                                 f"{st['partner']}."))
        en, tt = b.get('entry'), b.get('team_total')
        if en and en[1] >= 3 and runs >= 25:
            line = f"Came in at {en[0]}/{en[1]}"
            if tt:
                line += f"; {team} {tt[0]}/{tt[1]}" if tt[1] < 10 else f"; {team} {tt[0]} all out"
            cands.append((3, line + '.'))
        if b.get('target') and runs >= 20:
            cands.append((4, f"Helped chase {b['target']}." if facts.get('won')
                          else f"In a chase of {b['target']}."))
        if balls >= 15 and runs * 100 / balls >= 200:
            cands.append((5, f"Strike rate {runs * 100 / balls:.0f}."))
    for w in facts['bowl']:
        if w['wkts'] >= 5:
            cands.append((1, 'Five-wicket haul.'))
        elif w['wkts'] >= 3:
            cands.append((2, f"{w['wkts']}-wicket haul."))
        if w['overs'] >= 2 and w.get('rate') and w['econ'] <= 0.75 * w['rate']:
            cands.append((4, f"Tight spell: economy {w['econ']:.2f} against an innings rate "
                             f"of {w['rate']:.2f}."))
    out, seen = [], set()
    for _, t in sorted(cands, key=lambda c: c[0]):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def bcci_facts(sc, pid, team_name, won):
    f = {'team': team_name, 'won': won, 'bat': [], 'bowl': []}
    sched = num(sc.get('scheduled_overs')) or 0
    inns = sc.get('innings', [])
    for idx, inn in enumerate(inns):
        score = inn.get('score') or {}
        for b in inn.get('batting', []):
            if b['player_id'] != pid or b.get('batted') != 'yes':
                continue
            pos = int(num(b.get('batting_position')) or 0)
            entry = None
            if pos >= 3:
                for fw in inn.get('fow') or []:
                    if int(num(fw.get('fow_number')) or 0) == pos - 2 and num(fw.get('fow_runs')) is not None:
                        entry = (int(num(fw['fow_runs'])), pos - 2)
            stand = None
            me = (b.get('player_known_as') or '').lower()
            for p in inn.get('partnerships') or []:
                p1, p2 = p.get('p1') or {}, p.get('p2') or {}
                if (p1.get('known_as') or '').lower() == me:
                    other = p2
                elif (p2.get('known_as') or '').lower() == me:
                    other = p1
                else:
                    continue
                r = int(num(p.get('runs')) or 0)
                if not stand or r > stand['runs']:
                    wk = int(num(p.get('wicket')) or 0)
                    stand = {'runs': r, 'wicket': wk, 'partner': other.get('known_as') or other.get('name') or '',
                             'unbeaten': not p.get('out_batter')}
            target = None
            if sched > 0 and idx == 1 and inns[0].get('score'):
                target = int(num(inns[0]['score'].get('runs')) or 0) + 1
            f['bat'].append({'runs': int(num(b.get('runs')) or 0), 'balls': int(num(b.get('balls_faced')) or 0),
                             'entry': entry, 'stand': stand if stand and stand['partner'] else None,
                             'team_total': (int(score.get('runs') or 0), int(score.get('wickets') or 0)),
                             'target': target})
        for w in inn.get('bowling', []):
            if w['bowler_id'] == pid:
                f['bowl'].append({'wkts': int(num(w.get('wickets')) or 0), 'overs': num(w.get('overs')) or 0,
                                  'econ': num(w.get('economy_rate')) or 0,
                                  'rate': num(score.get('run_rate'))})
    return f


# ------------------------------------------------------------------ extraction
def appearance(sc, m, pid, row, xi):
    p, team = xi[pid]
    other = sc['team2'] if team is sc['team1'] else sc['team1']
    live = sc.get('match_status') != 'complete' and bool(sc.get('innings'))
    app = {'name': row['name'], 'pid': pid, 'team': team['name'], 'opp': other['name'],
           'status': day_label(sc) if live else '',
           'state': state_line(sc) if live else '',
           'date': m['start_date'],
           'teams': f"{m['team1_name'].upper()} v {m['team2_name'].upper()}",
           # international: "4th ODI"; domestic: "Syed Mushtaq Ali Trophy Elite" (+ round if any)
           'event': (m.get('title') or m['comp_name']) if m.get('_kind') == 'international'
           else ', '.join(x for x in (m['comp_name'], m.get('title')) if x),
           'result': (m.get('result_string') or '').strip(),
           'bat': [], 'bowl': [], 'victims': []}
    for inn in sc.get('innings', []):
        for b in inn.get('batting', []):
            if b['player_id'] == pid and b.get('batted') == 'yes':
                app['bat'].append(b)
        for w in inn.get('bowling', []):
            if w['bowler_id'] == pid:
                app['bowl'].append(w)
                # "b X", "c Y b X", "st Y b X" and "lbw X" (no "b" in lbw strings)
                tag = re.compile(r'(^|\s)(b|lbw) ' + re.escape(w['bowler_name']) + r'$')
                # victims come from the innings this spell was bowled in, so a batter
                # dismissed in both innings is listed twice
                for b in inn.get('batting', []):
                    if tag.search((b.get('dismissal_str') or '').strip()):
                        app['victims'].append(f"{b['player_known_as']} ({b.get('runs')})")
    try:
        res = app['result'].lower()
        won = res.startswith(team['name'].lower())
        app['notes'] = build_notes(bcci_facts(sc, pid, team['name'], won))
    except Exception as e:
        print(f"  notes failed for {row['name']}: {e}")
        app['notes'] = []
    return app


def fmt_bat(b):
    star = '' if b.get('is_out') else '*'
    bits = []
    if b.get('fours'):
        bits.append(f"{b['fours']} four{'s' if b['fours'] != 1 else ''}")
    if b.get('sixes'):
        bits.append(f"{b['sixes']} six{'es' if b['sixes'] != 1 else ''}")
    tail = (' | ' + ', '.join(bits)) if bits else ''
    return f"{b['runs']}{star} ({b['balls_faced']}){tail}"


def fmt_bowl(w):
    return f"{w['wickets']}/{w['runs']} ({w['overs']}) | Economy {w['economy_rate']}"


def stumps_count(sc):
    """Days completed. The feed repeats each Stumps note when play resumes, so count
    distinct close-of-play scores rather than notes."""
    seen = set()
    for i, inn in enumerate(sc.get('innings', [])):
        for n in inn.get('notes', []):
            t = n.get('text') or ''
            if t.lower().startswith('stumps'):
                mm = re.search(r'(\d+/\d+) in ([\d.]+) overs', t)
                seen.add((i, mm.groups()) if mm else t)
    return len(seen)


def day_label(sc):
    """'Day 2 Stumps' if the latest innings' last notes include Stumps, else in progress."""
    s = stumps_count(sc)
    inns = sc.get('innings', [])
    at_stumps = False
    if inns:
        cur = inns[-1]
        for n in reversed(cur.get('notes') or []):
            t = n.get('text') or ''
            if t.lower().startswith('stumps'):
                # At stumps if the innings still stands where the latest Stumps note left it.
                mm = re.search(r'(\d+)/(\d+) in ([\d.]+) overs', t)
                sc_ = cur['score']
                at_stumps = bool(mm) and (int(mm.group(1)), int(mm.group(2))) == (
                    sc_['runs'], sc_['wickets']) and abs(float(mm.group(3)) - float(cur['overs'])) < 0.05
                break
    return f"Day {s} Stumps" if at_stumps else f"Day {s + 1} (in progress)"


def state_line(sc):
    """e.g. 'Vidarbha 217/3, trailing by 3 runs.' for the innings in progress."""
    inns = sc.get('innings', [])
    if not inns:
        return ''
    totals = {}
    for inn in inns:
        totals[inn['batting_team_name']] = (totals.get(inn['batting_team_name'], 0)
                                            + inn['score']['runs'])
    cur = inns[-1]
    team, opp, s = cur['batting_team_name'], cur['bowling_team_name'], cur['score']
    txt = f"{team} {s['runs']}/{s['wickets']}" + ('d' if cur.get('closure_name') == 'declared' else '')
    if opp in totals:
        diff = totals[team] - totals[opp]
        if diff > 0:
            txt += f", leading by {diff} runs"
        elif diff < 0:
            txt += f", trailing by {-diff} runs"
        else:
            txt += ', scores level'
    return txt + '.'


def block(a):
    head = f"{a['name'].upper()} | {a['teams']} - {a['event']}"
    if a.get('status'):
        head += f" | {a['status']}"
    lines = [head]
    both = a['bat'] and a['bowl']
    for b in a['bat']:
        lines.append(('Bat: ' if both else '') + fmt_bat(b))
    for w in a['bowl']:
        lines.append(('Bowl: ' if both else '') + fmt_bowl(w))
    if not a['bat'] and not a['bowl']:
        lines.append('In the XI; did not bat or bowl.')
    if a['victims']:
        lines.append('Dismissed ' + ', '.join(a['victims'][:6]) + '.')
    lines += a.get('notes') or []
    if a.get('state'):
        lines.append(a['state'])
    elif a['result']:
        lines.append(tidy_result(a['result']).rstrip('.') + '.')
    return '\n'.join(lines)


# ---------------------------------------------------- Cricbuzz (overseas players)
IST = timezone(timedelta(hours=5, minutes=30))
# Skipped: women's cricket, and Indian domestic/age-group events (the BCCI feed covers those).
SKIP_SERIES = re.compile(r"women|wbbl|wpl|syed mushtaq|ranji|vijay hazare|duleep|irani|"
                         r"vinoo mankad|deodhar|u19|under|emerging", re.I)


def ist_date(ms):
    return datetime.fromtimestamp(int(ms) / 1000, IST).date()


def cb_matches_in_window(start, end):
    out = []
    for mi in cb_list_matches().values():
        if mi.get('state') in ('Preview', 'Upcoming', 'Toss'):
            continue
        if SKIP_SERIES.search(mi.get('seriesName', '')):
            continue
        if ist_date(mi['endDate']) < start or ist_date(mi['startDate']) > end:
            continue
        out.append(mi)
    return sorted(out, key=lambda m: int(m['startDate']))


def cb_name_match(row_name, cb_name):
    """Same surname, and first names equal or one a prefix of the other (Mitch/Mitchell)."""
    q, c = toks(row_name), toks(cb_name)
    if not q or not c or q[-1] != c[-1]:
        return False
    a, b = q[0], c[0]
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def cb_status(mi):
    """('Day 4 Stumps', 'Afghanistan need 275 runs.') from Cricbuzz's status text."""
    if mi.get('state') == 'Complete':
        return '', ''
    st = (mi.get('status') or '').strip()
    m = re.match(r'Day (\d+):\s*([^-]+?)(?:\s+-\s+(.*))?$', st)
    if m:
        day, phase, tail = m.group(1), m.group(2).strip(), (m.group(3) or '').strip()
        label = f"Day {day} Stumps" if phase.lower() == 'stumps' else f"Day {day} - {phase}"
        return label, (tail.rstrip('.') + '.') if tail else ''
    return 'In progress', st


def cb_facts(mi, cards, pid):
    f = {'team': '', 'won': False, 'bat': [], 'bowl': []}
    limited = (mi.get('matchFormat') or '').upper() in ('ODI', 'T20')
    res = (mi.get('status') or '').lower()
    for idx, inn in enumerate(cards):
        sd = inn.get('scoreDetails') or {}
        total = (int(sd.get('runs') or 0), int(sd.get('wickets') or 0))
        bats = list((inn['batTeamDetails'].get('batsmenData') or {}).values())
        for pos, b in enumerate(bats, 1):
            if b['batId'] != pid or not (b['balls'] or b['runs'] or b.get('outDesc')):
                continue
            tname = inn['batTeamDetails'].get('batTeamName', '')
            f['team'] = tname
            f['won'] = res.startswith(tname.lower()) and 'won' in res
            entry = None
            if pos >= 3:
                w = (inn.get('wicketsData') or {}).get(f'wkt_{pos - 2}')
                if w and num(w.get('wktRuns')) is not None:
                    entry = (int(w['wktRuns']), pos - 2)
            stand = None
            for key, p in (inn.get('partnershipsData') or {}).items():
                if pid == p.get('bat1Id'):
                    other = p.get('bat2Name')
                elif pid == p.get('bat2Id'):
                    other = p.get('bat1Name')
                else:
                    continue
                r = int(p.get('totalRuns') or 0)
                if not stand or r > stand['runs']:
                    n = int(key.split('_')[1])
                    stand = {'runs': r, 'wicket': n, 'partner': other or '',
                             'unbeaten': n > total[1]}
            target = None
            if limited and idx == 1:
                target = int((cards[0].get('scoreDetails') or {}).get('runs') or 0) + 1
            f['bat'].append({'runs': int(b['runs']), 'balls': int(b['balls']), 'entry': entry,
                             'stand': stand if stand and stand['partner'] else None,
                             'team_total': total, 'target': target})
        for w in (inn['bowlTeamDetails'].get('bowlersData') or {}).values():
            if w['bowlerId'] == pid:
                f['bowl'].append({'wkts': int(w['wickets']), 'overs': num(w['overs']) or 0,
                                  'econ': num(w['economy']) or 0, 'rate': num(sd.get('runRate'))})
    return f


def cb_appearances(mi, cards, overseas, learned):
    apps = []
    series = mi.get('seriesName', '')
    desc = mi.get('matchDesc', '')
    # internationals: "1st Test" / "5th ODI"; leagues: "Abu Dhabi T10 2026, 13th Match"
    event = desc if re.search(r'\b(test|odi|t20i)\b', desc, re.I) else f"{series}, {desc}"
    label, state = cb_status(mi)
    for row in overseas:
        cid = row.get('cricbuzz_id')
        st = {'pid': int(cid) if cid else None}

        def me(pid_, name_, row=row, st=st):
            if st['pid']:
                return pid_ == st['pid']
            if cb_name_match(row['name'], name_):
                st['pid'] = pid_
                return True
            return False

        bat, bowl = [], []
        for inn in cards:
            for b in inn['batTeamDetails']['batsmenData'].values():
                if me(b['batId'], b['batName']) and (b['balls'] or b['runs'] or b.get('outDesc')):
                    out_txt = (b.get('outDesc') or '').strip().lower()
                    bat.append({'runs': b['runs'], 'balls_faced': b['balls'],
                                'fours': b['fours'], 'sixes': b['sixes'],
                                'is_out': 0 if out_txt in ('', 'batting', 'not out') else 1})
            for w in (inn['bowlTeamDetails'].get('bowlersData') or {}).values():
                if me(w['bowlerId'], w['bowlName']):
                    bowl.append({'wickets': w['wickets'], 'runs': w['runs'],
                                 'overs': f"{float(w['overs']):.1f}",
                                 'economy_rate': f"{float(w['economy']):.2f}"})
        if not (bat or bowl):
            continue
        pid = st['pid']
        victims = []
        for inn in cards:
            for b in inn['batTeamDetails']['batsmenData'].values():
                d = (b.get('outDesc') or '').lower()
                v = f"{b['batName']} ({b['runs']})"
                if (b.get('bowlerId') == pid and 'run out' not in d and 'retired' not in d
                        and v not in victims):
                    victims.append(v)
        try:
            notes = build_notes(cb_facts(mi, cards, pid))
        except Exception as e:
            print(f"  notes failed for {row['name']}: {e}")
            notes = []
        if not cid:
            learned[row['name']] = pid
            row['cricbuzz_id'] = str(pid)
        apps.append({
            'name': row['name'], 'date': ist_date(mi['startDate']).isoformat(),
            'teams': f"{mi['team1']['teamName'].upper()} v {mi['team2']['teamName'].upper()}",
            'event': event, 'status': label, 'state': state,
            'result': (mi.get('status') or '').strip() if mi.get('state') == 'Complete' else '',
            'bat': bat, 'bowl': bowl, 'victims': victims, 'notes': notes})
    return apps


def save_cricbuzz_ids(learned):
    with open(WATCH_CSV, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fields, rows = list(reader.fieldnames), list(reader)
    if 'cricbuzz_id' not in fields:
        fields.insert(fields.index('cricsheet_id') + 1 if 'cricsheet_id' in fields
                      else len(fields), 'cricbuzz_id')
    for r in rows:
        r.setdefault('cricbuzz_id', '')
        if r['name'] in learned and not r['cricbuzz_id']:
            r['cricbuzz_id'] = str(learned[r['name']])
    with open(WATCH_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print('saved Cricbuzz IDs to mi_watchlist.csv for:', ', '.join(
        f'{n} ({i})' for n, i in learned.items()))


def span(start, end):
    if start == end:
        return f"{end.day} {end:%b}".upper()
    if start.month == end.month:
        return f"{start.day}-{end.day} {end:%b}".upper()
    return f"{start.day} {start:%b}-{end.day} {end:%b}".upper()


# ------------------------------------------------------------------------ main
def generate(start, end, edak, include_confirm=False, no_live=False, max_scorecards=150,
             no_cricbuzz=False, max_cricbuzz=60, log=print):
    """Build the PLAYER WATCH message. Returns (text, unconfirmed_name_matches)."""
    a = argparse.Namespace(include_confirm=include_confirm, no_live=no_live,
                           max_scorecards=max_scorecards, no_cricbuzz=no_cricbuzz,
                           max_cricbuzz=max_cricbuzz)
    print = log  # noqa: A001  (route progress lines to the caller)
    filters = json.load(open(FILTERS, encoding='utf-8'))
    by_id, unresolved, order, overseas = load_watch(a.include_confirm)
    print(f'watchlist: {len(by_id)} Indian with IDs, {len(unresolved)} Indian without IDs, '
          f'{len(overseas)} overseas')

    matches = {}
    for kind_name in ('international', 'domestic'):
        f = filters[kind_name]
        got = fetch_matches('results', f['comp_gids'], f.get('team_gids', []), start, end)
        done = [m for m in got if m.get('match_status') == 'complete']
        print(f'{kind_name}: {len(got)} matches in window, {len(done)} complete')
        for m in done:
            m['_kind'] = kind_name
            matches[m['gid']] = m
    if not a.no_live:
        live = live_matches(filters, end)
        print(f'multi-day matches in progress on {end}: {len(live)}')
        for m in live:
            matches.setdefault(m['gid'], m)
    todo = sorted(matches.values(), key=lambda m: (m['start_date'], m['gid']))
    if len(todo) > a.max_scorecards:
        print(f'capping at {a.max_scorecards} of {len(todo)} scorecards (use --max-scorecards)')
        todo = todo[:a.max_scorecards]

    apps, candidates, failed = [], {}, 0
    for i, m in enumerate(todo, 1):
        sc = get_scorecard(m['gid'], edak)
        if not sc:
            failed += 1
            continue
        xi = {p['player_id']: (p, t) for t in (sc['team1'], sc['team2'])
              for p in t.get('players', [])}
        for pid, row in by_id.items():
            if pid in xi:
                apps.append(appearance(sc, m, pid, row, xi))
        for row in unresolved:  # name-only matches are NOT trusted; list for review
            q = toks(row['name'])
            for pid, (p, t) in xi.items():
                if toks(p.get('player_known_as')) == q or toks(p.get('player_name')) == q:
                    candidates[(row['name'], pid)] = (
                        f"{row['name']} -> id {pid} '{p['player_known_as']}' "
                        f"({t['name']}, {m['match_short_name']})")
        if i % 20 == 0:
            print(f'  ...{i}/{len(todo)} scorecards')
    print(f'scorecards read: {len(todo) - failed}, failed: {failed}, '
          f'watchlist appearances: {len(apps)}')

    # ---- overseas players via Cricbuzz
    learned = {}
    if overseas and not a.no_cricbuzz:
        cands = cb_matches_in_window(start, end)
        if len(cands) > a.max_cricbuzz:
            print(f'cricbuzz: capping at the {a.max_cricbuzz} most recent of {len(cands)} matches')
            cands = cands[-a.max_cricbuzz:]  # list is oldest-first; keep the newest
        print(f'cricbuzz: scanning {len(cands)} matches for {len(overseas)} overseas players')
        n_cb = 0
        for mi in cands:
            try:
                cards = cb_scorecard(mi)
            except Exception as e:  # keep going; one bad page should not stop the run
                print(f"  cricbuzz {mi['matchId']} failed: {e}")
                continue
            if not cards:
                continue
            ts = cards[-1].get('timeScore')
            if mi.get('state') == 'Complete' and ts and ist_date(ts) < start:
                continue  # finished before the window
            got = cb_appearances(mi, cards, overseas, learned)
            apps += got
            n_cb += len(got)
        print(f'cricbuzz appearances: {n_cb}')
        if learned:
            save_cricbuzz_ids(learned)

    # ---- message
    out = [f"MI HPP RAW | PLAYER WATCH - {span(start, end)}",
           f"Window: {span(start, end)} | IST", '']
    seen = set()
    for name in order:
        mine = sorted([x for x in apps if x['name'] == name], key=lambda x: x['date'])
        if mine:
            seen.add(name)
        for x in mine:
            out += [block(x), '']
    has_id = {r['name'] for r in csv.DictReader(open(WATCH_CSV, newline='', encoding='utf-8'))
              if r['bcci_player_id'] or r.get('cricbuzz_id')}
    skipped = {r['name'] for r in overseas} if a.no_cricbuzz else set()
    quiet = [n for n in order if n not in seen and n in has_id and n not in skipped]
    if skipped:
        out.append('Overseas players not checked (Cricbuzz off).')
    if quiet:
        out.append('No match in window: ' + ', '.join(quiet))
    unseen = [r['name'] for r in overseas if r['name'] not in seen and r['name'] not in has_id]
    if unseen:
        print('Overseas players not found in any scanned match (no ID yet):', ', '.join(unseen))
    return '\n'.join(out).rstrip() + '\n', list(candidates.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='start', required=True)
    ap.add_argument('--to', dest='end', required=True)
    ap.add_argument('--include-confirm', action='store_true')
    ap.add_argument('--no-live', action='store_true',
                    help='skip multi-day matches still in progress on the --to date')
    ap.add_argument('--max-scorecards', type=int, default=150)
    ap.add_argument('--no-cricbuzz', action='store_true', help='skip overseas players (Cricbuzz)')
    ap.add_argument('--max-cricbuzz', type=int, default=60)
    ap.add_argument('--out', default='player_watch_preview.txt')
    a = ap.parse_args()
    edak = os.environ.get('EDAK')
    if not edak:
        raise SystemExit('EDAK is not set (env var or .env).')
    text, cands = generate(date.fromisoformat(a.start), date.fromisoformat(a.end), edak,
                           a.include_confirm, a.no_live, a.max_scorecards,
                           a.no_cricbuzz, a.max_cricbuzz)
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(text)
    print('\n' + '=' * 60 + '\n' + text)
    if cands:
        print('UNCONFIRMED name matches (not in the message; confirm before adding the ID):')
        for v in cands:
            print('  ', v)


if __name__ == '__main__':
    main()
