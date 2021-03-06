from __future__ import unicode_literals

import multiprocessing

import gunicorn_app.app.base

from gunicorn_app.six import iteritems


def handler_app(environ, start_response):
    response_body = b'Works fine'
    status = '200 OK'

    response_headers = [
        ('Content-Type', 'text/plain'),
    ]

    start_response(status, response_headers)

    return [response_body]


class GunicornApp(gunicorn_app.app.base.BaseApplication):

    def load(self):
        return app


if __name__ == '__main__':
    options = {
        'bind': '%s:%s' % ('127.0.0.1', '8080'),
        'workers': number_of_workers(),
    }
    GunicornApp(handler_app, options).run()
