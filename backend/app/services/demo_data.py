from __future__ import annotations

import io
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from app.config import Settings, settings
from app.db import db
from app.services.gaussian_fusion import recompute_observation_kalman
from app.services.photo_ingest import ingest_image

_NAMES = [
    ('Pilz', 'Steinpilz am Waldweg'), ('Pflanze', 'Farn im Schatten'), ('Ding', 'Verwitterter Wegweiser'),
    ('Ereignis', 'Morgennebel'), ('Pilz', 'Roter Täubling'), ('Pflanze', 'Wilde Brombeere'),
    ('Person', 'Wandergruppe'), ('Ding', 'Alte Bank'), ('Pilz', 'Parasol'), ('Pflanze', 'Waldmeister'),
    ('Ereignis', 'Regen nachmittags'), ('Ding', 'Bachbruecke'), ('Pilz', 'Hallimasch'), ('Pflanze', 'Buche im Herbst'),
    ('Unbekannt', 'Fund ohne Ortsangabe'), ('Pilz', 'Fund ohne Ortsangabe'),
]
_BASE = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc).timestamp()


def _fixture(index: int) -> bytes:
    image = Image.new('RGB', (900, 650), ((55 + index * 19) % 170, (105 + index * 11) % 200, (65 + index * 7) % 160))
    draw = ImageDraw.Draw(image)
    draw.ellipse((170, 100, 730, 660), fill=(220, 193, 135), outline=(40, 65, 43), width=10)
    draw.rectangle((0, 490, 900, 650), fill=(39, 84, 54))
    draw.text((35, 30), 'Synthetisches Demobild - kein Naturfoto', fill='white', stroke_width=2, stroke_fill='black')
    draw.text((40, 565), _NAMES[index][1], fill='white', stroke_width=2, stroke_fill='black')
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=80)
    return out.getvalue()


def seed_demo(config: Settings = settings) -> int:
    imported = 0
    for i, (category, title) in enumerate(_NAMES):
        fixed_id = f'00000000-0000-4000-8000-{i+1:012d}'
        media_id, result = ingest_image(source='demo', external_id=f'demo-fixture-{i+1:02d}', filename=f'{title}.jpg', data=_fixture(i), config=config, observation_id=fixed_id, media_id_override=f'10000000-0000-4000-8000-{i+1:012d}')
        if result == 'imported':
            # Fixture IDs and attributes are stable; subsequent seeds never overwrite edits.
            with db(config) as conn:
                if i not in (14, 15):
                    cluster = i % 3
                    lat = (53.559, 53.564, 53.569)[cluster] + (i % 4) * .001
                    lon = (9.805, 9.814, 9.823)[cluster] + (i % 3) * .001
                else:
                    lat = lon = None
                observed = int((_BASE + (i * 5 * 86400 if i != 15 else 0)) * 1000) if i != 15 else None
                conn.execute('UPDATE observations SET category=?,title=? WHERE id=?', (category, title, fixed_id))
                conn.execute('UPDATE media SET lat=?,lon=?,sigma_meters=?,observed_at_ms=?,sigma_seconds=?,location_source=?,time_source=? WHERE id=?',
                             (lat, lon, (25.0 + i) if lat is not None and i % 2 == 0 else None, observed,
                              3600.0 if observed is not None and i % 3 == 0 else None,
                              'demo' if lat is not None else 'unknown', 'demo' if observed is not None else 'unknown', media_id))
                recompute_observation_kalman(conn, fixed_id, config=config)
            imported += 1
    return imported
