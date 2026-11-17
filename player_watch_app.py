"""MI Player Watch: phone-friendly Streamlit page.

Run:  streamlit run player_watch_app.py
Secrets (.streamlit/secrets.toml or Streamlit Cloud): EDAK = "...", APP_PASSWORD = "..." (optional)
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

import streamlit as st

import mi_player_watch as pw

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title='MI Player Watch', page_icon='🏏', layout='centered')
st.markdown('<style>.block-container{padding-top:1.2rem;max-width:640px}</style>',
            unsafe_allow_html=True)
st.title('🏏 MI Player Watch')


def secret(key):
    try:
        return st.secrets[key]
    except Exception:
        return None


edak = secret('EDAK')
if not edak:
    st.error('EDAK secret is not set.')
    st.stop()

today = datetime.now(IST).date()
c1, c2 = st.columns(2)
start = c1.date_input('From', today - timedelta(days=1))
end = c2.date_input('To', today)
# Always on: Indian (BCCI feed) and overseas (Cricbuzz) players, plus matches in progress.
use_cb, live, confirm = True, True, False


@st.cache_data(ttl=600, show_spinner=False)
def run(start, end, use_cb, live, confirm):
    lines = []
    text, cands = pw.generate(start, end, edak, include_confirm=confirm, no_live=not live,
                              no_cricbuzz=not use_cb, log=lambda *x: lines.append(' '.join(map(str, x))))
    return text, cands, lines


if st.button('Get update', type='primary', use_container_width=True):
    if start > end:
        st.error('"From" is after "To".')
    else:
        with st.spinner('Reading scorecards...'):
            try:
                st.session_state['res'] = run(start, end, use_cb, live, confirm)
            except Exception as e:
                st.session_state['res'] = None
                st.error(f'Failed: {e}')

res = st.session_state.get('res')
if res:
    text, cands, lines = res
    st.caption('Tap the copy icon at the top right of the box, then paste into WhatsApp.')
    st.code(text, language=None)
    st.link_button('Open in WhatsApp', 'https://wa.me/?text=' + urllib.parse.quote(text),
                   use_container_width=True)
    st.download_button('Download .txt', text, 'player_watch.txt', use_container_width=True)
    if cands:
        with st.expander('Unconfirmed player IDs (not in the message)'):
            st.write('\n'.join(cands))
    with st.expander('Run log'):
        st.text('\n'.join(lines))
