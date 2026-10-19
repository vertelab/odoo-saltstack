# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Freeze-proof the minion logo.

Before 18.0.1.55.0 the rule was "if image is set, keep it" — which meant an
automatically assigned technology logo looked exactly like a hand-uploaded
one, and the image froze forever. A container that gained a role never got a
new logo.

This migration marks every image that matches one of the bundled logo files
as automatic (image_manual = False), so sync may replace it. Anything else is
left alone and treated as a human choice.
"""

import base64
import logging
import os

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    Minion = env['salt.minion']

    # Fingerprints of the bundled logos, as base64 of the raw file bytes.
    logo_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'static', 'src', 'img', 'logos',
    )
    known = set()
    try:
        for fname in os.listdir(logo_dir):
            if not fname.lower().endswith('.png'):
                continue
            with open(os.path.join(logo_dir, fname), 'rb') as f:
                known.add(base64.b64encode(f.read()).decode())
    except OSError:
        _logger.warning('Logo directory missing: %s — skipping migration', logo_dir)
        return

    auto = manual = 0
    for minion in Minion.with_context(active_test=False).search([]):
        if not minion.image:
            continue
        # Odoo stores the image base64-encoded; compare the stored bytes.
        if minion.image in known:
            minion.write({'image_manual': False})
            auto += 1
        else:
            minion.write({'image_manual': True})
            manual += 1

    _logger.info(
        'saltstack: logo migration — %s automatic, %s manual', auto, manual)
