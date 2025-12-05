#!/bin/bash
# Serve frontend static files for local development
exec python -m http.server 8090 --directory frontend
