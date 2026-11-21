FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# pristine sqlglot source at the pinned commit; the selector parses it to locate edited lines
RUN python -c "import io, tarfile, urllib.request; tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read())).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests || true
COPY selector.py patchutil.py tpcdsgate.py /app/
COPY map /app/map
RUN python /app/tpcdsgate.py > /app/map/tpcds.md5 || echo none > /app/map/tpcds.md5  # no digest: never skip
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
