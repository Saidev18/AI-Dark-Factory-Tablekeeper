FROM python:3.12-slim-bookworm
COPY tests/test_http.py /test_http.py
COPY tests/test_pairs.py /test_pairs.py
COPY tests/test_policies.py /test_policies.py
ENTRYPOINT ["python", "/test_http.py"]
