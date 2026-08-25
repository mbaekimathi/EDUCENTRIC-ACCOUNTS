"""
cPanel / Phusion Passenger entrypoint.

In cPanel → Setup Python App:
  Application root = this project folder
  Application startup file = passenger_wsgi.py
  Application Entry point = application

After code changes on the server, touch tmp/restart.txt to reload.
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
