"""
Bridge for Django test discovery.

Django's default discovery pattern is `test*.py`. Our existing suite lives in
`core/tests.py`, which is not matched by that pattern when running:

    python manage.py test

This file ensures `python manage.py test` runs the same tests without having to
rename or restructure the existing module.
"""

from .tests import *  # noqa: F401,F403

