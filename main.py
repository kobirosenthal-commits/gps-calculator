import os

from app import app
from waitress import serve


if __name__ == '__main__':
    host = os.environ.get('CREDIT_HOST', '0.0.0.0')
    port = int(os.environ.get('CREDIT_PORT', '5000'))
    debug = os.environ.get('CREDIT_DEBUG', '0') == '1'

    if debug:
        app.run(debug=True, host=host, port=port)
    else:
        threads = int(os.environ.get('CREDIT_THREADS', '8'))
        serve(app, host=host, port=port, threads=threads)
