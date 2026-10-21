"""Apply only the Archive dashboard edits to shared route and template files."""
from pathlib import Path
import sys

EDITS = {
    "labs/ui/routes.py": [
        ('LIVE_TABS = {\n    "overview": "Overview",\n',
         'from labs.services.live_archive import ARCHIVE_TABS\n\nLIVE_TABS = {\n    "overview": "Overview",\n    "archive": "Archive",\n'),
        ('            overview_cards = build_overview(conn)\n',
         '            overview_cards = [card for card in build_overview(conn)\n'
         '                              if card.get("tab", card["key"]) not in ARCHIVE_TABS]\n'),
        ('        live_tabs=LIVE_TABS,\n', '        live_tabs=LIVE_TABS,\n        archive_tabs=ARCHIVE_TABS,\n'),
    ],
    "templates/live_strategy.html": [
        ('''      {% for key, label in live_tabs.items() %}
      <a href="{{ url_for('labs.live_strategy', tab=key, date_from=date_from, date_to=date_to) }}" class="{{ 'active' if active_live_tab == key else '' }}">{{ label }}</a>
      {% endfor %}
    </div>

    {% if active_live_tab != 'overview' %}''',
         '''      {% for key, label in live_tabs.items() %}
      {% if key not in archive_tabs %}
      <a href="{{ url_for('labs.live_strategy', tab=key, date_from=date_from, date_to=date_to) }}" class="{{ 'active' if active_live_tab == key or (key == 'archive' and active_live_tab in archive_tabs) else '' }}">{{ label }}</a>
      {% endif %}
      {% endfor %}
    </div>

    {% if active_live_tab in archive_tabs %}
    <div class="live-tabs" aria-label="Archived strategies">
      {% for key, label in archive_tabs.items() %}
      <a href="{{ url_for('labs.live_strategy', tab=key, date_from=date_from, date_to=date_to) }}" class="{{ 'active' if active_live_tab == key else '' }}">{{ label }}</a>
      {% endfor %}
    </div>
    {% endif %}

    {% if active_live_tab not in ['overview', 'archive'] %}'''),
        ("    {% include '_overview.html' %}\n\n",
         "    {% include '_overview.html' %}\n\n    {% elif active_live_tab == 'archive' %}\n    {% include '_live_archive.html' %}\n\n"),
    ],
}


def patched(rel: str, text: str) -> str:
    text = text.replace('\r\n', '\n')
    for old, new in EDITS[rel]:
        if new in text:
            continue
        if text.count(old) != 1:
            raise ValueError(f'{rel}: expected one Archive anchor, found {text.count(old)}')
        text = text.replace(old, new, 1)
    return text


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    for rel in EDITS:
        path = root / rel
        original = path.read_text(encoding='utf-8')
        result = patched(rel, original)
        if result != original:
            path.write_text(result, encoding='utf-8', newline='')
        print(f'{rel}: verified Archive wiring')


if __name__ == '__main__':
    main()
